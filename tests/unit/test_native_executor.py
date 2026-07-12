from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.recomp.native_executor import (
    NativeResumableExecutor,
    _partition_instructions_by_address,
)
from tools.recomp.x86_lifter import CpuState, SparseMemory, lift_x86_function


class NativeResumableExecutorTests(unittest.TestCase):
    def test_address_partition_does_not_shift_unrelated_modules(self) -> None:
        class Instruction:
            def __init__(self, address: int) -> None:
                self.address = address

        original = [Instruction(0x1000), Instruction(0x11000), Instruction(0x21000)]
        extended = [*original, Instruction(0x18000)]

        original_modules = _partition_instructions_by_address(
            original,
            maximum_count=3000,
        )
        extended_modules = _partition_instructions_by_address(
            extended,
            maximum_count=3000,
        )

        self.assertEqual(
            [[item.address for item in module] for module in original_modules],
            [[0x1000], [0x11000], [0x21000]],
        )
        self.assertEqual(
            [[item.address for item in module] for module in extended_modules],
            [[0x1000], [0x11000, 0x18000], [0x21000]],
        )

    def test_dense_address_partition_respects_module_limit(self) -> None:
        class Instruction:
            def __init__(self, address: int) -> None:
                self.address = address

        modules = _partition_instructions_by_address(
            [Instruction(address) for address in range(4000)],
            maximum_count=3000,
        )

        self.assertEqual(sum(len(module) for module in modules), 4000)
        self.assertTrue(all(len(module) <= 3000 for module in modules))

    def test_indirect_esp_relative_call_resolves_before_return_push(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("FF542404C3"),  # call dword ptr [esp + 4]; ret
            base_address=0x1000,
            symbol="esp_relative_callback",
        )
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory({0x8000: 0, 0x8004: 0x2000})

        def handler(cpu, _memory, _target, _trace) -> None:
            cpu.set_register("eax", 0x77)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                callback_addresses={0x2000},
            )
            returned_to = executor.run(
                state,
                memory,
                call_handlers={0x2000: handler},
                max_steps=20,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 0x77)

    def test_callback_target_yields_even_when_compiled_in_same_module(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("E801000000C331C0C3"),
            base_address=0x1000,
            symbol="same_module_callback",
        )
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory({0x8000: 0})

        def handler(cpu, _memory, _target, _trace) -> None:
            cpu.set_register("eax", 0x42)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                callback_addresses={0x1006},
                memory_callback_addresses={0x3004},
            )
            self.assertNotIn(0x1006 >> 12, executor._callback_pages)
            self.assertIn(0x3004 >> 12, executor._callback_pages)
            returned_to = executor.run(
                state,
                memory,
                call_handlers={0x1006: handler},
                max_steps=20,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 0x42)

    def test_live_slices_preserve_state_and_report_cumulative_steps(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("40EBFD"),  # inc eax; jmp 0x1000
            base_address=0x1000,
            symbol="sliced_loop",
        )
        state = CpuState.with_registers(eax=0, esp=0x8000)
        yields: list[int] = []
        # Windows keeps ctypes-loaded DLLs locked until process exit.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
            )
            returned_to = executor.run(
                state,
                SparseMemory(),
                max_steps=100,
                slice_steps=25,
                yield_handler=lambda _state, _memory, steps: yields.append(steps),
            )

        self.assertIn(returned_to, {0x1000, 0x1001})
        self.assertEqual(yields, [25, 50, 75, 100])
        self.assertEqual(state.get_register("eax"), 50)
        self.assertEqual(executor.last_run_summary["reason"], "step_budget")
        self.assertEqual(executor.last_run_summary["steps"], 100)

    def test_memory_predicate_yields_immediately_after_observed_write(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("A30000D0FE40C3"),  # mov [0xFED00000], eax; inc eax; ret
            base_address=0x1000,
            symbol="flip_boundary_yield",
        )

        class YieldMemory(SparseMemory):
            requested = False

            def write_u32(self, address: int, value: int) -> None:
                super().write_u32(address, value)
                if address == 0xFED00000:
                    self.requested = True

        memory = YieldMemory({0x8000: 0})
        state = CpuState.with_registers(eax=7, esp=0x8000)
        yields: list[int] = []

        def handle_yield(_state, _memory, steps: int) -> None:
            yields.append(steps)
            memory.requested = False

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(function, build_dir=Path(temp_dir))
            returned_to = executor.run(
                state,
                memory,
                max_steps=20,
                yield_handler=handle_yield,
                yield_predicate=lambda: memory.requested,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(yields, [1])
        self.assertEqual(state.get_register("eax"), 8)

    def test_rep_stosd_is_atomic_for_guest_step_budget(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("F3ABC3"),  # rep stosd; ret
            base_address=0x1000,
            symbol="sliced_rep_stosd",
        )
        state = CpuState.with_registers(
            eax=0xA1B2C3D4,
            ecx=100,
            edi=0x2000,
            esp=0x8000,
        )
        memory = SparseMemory()
        memory.write_u32(0x8000, 0)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
            )
            returned_to = executor.run(
                state,
                memory,
                max_steps=1,
            )

        self.assertEqual(returned_to, 0x1002)
        self.assertEqual(state.get_register("ecx"), 0)
        self.assertEqual(state.get_register("edi"), 0x2000 + 400)
        self.assertEqual(memory.read_u32(0x2000), 0xA1B2C3D4)
        self.assertEqual(memory.read_u32(0x2000 + 396), 0xA1B2C3D4)
        self.assertEqual(executor.last_run_summary["reason"], "step_budget")
        self.assertEqual(executor.last_run_summary["steps"], 1)


if __name__ == "__main__":
    unittest.main()
