from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.recomp.native_executor import (
    NativeExecutorError,
    NativeResumableExecutor,
    _partition_instructions_by_address,
)
from tools.recomp.x86_lifter import (
    CpuState,
    RESUMABLE_REPEAT_CHUNK_ITERATIONS,
    SparseMemory,
    lift_x86_function,
)


class NativeResumableExecutorTests(unittest.TestCase):
    def test_guest_divide_fault_is_reported_without_host_exception(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("31D2B80A00000031C9F7F1C3"),
            base_address=0x0F00,
            symbol="native_divide_by_zero",
        )
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory({0x8000: 0})

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(function, build_dir=Path(temp_dir))
            with self.assertRaisesRegex(
                NativeExecutorError,
                "division by zero at 0x00000F09",
            ):
                executor.run(state, memory, max_steps=20)

        self.assertEqual(executor.last_run_summary["reason"], "guest_arithmetic_fault")
        self.assertEqual(executor.last_run_summary["target_hex"], "0x00000F09")

    def test_absolute_byte_accumulator_moves_run_natively(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("A000300000A201300000C3"),
            base_address=0x1000,
            symbol="native_absolute_byte_moves",
        )
        state = CpuState.with_registers(eax=0xAABBCCDD, esp=0x8000)
        memory = SparseMemory({0x3000: 0x7B, 0x8000: 0})

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
            )
            returned_to = executor.run(state, memory, max_steps=10)

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 0xAABBCC7B)
        self.assertEqual(memory.read(0x3001, 1), b"{")

    def test_independent_module_functions_dispatch_across_native_modules(self) -> None:
        first = lift_x86_function(
            bytes.fromhex("E9FB0F0000"),  # jmp 0x2000
            base_address=0x1000,
            symbol="first_module",
        )
        second = lift_x86_function(
            bytes.fromhex("40C3"),  # inc eax; ret
            base_address=0x2000,
            symbol="second_module",
        )
        merged = type(first)(
            symbol="merged_modules",
            base_address=first.base_address,
            code_size=0x1002,
            instructions=(*first.instructions, *second.instructions),
        )
        state = CpuState.with_registers(eax=4, esp=0x8000)
        memory = SparseMemory({0x8000: 0})

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                merged,
                build_dir=Path(temp_dir),
                module_functions=[first, second],
            )
            returned_to = executor.run(state, memory, max_steps=10)

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 5)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_module_count"], 2)
        self.assertEqual(performance["native_dispatch_count"], 1)
        self.assertEqual(performance["native_module_call_count"], 2)

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
        handler_hot_paths = executor.last_run_summary["performance"][
            "handler_hot_paths"
        ]
        self.assertEqual(handler_hot_paths[0]["target_hex"], "0x00002000")
        self.assertEqual(handler_hot_paths[0]["name"], "handler")
        self.assertEqual(handler_hot_paths[0]["count"], 1)

    def test_page_worklists_preserve_native_cache_coherence(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "A100200000"  # mov eax, [0x2000]
                "E8F61F0000"  # call 0x3000
                "A100200000"  # mov eax, [0x2000]
                "C3"
            ),
            base_address=0x1000,
            symbol="page_worklist_coherence",
        )
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory({0x2000: 1, 0x8000: 0})

        def handler(_cpu, guest_memory, _target, _trace) -> None:
            guest_memory.write_u32(0x2000, 2)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                callback_addresses={0x3000},
            )
            returned_to = executor.run(
                state,
                memory,
                call_handlers={0x3000: handler},
                max_steps=20,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 2)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["invalidated_page_count"], 1)
        self.assertEqual(
            performance["dirty_page_scan_count"],
            performance["dirty_page_writeback_count"],
        )
        self.assertEqual(performance["dirty_page_list_peak"], 1)
        self.assertEqual(performance["dirty_byte_writeback_count"], 4)

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

    def test_opt_in_memory_write_observer_reports_guest_instruction(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("C7050430000078563412C3"),
            base_address=0x1000,
            symbol="observed_native_write",
        )
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory({0x8000: 0})
        writes: list[tuple[int, int, int, int, int]] = []

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                memory_callback_addresses={0x3004},
                synchronize_eip_for_callbacks=True,
            )
            returned_to = executor.run(
                state,
                memory,
                memory_write_observer=lambda eip, address, size, value, steps: writes.append(
                    (eip, address, size, value, steps)
                ),
                max_steps=10,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(memory.read_u32(0x3004), 0x12345678)
        self.assertEqual(writes, [(0x1000, 0x3004, 4, 0x12345678, 1)])

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
        performance = executor.last_run_summary["performance"]
        self.assertGreater(performance["elapsed_us"], 0)
        self.assertGreater(performance["steps_per_second"], 0)
        self.assertEqual(performance["native_module_count"], 1)
        self.assertGreater(performance["native_dispatch_count"], 0)
        self.assertGreaterEqual(
            performance["native_module_call_count"],
            performance["native_dispatch_count"],
        )
        self.assertEqual(performance["slice_yield_count"], 4)
        self.assertGreaterEqual(performance["dirty_sync_call_count"], 4)
        self.assertIn("native_dispatch", performance["timings"])
        self.assertIn("yield_handler", performance["timings"])
        self.assertEqual(
            performance["hot_paths"][0]["total_us"],
            max(metric["total_us"] for metric in performance["timings"].values()),
        )

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

    def test_large_rep_stosd_cooperatively_yields_and_resumes(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("F3ABC3"),  # rep stosd; ret
            base_address=0x1000,
            symbol="cooperative_rep_stosd",
        )
        state = CpuState.with_registers(
            eax=0xA1B2C3D4,
            ecx=RESUMABLE_REPEAT_CHUNK_ITERATIONS + 3,
            edi=0x2000,
            esp=0x800000,
        )
        memory = SparseMemory({0x800000: 0})
        yields: list[int] = []
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
            )
            stopped_at = executor.run(
                state,
                memory,
                yield_handler=(
                    lambda _state, _memory, steps: yields.append(steps) or False
                ),
            )

            self.assertEqual(stopped_at, 0x1000)
            self.assertEqual(yields, [0])
            self.assertEqual(state.get_register("ecx"), 3)
            self.assertEqual(
                state.get_register("edi"),
                0x2000 + RESUMABLE_REPEAT_CHUNK_ITERATIONS * 4,
            )
            self.assertEqual(
                executor.last_run_summary["reason"], "yield_handler_stop"
            )

            returned_to = executor.run(state, memory, max_steps=10)

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("ecx"), 0)
        self.assertEqual(
            state.get_register("edi"),
            0x2000 + (RESUMABLE_REPEAT_CHUNK_ITERATIONS + 3) * 4,
        )
        self.assertEqual(
            memory.read_u32(state.get_register("edi") - 4), 0xA1B2C3D4
        )


if __name__ == "__main__":
    unittest.main()
