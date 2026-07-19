from __future__ import annotations

import math
import struct
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
    def test_batched_x87_frontiers_execute_natively(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "D9E8"  # fld1
                "D9E8"  # fld1
                "D9F3"  # fpatan
                "DA0500200000"  # fiadd dword [0x2000]
                "D90504200000"  # fld dword [0x2004]
                "DEF1"  # fdivrp st(1), st
                "DB1D08200000"  # fistp dword [0x2008]
                "DB2D10200000"  # fld tbyte [0x2010]
                "DD3500210000"  # fnsave [0x2100]
                "DD2500210000"  # frstor [0x2100]
                "DDC0"  # ffree st(0)
                "C3"
            ),
            base_address=0x6000,
            symbol="native_batched_x87_frontiers",
        )
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory(
            {
                0x2000: struct.pack("<i", 2),
                0x2004: struct.pack("<f", 8.0),
                0x2010: bytes.fromhex("00000000000000C00040"),
                0x2100: bytes(108),
                0x8000: 0,
            }
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(function, build_dir=Path(temp_dir))
            returned_to = executor.run(state, memory, max_steps=32)

        self.assertEqual(returned_to, 0)
        self.assertEqual(memory.read_u32(0x2008), 3)
        self.assertEqual(state.fpu_stack, [])
        self.assertEqual(state.fpu_control_word, 0x037F)

    def test_batched_vector_frontiers_execute_natively(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "0F100500200000"  # movups xmm0, [0x2000]
                "0F5D0510200000"  # minps xmm0, [0x2010]
                "0F5F0520200000"  # maxps xmm0, [0x2020]
                "0F110530200000"  # movups [0x2030], xmm0
                "F30F2DC0"  # cvtss2si eax, xmm0
                "0F7F0540200000"  # movq [0x2040], mm0
                "C3"
            ),
            base_address=0x6100,
            symbol="native_batched_vector_frontiers",
        )
        state = CpuState.with_registers(esp=0x8000)
        state.set_mmx_register("mm0", 0x1122334455667788)
        memory = SparseMemory(
            {
                0x2000: struct.pack("<4f", 5.0, -2.0, float("nan"), -0.0),
                0x2010: struct.pack("<4f", 4.0, -3.0, 1.0, 0.0),
                0x2020: struct.pack("<4f", 6.0, -4.0, 2.0, -0.0),
                0x2030: bytes(16),
                0x2040: bytes(8),
                0x8000: 0,
            }
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(function, build_dir=Path(temp_dir))
            returned_to = executor.run(state, memory, max_steps=20)

        vector = struct.unpack("<4f", memory.read(0x2030, 16))
        self.assertEqual(returned_to, 0)
        self.assertEqual(vector[:3], (6.0, -3.0, 2.0))
        self.assertLess(math.copysign(1.0, vector[3]), 0.0)
        self.assertEqual(state.get_register("eax"), 6)
        self.assertEqual(memory.read(0x2040, 8), bytes.fromhex("8877665544332211"))

    def test_batched_integer_string_and_system_frontiers_execute_natively(self) -> None:
        integer_function = lift_x86_function(
            bytes.fromhex(
                "FDF3A5FC"  # std; rep movsd; cld
                "C1C910"  # ror ecx, 16
                "66C1C808"  # ror ax, 8
                "D1D8"  # rcr eax, 1
                "B108"  # mov cl, 8
                "0FADD0"  # shrd eax, edx, cl
                "BA00500000"  # mov edx, 0x5000
                "B803000000"  # mov eax, 3
                "F00FC102"  # lock xadd [edx], eax
                "86E0"  # xchg al, ah
                "91"  # xchg ecx, eax
                "9494"  # xchg esp, eax twice
                "9E"  # sahf
                "1CFF"  # sbb al, 0xff
                "3401"  # xor al, 1
                "C3"
            ),
            base_address=0x6300,
            symbol="native_batched_integer_frontiers",
        )
        integer_state = CpuState.with_registers(
            eax=0xAABBCCDD,
            ecx=3,
            edx=0x11223344,
            esi=0x3008,
            edi=0x4008,
            esp=0x8000,
        )
        integer_state.flags.cf = True
        integer_memory = SparseMemory(
            {
                0x3000: struct.pack("<III", 1, 2, 3),
                0x4000: bytes(12),
                0x5000: 5,
                0x8000: 0,
            }
        )

        system_function = lift_x86_function(
            bytes.fromhex(
                "0F010500600000"  # sgdt [0x6000]
                "FAFBEC"  # cli; sti; in al, dx
                "EA117000000800"  # jmp 0x0008:0x00007011
                "C3"
            ),
            base_address=0x7000,
            symbol="native_batched_system_frontiers",
        )
        system_state = CpuState.with_registers(
            eax=0xFFFFFFFF,
            edx=0x80C0,
            esp=0x8100,
            gdtr_base=0x12345000,
            gdtr_limit=0x03FF,
        )
        system_memory = SparseMemory({0x6000: bytes(6), 0x8100: 0})

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            integer_executor = NativeResumableExecutor(
                integer_function,
                build_dir=Path(temp_dir) / "integer",
            )
            integer_return = integer_executor.run(
                integer_state,
                integer_memory,
                max_steps=32,
            )
            system_executor = NativeResumableExecutor(
                system_function,
                build_dir=Path(temp_dir) / "system",
            )
            system_return = system_executor.run(
                system_state,
                system_memory,
                max_steps=16,
            )

        self.assertEqual(integer_return, 0)
        self.assertEqual(integer_memory.read(0x4000, 12), struct.pack("<III", 1, 2, 3))
        self.assertEqual(integer_memory.read_u32(0x5000), 8)
        self.assertFalse(integer_state.flags.df)
        self.assertEqual(system_return, 0)
        self.assertEqual(
            system_memory.read(0x6000, 6),
            struct.pack("<HI", 0x03FF, 0x12345000),
        )
        self.assertEqual(system_state.get_register("eax"), 0xFFFFFF00)
        self.assertTrue(system_state.flags.interrupt_enabled)
        self.assertEqual(system_state.cs_selector, 0x0008)

    def test_fsubrp_reverse_subtracts_and_pops_natively(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "D90500200000"  # fld dword [0x2000]
                "D90504200000"  # fld dword [0x2004]
                "DEE1"  # fsubrp st(1), st
                "D91D08200000"  # fstp dword [0x2008]
                "C3"
            ),
            base_address=0x00062561,
            symbol="native_fsubrp",
        )
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory(
            {
                0x2000: struct.pack("<f", 4.0),
                0x2004: struct.pack("<f", 10.0),
                0x8000: 0,
            }
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
            )
            returned_to = executor.run(state, memory, max_steps=10)

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.fpu_stack, [])
        self.assertEqual(struct.unpack("<f", memory.read(0x2008, 4))[0], 6.0)

    def test_fimul_multiplies_x87_top_by_signed_integer_natively(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "D9442404"  # fld dword [esp+4]
                "DA4C2408"  # fimul dword [esp+8]
                "D91D00200000"  # fstp dword [0x2000]
                "C3"
            ),
            base_address=0x00082E71,
            symbol="native_fimul",
        )
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory(
            {
                0x8000: 0,
                0x8004: struct.pack("<f", 1.5),
                0x8008: -3,
            }
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
            )
            returned_to = executor.run(state, memory, max_steps=10)

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.fpu_stack, [])
        self.assertEqual(struct.unpack("<f", memory.read(0x2000, 4))[0], -4.5)

    def test_fidiv_divides_x87_top_by_signed_integer_natively(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "DB0500200000"  # fild dword [0x2000]
                "DA3504200000"  # fidiv dword [0x2004]
                "D91D08200000"  # fstp dword [0x2008]
                "C3"
            ),
            base_address=0x0D00,
            symbol="native_fidiv",
        )
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory(
            {
                0x2000: 10,
                0x2004: 4,
                0x8000: 0,
            }
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
            )
            returned_to = executor.run(state, memory, max_steps=10)

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.fpu_stack, [])
        self.assertEqual(struct.unpack("<f", memory.read(0x2008, 4))[0], 2.5)

    def test_fcompp_compares_and_pops_both_x87_operands_natively(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "D90500200000"  # fld dword [0x2000]
                "D90504200000"  # fld dword [0x2004]
                "DED9"  # fcompp
                "DFE0"  # fnstsw ax
                "C3"
            ),
            base_address=0x0E00,
            symbol="native_fcompp",
        )
        state = CpuState.with_registers(
            esp=0x8000,
            fpu_status_word=0x4000,
        )
        memory = SparseMemory(
            {
                0x2000: struct.pack("<f", 3.0),
                0x2004: struct.pack("<f", 2.0),
                0x8000: 0,
            }
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
            )
            returned_to = executor.run(state, memory, max_steps=10)

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.fpu_stack, [])
        self.assertEqual(state.fpu_status_word, 0x0100)
        self.assertEqual(state.get_register("eax") & 0xFFFF, 0x0100)

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
        hot_targets = {
            item["target"]: item
            for item in performance["native_dispatch_hot_targets"]
        }
        self.assertEqual(set(hot_targets), {0x1000, 0x2000})
        self.assertEqual(hot_targets[0x1000]["module_calls"], 1)
        self.assertEqual(hot_targets[0x2000]["module_calls"], 1)
        self.assertEqual(
            sum(item["guest_steps"] for item in hot_targets.values()),
            executor.last_run_summary["steps"],
        )

    def test_reused_native_cache_invalidates_host_memory_changes(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("A100300000C3"),  # mov eax, [0x3000]; ret
            base_address=0x1000,
            symbol="reused_cache_invalidation",
        )
        memory = SparseMemory({0x3000: 1, 0x8000: 0})

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
            )
            first_state = CpuState.with_registers(esp=0x8000)
            self.assertEqual(executor.run(first_state, memory, max_steps=10), 0)
            self.assertEqual(first_state.get_register("eax"), 1)
            self.assertFalse(
                executor.last_run_summary["performance"][
                    "native_cache_context_reused"
                ]
            )

            memory.write_u32(0x3000, 2)
            second_state = CpuState.with_registers(esp=0x8000)
            self.assertEqual(executor.run(second_state, memory, max_steps=10), 0)

        self.assertEqual(second_state.get_register("eax"), 2)
        self.assertTrue(
            executor.last_run_summary["performance"][
                "native_cache_context_reused"
            ]
        )
        self.assertEqual(
            executor.last_run_summary["performance"]["invalidated_page_count"],
            1,
        )

    def test_frontier_executor_reuses_clean_page_cache(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("A100300000C3"),  # mov eax, [0x3000]; ret
            base_address=0x1000,
            symbol="frontier_cache_reuse",
        )
        memory = SparseMemory({0x3000: 1, 0x8000: 0})

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            build_dir = Path(temp_dir)
            first = NativeResumableExecutor(function, build_dir=build_dir)
            first_state = CpuState.with_registers(esp=0x8000)
            self.assertEqual(first.run(first_state, memory, max_steps=10), 0)

            second = NativeResumableExecutor(function, build_dir=build_dir)
            self.assertEqual(second.seed_page_cache_from(first, memory), 2)
            second_state = CpuState.with_registers(esp=0x8000)
            self.assertEqual(second.run(second_state, memory, max_steps=10), 0)
            self.assertEqual(
                second.last_run_summary["performance"]["page_cache_fill_count"],
                0,
            )

            memory.write_u32(0x3000, 2)
            third = NativeResumableExecutor(function, build_dir=build_dir)
            self.assertEqual(third.seed_page_cache_from(second, memory), 1)
            third_state = CpuState.with_registers(esp=0x8000)
            self.assertEqual(third.run(third_state, memory, max_steps=10), 0)

        self.assertEqual(second_state.get_register("eax"), 1)
        self.assertEqual(third_state.get_register("eax"), 2)
        self.assertTrue(
            second.last_run_summary["performance"]["native_cache_context_reused"]
        )
        self.assertEqual(
            third.last_run_summary["performance"]["page_cache_fill_count"],
            1,
        )

    def test_word_and_qword_helpers_use_native_page_cache(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "66A100300000"  # mov ax, [0x3000]
                "0F6F0D00400000"  # movq mm1, [0x4000]
                "C3"
            ),
            base_address=0x1000,
            symbol="compound_page_cache",
        )
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory(
            {
                0x3000: bytes.fromhex("3412"),
                0x4000: bytes.fromhex("8877665544332211"),
                0x8000: 0,
            }
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(function, build_dir=Path(temp_dir))
            self.assertEqual(executor.run(state, memory, max_steps=10), 0)

        performance = executor.last_run_summary["performance"]
        self.assertEqual(state.get_register("eax"), 0x1234)
        self.assertEqual(state.get_mmx_register("mm1"), 0x1122334455667788)
        self.assertEqual(performance["read_u8_callback_count"], 1)
        self.assertEqual(performance["read_u32_callback_count"], 2)
        self.assertEqual(performance["page_cache_fill_count"], 3)

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

    def test_exact_memory_callback_does_not_disable_neighboring_page_cache(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "A100300000"      # mov eax, [0x3000]
                "8B1D04300000"    # mov ebx, [0x3004]
                "030500300000"    # add eax, [0x3000]
                "C3"
            ),
            base_address=0x1000,
            symbol="exact_memory_callback",
        )
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory(
            {
                0x3000: 7,
                0x3004: 9,
                0x8000: 0,
            }
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                memory_callback_addresses={0x3004},
            )
            returned_to = executor.run(state, memory, max_steps=20)

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 14)
        self.assertEqual(state.get_register("ebx"), 9)
        performance = executor.last_run_summary["performance"]
        # The exact callback keeps the existing page buffer coherent in place;
        # only the data and return-address pages need cache fills.
        self.assertEqual(performance["read_u32_callback_count"], 3)
        self.assertEqual(performance["exact_read_u32_callback_count"], 1)
        self.assertEqual(performance["page_miss_read_u32_callback_count"], 2)
        self.assertEqual(performance["page_cache_fill_count"], 2)
        self.assertGreater(performance["dirty_sync_no_work_count"], 0)
        read_samples = performance["read_callback_sampling"]
        self.assertEqual(read_samples["interval"], 1024)
        self.assertEqual(
            {
                (sample["kind"], sample["address_hex"])
                for sample in read_samples["hot_addresses"]
            },
            {
                ("exact_u32", "0x00003004"),
                ("page_miss_u32", "0x00003000"),
            },
        )

    def test_memory_policy_can_cache_high_title_ram_around_exact_callbacks(self) -> None:
        high_address = 0x83380000
        function = lift_x86_function(
            b"\xA1"
            + struct.pack("<I", high_address)
            + b"\x8B\x1D"
            + struct.pack("<I", high_address + 4)
            + b"\x03\x05"
            + struct.pack("<I", high_address)
            + b"\xC3",
            base_address=0x1000,
            symbol="high_title_ram_page_cache",
        )

        class HighTitleRamMemory(SparseMemory):
            def native_cacheable_address(self, address: int) -> bool:
                return address < 0x80000000 or 0x82000000 <= address < 0x84000000

        state = CpuState.with_registers(esp=0x8000)
        memory = HighTitleRamMemory(
            {
                high_address: 7,
                high_address + 4: 9,
                0x8000: 0,
            }
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                memory_callback_addresses={high_address + 4},
            )
            returned_to = executor.run(state, memory, max_steps=20)

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 14)
        self.assertEqual(state.get_register("ebx"), 9)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["read_u32_callback_count"], 3)
        self.assertEqual(performance["exact_read_u32_callback_count"], 1)
        self.assertEqual(performance["page_miss_read_u32_callback_count"], 2)
        self.assertEqual(performance["page_cache_fill_count"], 2)

    def test_dynamic_exact_reads_refresh_cached_bytes_without_page_refills(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "A100300000"      # mov eax, [0x3000]
                "8B1D04300000"    # mov ebx, [0x3004]
                "8B0D04300000"    # mov ecx, [0x3004]
                "030500300000"    # add eax, [0x3000]
                "C3"
            ),
            base_address=0x1000,
            symbol="dynamic_exact_range_refresh",
        )

        class DynamicMemory(SparseMemory):
            def read_u32(self, address: int) -> int:
                value = super().read_u32(address)
                if address == 0x3004:
                    super().write_u32(address, value + 1)
                return value

        state = CpuState.with_registers(esp=0x8000)
        memory = DynamicMemory({0x3000: 7, 0x3004: 9, 0x8000: 0})

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                memory_callback_addresses={0x3004},
            )
            returned_to = executor.run(state, memory, max_steps=20)

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 14)
        self.assertEqual(state.get_register("ebx"), 9)
        self.assertEqual(state.get_register("ecx"), 10)
        self.assertEqual(memory.read_u32(0x3004), 11)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["page_cache_fill_count"], 2)
        self.assertEqual(performance["exact_read_u32_callback_count"], 2)
        self.assertEqual(performance["cached_range_refresh_count"], 2)
        self.assertGreaterEqual(performance["cached_range_refresh_byte_count"], 8)

    def test_exact_callback_syncs_only_declared_dependency_pages(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "A100300000"      # mov eax, [0x3000]
                "A100500000"      # mov eax, [0x5000]
                "C7050030000005000000"  # mov dword ptr [0x3000], 5
                "C7050050000007000000"  # mov dword ptr [0x5000], 7
                "8B1D04400000"    # mov ebx, [0x4004]
                "C3"
            ),
            base_address=0x1000,
            symbol="selective_exact_callback_sync",
        )

        class DependencyMemory(SparseMemory):
            def native_memory_callback_dependency_addresses(
                self,
                address: int,
                size: int,
                *,
                is_write: bool,
            ) -> tuple[int, ...] | None:
                if not is_write and address == 0x4004 and size == 4:
                    return (0x3000,)
                return None

            def read_u32(self, address: int) -> int:
                if address == 0x4004:
                    return SparseMemory.read_u32(self, 0x3000)
                return super().read_u32(address)

        state = CpuState.with_registers(esp=0x8000)
        memory = DependencyMemory(
            {
                0x3000: 1,
                0x4004: 0,
                0x5000: 2,
                0x8000: 0,
            }
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                memory_callback_addresses={0x4004},
            )
            returned_to = executor.run(state, memory, max_steps=20)

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("ebx"), 5)
        self.assertEqual(memory.read_u32(0x5000), 7)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["selective_dirty_sync_call_count"], 1)
        self.assertEqual(
            performance["selective_dirty_sync_page_writeback_count"],
            1,
        )
        self.assertGreaterEqual(
            performance["selective_dirty_sync_retained_page_count"],
            1,
        )

    def test_call_handler_yield_resumes_after_completed_guest_return(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "E8FB0F0000"  # call 0x2000
                "40"          # inc eax
                "C3"
            ),
            base_address=0x1000,
            symbol="cooperative_call_yield",
        )
        state = CpuState.with_registers(eax=3, esp=0x8000)
        memory = SparseMemory({0x8000: 0})

        def handler(cpu, _memory, _target, _trace) -> None:
            cpu.set_register("eax", cpu.get_register("eax") + 4)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                callback_addresses={0x2000},
            )
            stopped_at = executor.run(
                state,
                memory,
                call_handlers={0x2000: handler},
                call_handler_yield_predicate=lambda target: target == 0x2000,
                max_steps=20,
            )

            self.assertEqual(stopped_at, 0x1005)
            self.assertEqual(state.eip, 0x1005)
            self.assertEqual(state.get_register("esp"), 0x8000)
            self.assertEqual(state.get_register("eax"), 7)
            self.assertEqual(
                executor.last_run_summary["reason"], "call_handler_yield"
            )
            self.assertEqual(
                executor.last_run_summary["performance"][
                    "call_handler_yield_count"
                ],
                1,
            )

            returned_to = executor.run(state, memory, max_steps=20)

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 8)

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

    def test_dynamic_memory_callback_range_preserves_every_native_write(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "C7050030000011111111"  # mov dword ptr [0x3000], 0x11111111
                "C7050430000022222222"  # mov dword ptr [0x3004], 0x22222222
                "C3"
            ),
            base_address=0x1000,
            symbol="dynamic_observed_native_writes",
        )
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory({0x8000: 0})
        writes: list[tuple[int, int, int, int, int]] = []

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                synchronize_eip_for_callbacks=True,
            )
            returned_to = executor.run(
                state,
                memory,
                memory_write_observer=lambda eip, address, size, value, steps: writes.append(
                    (eip, address, size, value, steps)
                ),
                memory_write_observer_ranges_provider=lambda: ((0x3000, 0x4000),),
                max_steps=10,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(memory.read_u32(0x3000), 0x11111111)
        self.assertEqual(memory.read_u32(0x3004), 0x22222222)
        self.assertEqual(
            writes,
            [
                (0x1000, 0x3000, 4, 0x11111111, 1),
                (0x100A, 0x3004, 4, 0x22222222, 2),
            ],
        )

    def test_dynamic_memory_callback_range_can_drain_one_native_batch(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "A100300000"
                "C7050030000011111111"
                "C7050430000022222222"
                "C3"
            ),
            base_address=0x1000,
            symbol="batched_observed_native_writes",
        )
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory({0x8000: 0})
        batches: list[list[tuple[int, int, int]]] = []
        packed_batches: list[tuple[bytes, int, bool]] = []

        def observe_batch(
            packed_records,
            addresses,
            values,
            sizes,
            eips,
            source_addresses,
            steps,
            count,
            observed_range_start,
            contiguous_u32,
        ) -> None:
            packed_batches.append(
                (
                    bytes(packed_records[: count * 16]),
                    observed_range_start,
                    contiguous_u32,
                )
            )
            batches.append(
                [
                    (
                        int(addresses[index]),
                        int(values[index]),
                        int(sizes[index]),
                    )
                    for index in range(count)
                ]
            )
            self.assertEqual(
                [int(eips[index]) for index in range(count)],
                [0x1005, 0x100F],
            )
            self.assertEqual(
                [int(source_addresses[index]) for index in range(count)],
                [0, 0],
            )
            self.assertEqual(
                [int(steps[index]) for index in range(count)],
                [2, 3],
            )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
            )
            returned_to = executor.run(
                state,
                memory,
                memory_write_batch_observer=observe_batch,
                memory_write_batch_address_base=0x80000000,
                memory_write_observer_ranges_provider=lambda: ((0x3000, 0x4000),),
                max_steps=10,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(
            batches,
            [[
                (0x3000, 0x11111111, 4),
                (0x3004, 0x22222222, 4),
            ]],
        )
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_observed_write_count"], 2)
        self.assertEqual(performance["native_observed_write_batch_count"], 1)
        self.assertIn("memory_write_observer_batch", performance["timings"])
        packed, range_start, contiguous = packed_batches[0]
        self.assertEqual(range_start, 0x3000)
        self.assertTrue(contiguous)
        self.assertEqual(
            [
                struct.unpack_from("<BBHI8s", packed, offset)[3]
                for offset in (0, 16)
            ],
            [0x80000000, 0x80000004],
        )

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
