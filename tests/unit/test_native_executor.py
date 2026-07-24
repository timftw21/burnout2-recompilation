from __future__ import annotations

import ctypes
import math
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.recomp.native_executor import (
    NATIVE_HOST_SERVICE_COLD_CALLBACK,
    NATIVE_HOST_SERVICE_LIFECYCLE_CREATE_WORKER,
    NATIVE_HOST_SERVICE_RETURN_CONSTANT,
    NATIVE_HOST_SERVICE_SYSTEM_TIME,
    NATIVE_WORKER_COMPLETED,
    NATIVE_WORKER_READY,
    NATIVE_WORKER_RUNNING,
    NATIVE_DISPATCH_EDGE_CAPACITY_LIMIT,
    NATIVE_DISPATCH_EDGE_REPORT_LIMIT,
    NativeCooperativeSchedulerState,
    NativeExecutorError,
    NativeHostServiceEntry,
    NativeHostServiceState,
    NativeModuleManifest,
    NativeResumableExecutor,
    NativeWorkerLifecycleState,
    _partition_instructions_by_address,
    _partition_instructions_for_call_fusion,
)
from tools.recomp.x86_lifter import (
    CpuState,
    ExecutionTrace,
    LiftedFunction,
    NativeFastPath,
    RESUMABLE_REPEAT_CHUNK_ITERATIONS,
    SparseMemory,
    lift_x86_function,
)


class NativeResumableExecutorTests(unittest.TestCase):
    def test_native_resource_method_scanner_compacts_packets(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("C3"),
            base_address=0x1000,
            symbol="native_resource_method_scanner",
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(function, build_dir=Path(temp_dir))
            indexed_words = (
                (2 << 16) | 0,
                (7 << 16) | 3,
                (4 << 16) | 9,
                (8 << 16) | 1,
            )
            words = (
                (512 << 18) | 0x0400,
                *([0] * 512),
                (1 << 18) | 0x17FC,
                6,
                0x40000000 | (len(indexed_words) << 18) | 0x1800,
                *indexed_words,
                (1 << 18) | 0x012C,
                1,
            )
            packed = bytearray(struct.pack(f"<{len(words)}I", *words))
            (
                methods,
                values,
                method_count,
                data_word_count,
                tracked_word_count,
                skipped_word_count,
                aggregated_index_word_count,
            ) = executor.scan_resource_methods(packed, len(packed))

            self.assertEqual(
                [int(methods[index]) for index in range(method_count)],
                [0x17FC, 0x1808, 0x012C],
            )
            self.assertEqual(
                [int(values[index]) for index in range(method_count)],
                [6, 9, 1],
            )
            self.assertEqual(data_word_count, 518)
            self.assertEqual(tracked_word_count, 6)
            self.assertEqual(skipped_word_count, 512)
            self.assertEqual(aggregated_index_word_count, 4)

    def test_native_resource_span_scanner_retains_packet_state(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("C3"),
            base_address=0x1000,
            symbol="native_resource_span_method_scanner",
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(function, build_dir=Path(temp_dir))
            vertex_address = 0x22010000
            vertex_format = (12 << 8) | (3 << 4) | 2
            texture_address = 0x01234000
            texture_format = 0x022F0600
            texture_rect = (64 << 16) | 32
            first_words = (
                (2 << 18) | 0x0400,
                0,
                0,
                (2 << 18) | 0x1720,
                vertex_address,
                vertex_address + 0x40,
                (2 << 18) | 0x1760,
                vertex_format,
                vertex_format,
                (1 << 18) | 0x1B00,
                texture_address,
                (1 << 18) | 0x1B04,
                texture_format,
                (1 << 18) | 0x1B18,
                texture_rect,
                (1 << 18) | 0x17FC,
                6,
                0x40000000 | (4 << 18) | 0x1800,
                (2 << 16) | 0,
                (7 << 16) | 3,
            )
            second_words = (
                (4 << 16) | 9,
                (8 << 16) | 1,
                (1 << 18) | 0x1800,
                (11 << 16) | 10,
                (1 << 18) | 0x17FC,
                0,
                (1 << 18) | 0x012C,
                1,
            )

            def scan(words: tuple[int, ...], address: int, flag: int):
                packed = struct.pack(f"<{len(words)}I", *words)
                payload = (ctypes.c_uint8 * len(packed)).from_buffer_copy(packed)
                addresses = (ctypes.c_uint32 * 1)(address)
                offsets = (ctypes.c_uint32 * 1)(0)
                sizes = (ctypes.c_uint32 * 1)(len(packed))
                flags = (ctypes.c_uint8 * 1)(flag)
                return executor.scan_resource_method_spans(
                    payload,
                    len(packed),
                    addresses,
                    offsets,
                    sizes,
                    flags,
                    1,
                )

            first = scan(first_words, 0x21B80000, 0)
            self.assertEqual(first[3], 0)
            self.assertEqual(first[4:], (12, 10, 2, 2))

            retained_state = executor.export_resource_method_span_state()
            replacement = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
            )
            replacement.import_resource_method_span_state(retained_state)
            executor = replacement

            second = scan(
                second_words,
                0x21B80000 + len(first_words) * 4,
                1,
            )
            self.assertEqual(
                [int(second[0][index]) for index in range(second[3])],
                [
                    0xFFF00000,
                    0xFFF00010,
                    0xFFF00020,
                    0xFFF00100,
                    0xFFF00104,
                ],
            )
            self.assertEqual(
                [int(second[1][index]) for index in range(second[3])],
                [
                    texture_address,
                    texture_format,
                    texture_rect,
                    vertex_address,
                    vertex_address + 0x40 + 12 * 12,
                ],
            )
            self.assertEqual(
                [int(second[2][index]) for index in range(second[3])],
                [1, 1, 1, 1, 1],
            )
            self.assertEqual(second[4:], (5, 5, 0, 2))
            finished = executor.finish_resource_method_spans()
            self.assertEqual(finished[3:], (0, 0, 0, 0, 0))

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
        self.assertEqual(
            executor.last_run_summary["performance"]["native_module_exit_profile"][
                "reason_counts"
            ]["fault"],
            1,
        )

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

    def test_guarded_native_fast_path_falls_back_and_reports_invocations(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("40C3"),  # inc eax; ret
            base_address=0x1000,
            symbol="guarded_native_fast_path",
        )
        fast_path = NativeFastPath(
            name="test_add_ten",
            guard="ctx->eax == 4u",
            body=(
                "b2r_record_native_fast_path(ctx, 0x00001000u);",
                "ctx->eax += 10u;",
                "const uint32_t b2r_return_address = b2r_read_u32(ctx, ctx->esp);",
                "ctx->esp += 4u;",
                "eip = b2r_return_address;",
                "pending_module_exit_reason = B2R_MODULE_EXIT_RETURN;",
                "continue;",
            ),
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                native_fast_paths={0x1000: fast_path},
            )
            fast_state = CpuState.with_registers(eax=4, esp=0x8000)
            returned_to = executor.run(
                fast_state,
                SparseMemory({0x8000: 0}),
                max_steps=10,
            )
            fast_performance = executor.last_run_summary["performance"]

            fallback_state = CpuState.with_registers(eax=3, esp=0x8000)
            fallback_returned_to = executor.run(
                fallback_state,
                SparseMemory({0x8000: 0}),
                max_steps=10,
            )
            fallback_performance = executor.last_run_summary["performance"]

        self.assertEqual(returned_to, 0)
        self.assertEqual(fast_state.get_register("eax"), 14)
        self.assertEqual(fast_performance["native_fast_path_invocation_count"], 1)
        self.assertEqual(
            fast_performance["native_fast_paths"],
            [
                {
                    "address": 0x1000,
                    "address_hex": "0x00001000",
                    "name": "test_add_ten",
                    "invocation_count": 1,
                }
            ],
        )
        self.assertEqual(fallback_returned_to, 0)
        self.assertEqual(fallback_state.get_register("eax"), 4)
        self.assertEqual(
            fallback_performance["native_fast_path_invocation_count"],
            0,
        )

    def test_module_functions_coalesce_into_address_partitions(self) -> None:
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
        self.assertEqual(performance["native_module_count"], 1)
        self.assertEqual(performance["native_dispatch_count"], 1)
        self.assertEqual(performance["native_module_call_count"], 1)
        self.assertEqual(
            performance["native_module_exit_profile"]["reason_counts"]["return"],
            1,
        )
        hot_targets = {
            item["target"]: item
            for item in performance["native_dispatch_hot_targets"]
        }
        self.assertEqual(set(hot_targets), {0x1000})
        self.assertEqual(hot_targets[0x1000]["module_calls"], 1)
        self.assertEqual(
            sum(item["guest_steps"] for item in hot_targets.values()),
            executor.last_run_summary["steps"],
        )
        self.assertEqual(
            executor.cache_summary["known_reachable_partition_count"],
            1,
        )
        self.assertEqual(executor.cache_summary["ahead_compiled_count"], 1)

    def test_incremental_modules_do_not_recompile_warm_base_partition(self) -> None:
        base = lift_x86_function(
            bytes.fromhex("E9FB0F0000"),  # jmp 0x2000
            base_address=0x1000,
            symbol="stable_base_module",
        )
        frontier = lift_x86_function(
            bytes.fromhex("40C3"),  # inc eax; ret
            base_address=0x2000,
            symbol="incremental_frontier_module",
        )
        merged = LiftedFunction(
            symbol="base_with_incremental_frontier",
            base_address=base.base_address,
            code_size=frontier.instructions[-1].next_address - base.base_address,
            instructions=tuple((*base.instructions, *frontier.instructions)),
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            build_dir = Path(temp_dir)
            cold_base = NativeResumableExecutor(
                base,
                build_dir=build_dir,
                module_functions=[base],
            )
            expanded = NativeResumableExecutor(
                merged,
                build_dir=build_dir,
                module_functions=[base],
                incremental_module_functions=[frontier],
                compile_worker_limit=1,
                low_priority_compilation=True,
            )
            state = CpuState.with_registers(eax=4, esp=0x8000)
            returned = expanded.run(
                state,
                SparseMemory({0x8000: 0}),
                max_steps=10,
            )
            warm = NativeResumableExecutor(
                merged,
                build_dir=build_dir,
                module_functions=[base],
                incremental_module_functions=[frontier],
            )

        self.assertEqual(cold_base.cache_summary["base_compiled_count"], 1)
        self.assertEqual(expanded.cache_summary["warm_hit_count"], 1)
        self.assertEqual(expanded.cache_summary["base_compiled_count"], 0)
        self.assertEqual(expanded.cache_summary["incremental_compiled_count"], 1)
        self.assertEqual(expanded.cache_summary["base_partition_count"], 1)
        self.assertEqual(expanded.cache_summary["incremental_partition_count"], 1)
        self.assertEqual(
            expanded.cache_summary["incremental_max_partition_instruction_count"],
            2,
        )
        self.assertEqual(
            expanded.cache_summary["incremental_compile_optimization"],
            "Od",
        )
        self.assertEqual(expanded.cache_summary["compile_worker_limit"], 1)
        self.assertTrue(expanded.cache_summary["low_priority_compilation"])
        self.assertTrue(expanded.cache_summary["executor_instance_id"])
        self.assertEqual(returned, 0)
        self.assertEqual(state.get_register("eax"), 5)
        self.assertEqual(warm.cache_summary["warm_hit_count"], 2)
        self.assertEqual(warm.cache_summary["ahead_compiled_count"], 0)

    def test_identical_emitted_source_reuses_artifact_after_emitter_metadata_change(
        self,
    ) -> None:
        function = lift_x86_function(
            bytes.fromhex("40C3"),
            base_address=0x1000,
            symbol="equivalent_emitter_source",
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            build_dir = root / "native"
            emitter_marker = root / "emitter.py"
            emitter_marker.write_text("first", encoding="utf-8")
            with patch(
                "tools.recomp.native_executor.NATIVE_EMITTER_SOURCE_FILE",
                emitter_marker,
            ):
                cold = NativeResumableExecutor(function, build_dir=build_dir)
                emitter_marker.write_text("second-version", encoding="utf-8")
                reused = NativeResumableExecutor(function, build_dir=build_dir)

        self.assertEqual(cold.cache_summary["ahead_compiled_count"], 1)
        self.assertEqual(reused.cache_summary["warm_hit_count"], 0)
        self.assertEqual(reused.cache_summary["ahead_compiled_count"], 0)
        self.assertEqual(reused.cache_summary["equivalent_source_reuse_count"], 1)
        self.assertEqual(reused.cache_summary["recovered_artifact_count"], 1)

    def test_same_module_return_stays_inside_native_partition(self) -> None:
        caller = lift_x86_function(
            bytes.fromhex("E8FB00000040C3"),  # call 0x1100; inc eax; ret
            base_address=0x1000,
            symbol="same_partition_caller",
        )
        callee = lift_x86_function(
            bytes.fromhex("40C3"),  # inc eax; ret
            base_address=0x1100,
            symbol="same_partition_callee",
        )
        merged = type(caller)(
            symbol="same_partition_return",
            base_address=caller.base_address,
            code_size=0x102,
            instructions=(*caller.instructions, *callee.instructions),
        )
        state = CpuState.with_registers(eax=3, esp=0x8000)
        memory = SparseMemory({0x8000: 0})

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                merged,
                build_dir=Path(temp_dir),
                module_functions=[caller, callee],
            )
            returned_to = executor.run(
                state,
                memory,
                max_steps=10,
                profile_hot_paths=True,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 5)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_module_count"], 1)
        self.assertEqual(performance["native_module_call_count"], 1)
        self.assertEqual(
            performance["native_module_exit_profile"]["reason_counts"]["return"],
            1,
        )
        self.assertEqual(
            performance["native_module_exit_profile"]["classified_module_calls"],
            1,
        )
        self.assertEqual(executor.last_run_summary["steps"], 5)
        self.assertEqual(
            [
                item["target"]
                for item in performance["native_dispatch_hot_targets"]
            ],
            [0x1000],
        )

    def test_cross_module_return_falls_back_to_native_dispatcher(self) -> None:
        caller = lift_x86_function(
            bytes.fromhex("E8FBFF000040C3"),  # call 0x11000; inc eax; ret
            base_address=0x1000,
            symbol="cross_partition_caller",
        )
        callee = lift_x86_function(
            bytes.fromhex("40C3"),  # inc eax; ret
            base_address=0x11000,
            symbol="cross_partition_callee",
        )
        merged = type(caller)(
            symbol="cross_partition_return",
            base_address=caller.base_address,
            code_size=0x10002,
            instructions=(*caller.instructions, *callee.instructions),
        )
        state = CpuState.with_registers(eax=3, esp=0x8000)
        memory = SparseMemory({0x8000: 0})

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            with patch(
                "tools.recomp.native_executor._partition_instructions_for_call_fusion",
                side_effect=lambda instructions, **_limits: (
                    _partition_instructions_by_address(
                        instructions,
                        maximum_count=3000,
                    )
                ),
            ):
                executor = NativeResumableExecutor(
                    merged,
                    build_dir=Path(temp_dir),
                    module_functions=[caller, callee],
                )
            returned_to = executor.run(
                state,
                memory,
                max_steps=10,
                profile_hot_paths=True,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 5)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_module_count"], 2)
        self.assertEqual(performance["native_module_call_count"], 3)
        self.assertEqual(
            performance["native_module_exit_profile"]["reason_counts"],
            {
                "branch": 0,
                "call": 1,
                "callback": 0,
                "fallthrough_or_unknown": 0,
                "fault": 0,
                "return": 2,
                "software_interrupt": 0,
                "step_budget": 0,
                "yield": 0,
            },
        )
        self.assertEqual(
            {
                item["target"]: {
                    name: count
                    for name, count in item["module_exit_reasons"].items()
                    if count
                }
                for item in performance["native_dispatch_hot_targets"]
            },
            {
                0x1000: {"call": 1},
                0x1005: {"return": 1},
                0x11000: {"return": 1},
            },
        )
        self.assertEqual(executor.last_run_summary["steps"], 5)
        self.assertEqual(
            {
                item["target"]
                for item in performance["native_dispatch_hot_targets"]
            },
            {0x1000, 0x1005, 0x11000},
        )
        edge_profile = performance["native_module_edge_profile"]
        self.assertTrue(edge_profile["exact"])
        self.assertEqual(edge_profile["unique_edge_count"], 3)
        self.assertEqual(edge_profile["reported_edge_count"], 3)
        self.assertEqual(edge_profile["dropped_edge_count"], 0)
        self.assertEqual(edge_profile["report_limit"], NATIVE_DISPATCH_EDGE_REPORT_LIMIT)
        self.assertLessEqual(
            edge_profile["table_capacity"],
            NATIVE_DISPATCH_EDGE_CAPACITY_LIMIT,
        )
        self.assertEqual(edge_profile["classified_module_calls"], 3)
        self.assertEqual(edge_profile["unclassified_module_calls"], 0)
        self.assertEqual(edge_profile["overflow_module_calls"], 0)
        self.assertEqual(
            {
                (item["entry_target"], item["exit_target"]): {
                    name: count
                    for name, count in item["module_exit_reasons"].items()
                    if count
                }
                for item in edge_profile["edges"]
            },
            {
                (0x1000, 0x11000): {"call": 1},
                (0x11000, 0x1005): {"return": 1},
                (0x1005, 0): {"return": 1},
            },
        )
        self.assertIn("native_dispatch_self", performance["timings"])
        self.assertLessEqual(
            performance["timings"]["native_dispatch_self"]["total_us"],
            performance["timings"]["native_dispatch"]["total_us"],
        )

    def test_cross_module_jump_is_classified_as_branch(self) -> None:
        first = lift_x86_function(
            bytes.fromhex("E9FBFF0000"),  # jmp 0x11000
            base_address=0x1000,
            symbol="cross_partition_jump",
        )
        second = lift_x86_function(
            bytes.fromhex("40C3"),  # inc eax; ret
            base_address=0x11000,
            symbol="cross_partition_jump_target",
        )
        merged = type(first)(
            symbol="cross_partition_jump_merged",
            base_address=first.base_address,
            code_size=0x10002,
            instructions=(*first.instructions, *second.instructions),
        )
        state = CpuState.with_registers(eax=3, esp=0x8000)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            with patch(
                "tools.recomp.native_executor._partition_instructions_for_call_fusion",
                side_effect=lambda instructions, **_limits: (
                    _partition_instructions_by_address(
                        instructions,
                        maximum_count=3000,
                    )
                ),
            ):
                executor = NativeResumableExecutor(
                    merged,
                    build_dir=Path(temp_dir),
                    module_functions=[first, second],
                )
            returned_to = executor.run(
                state,
                SparseMemory({0x8000: 0}),
                max_steps=10,
                profile_hot_paths=True,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 4)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_module_call_count"], 2)
        self.assertEqual(
            {
                name: count
                for name, count in performance["native_module_exit_profile"][
                    "reason_counts"
                ].items()
                if count
            },
            {"branch": 1, "return": 1},
        )
        self.assertEqual(executor.last_run_summary["steps"], 3)

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
        self.assertEqual(executor.current_run_metrics["invalidated_page_count"], 1)

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

    def test_call_fusion_co_locates_cross_partition_callee(self) -> None:
        class Instruction:
            def __init__(
                self,
                address: int,
                mnemonic: str = "nop",
                target: int | None = None,
            ) -> None:
                self.address = address
                self.mnemonic = mnemonic
                self.target = target

        caller = Instruction(0x1000, "call", 0x21000)
        callee = Instruction(0x21000, "ret")
        unrelated = Instruction(0x41000)

        modules = _partition_instructions_for_call_fusion(
            [caller, callee, unrelated],
            base_maximum_count=1,
            maximum_count=2,
        )

        self.assertEqual(
            [[item.address for item in module] for module in modules],
            [[0x1000, 0x21000], [0x41000]],
        )

    def test_call_fusion_keeps_native_fast_path_owner_small(self) -> None:
        class Instruction:
            def __init__(
                self,
                address: int,
                mnemonic: str = "nop",
                target: int | None = None,
            ) -> None:
                self.address = address
                self.mnemonic = mnemonic
                self.target = target

        caller = Instruction(0x1000, "call", 0x21000)
        fast_path_owner = Instruction(0x21000, "ret")
        unrelated = Instruction(0x41000)

        modules = _partition_instructions_for_call_fusion(
            [caller, fast_path_owner, unrelated],
            base_maximum_count=1,
            maximum_count=2,
            isolated_addresses={fast_path_owner.address},
        )

        self.assertEqual(
            [[item.address for item in module] for module in modules],
            [[0x1000], [0x21000], [0x41000]],
        )

    def test_call_fused_native_module_executes_cross_partition_return(self) -> None:
        call_displacement = 0x21000 - 0x1005
        caller = lift_x86_function(
            b"\xE8" + call_displacement.to_bytes(4, "little", signed=True) + b"\xC3",
            base_address=0x1000,
            symbol="call_fused_caller",
        )
        callee = lift_x86_function(
            bytes.fromhex("B82A000000C3"),
            base_address=0x21000,
            symbol="call_fused_callee",
        )
        function = LiftedFunction(
            symbol="call_fused_frame",
            base_address=caller.base_address,
            code_size=callee.instructions[-1].next_address - caller.base_address,
            instructions=tuple((*caller.instructions, *callee.instructions)),
        )
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory({0x8000: 0})

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(function, build_dir=Path(temp_dir))
            returned = executor.run(state, memory, max_steps=20)

        self.assertEqual(returned, 0)
        self.assertEqual(state.get_register("eax"), 42)
        self.assertEqual(
            executor.cache_summary["known_reachable_partition_count"],
            1,
        )
        self.assertEqual(
            executor.last_run_summary["performance"]["native_module_call_count"],
            1,
        )

    def test_native_module_manifest_skips_cpp_emission_on_warm_hit(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("40C3"),
            base_address=0x1000,
            symbol="manifest_warm_hit",
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            build_dir = Path(temp_dir)
            cold = NativeResumableExecutor(function, build_dir=build_dir)
            with patch(
                "tools.recomp.native_executor.emit_cpp",
                side_effect=AssertionError("warm cache regenerated C++"),
            ):
                warm = NativeResumableExecutor(function, build_dir=build_dir)
            (build_dir / "native-module-manifest.sqlite3").unlink()
            with patch(
                "tools.recomp.native_executor.emit_cpp",
                side_effect=AssertionError("orphaned artifact regenerated C++"),
            ):
                recovered = NativeResumableExecutor(function, build_dir=build_dir)
            generated_import_libraries = list(
                build_dir.glob("native-loop-*.lib")
            )

        self.assertEqual(cold.cache_summary["ahead_compiled_count"], 1)
        self.assertEqual(warm.cache_summary["warm_hit_count"], 1)
        self.assertEqual(warm.cache_summary["ahead_compiled_count"], 0)
        self.assertEqual(recovered.cache_summary["recovered_artifact_count"], 1)
        self.assertEqual(recovered.cache_summary["ahead_compiled_count"], 0)
        self.assertEqual(generated_import_libraries, [])

    def test_native_fast_path_change_invalidates_only_owning_partition(self) -> None:
        first = lift_x86_function(
            bytes.fromhex("C3"),
            base_address=0x1000,
            symbol="localized_fast_path_first",
        )
        second = lift_x86_function(
            bytes.fromhex("C3"),
            base_address=0x2000,
            symbol="localized_fast_path_second",
        )
        function = LiftedFunction(
            symbol="localized_fast_path_frame",
            base_address=first.base_address,
            code_size=second.instructions[-1].next_address - first.base_address,
            instructions=tuple((*first.instructions, *second.instructions)),
        )
        initial_fast_path = NativeFastPath(
            name="localized_initial",
            guard="true",
            body=("ctx->eax = 1u;",),
        )
        changed_fast_path = NativeFastPath(
            name="localized_changed",
            guard="true",
            body=("ctx->eax = 2u;",),
        )

        with (
            tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir,
            patch.object(NativeResumableExecutor, "BASE_INSTRUCTIONS_PER_MODULE", 1),
            patch.object(NativeResumableExecutor, "MAX_INSTRUCTIONS_PER_MODULE", 1),
        ):
            build_dir = Path(temp_dir)
            cold = NativeResumableExecutor(
                function,
                build_dir=build_dir,
                native_fast_paths={first.base_address: initial_fast_path},
            )
            changed = NativeResumableExecutor(
                function,
                build_dir=build_dir,
                native_fast_paths={first.base_address: changed_fast_path},
            )

        self.assertEqual(cold.cache_summary["known_reachable_partition_count"], 2)
        self.assertEqual(cold.cache_summary["ahead_compiled_count"], 2)
        self.assertEqual(changed.cache_summary["warm_hit_count"], 1)
        self.assertEqual(changed.cache_summary["ahead_compiled_count"], 1)
        self.assertTrue(changed.cache_summary["localized_fast_path_configuration"])

    def test_native_module_manifest_prunes_old_artifacts(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            build_dir = Path(temp_dir)
            manifest = NativeModuleManifest(build_dir, max_artifacts=1)
            first_source = build_dir / "first.cpp"
            first_artifact = build_dir / "first.dll"
            second_source = build_dir / "second.cpp"
            second_artifact = build_dir / "second.dll"
            orphan_import_library = build_dir / "native-loop-legacy.lib"
            for path in (
                first_source,
                first_artifact,
                second_source,
                second_artifact,
                orphan_import_library,
            ):
                path.write_bytes(path.name.encode("ascii"))
            pe_stub = bytearray(256)
            pe_stub[:2] = b"MZ"
            pe_stub[60:64] = (64).to_bytes(4, "little")
            pe_stub[64:68] = b"PE\0\0"
            first_artifact.write_bytes(pe_stub)
            second_artifact.write_bytes(pe_stub)
            manifest.record(
                configuration_key="config",
                partition_start=0x1000,
                partition_end=0x1001,
                content_digest="first",
                source_path=first_source,
                artifact_path=first_artifact,
            )
            manifest.record(
                configuration_key="config",
                partition_start=0x2000,
                partition_end=0x2001,
                content_digest="second",
                source_path=second_source,
                artifact_path=second_artifact,
            )

            manifest.ORPHAN_GRACE_NS = 0
            manifest.prune()
            summary = manifest.summary()
            manifest.close()

            self.assertEqual(summary["artifact_count"], 1)
            self.assertEqual(summary["pruned_artifacts"], 1)
            self.assertFalse(first_artifact.exists())
            self.assertFalse(first_source.exists())
            self.assertTrue(second_artifact.exists())
            self.assertFalse(orphan_import_library.exists())
            self.assertEqual(summary["pruned_orphan_files"], 1)

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
        for timing_name in (
            "dirty_page_writeback_loop",
            "dirty_page_payload_materialize",
            "dirty_page_memory_commit",
        ):
            self.assertIn(timing_name, performance["timings"])
            self.assertGreaterEqual(
                performance["timings"][timing_name]["count"],
                1,
            )

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
        performance = executor.last_run_summary["performance"]
        self.assertEqual(
            performance["native_module_exit_profile"]["reason_counts"][
                "callback"
            ],
            1,
        )

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

    def test_split_callbacks_keep_nonzero_fallback_words_in_native_cache(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "A104300000"  # mov eax, [0x3004]
                "C7050430000005000000"  # mov dword ptr [0x3004], 5
                "8B1D04300000"  # mov ebx, [0x3004]
                "C7050430000000000000"  # mov dword ptr [0x3004], 0
                "8B0D04300000"  # mov ecx, [0x3004]
                "C3"
            ),
            base_address=0x1000,
            symbol="split_zero_guarded_memory_callbacks",
        )

        class ZeroFallbackMemory(SparseMemory):
            native_memory_callback_policy_static = True

            def __init__(self, initial) -> None:
                self.write_u32_call_count = 0
                super().__init__(initial)
                self.write_u32_call_count = 0

            def native_memory_callback_dependency_addresses(
                self,
                address: int,
                size: int,
                *,
                is_write: bool,
            ) -> tuple[int, ...] | None:
                if not is_write and address == 0x3004 and size == 4:
                    return (address,)
                return None

            def read_u32(self, address: int) -> int:
                value = super().read_u32(address)
                if address == 0x3004 and value == 0:
                    super().write_u32(address, 11)
                    return 11
                return value

            def write_u32(self, address: int, value: int) -> None:
                self.write_u32_call_count += 1
                super().write_u32(address, value)

        memory = ZeroFallbackMemory({0x3004: 9, 0x8000: 0})
        state = CpuState.with_registers(esp=0x8000)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                memory_read_callback_addresses={0x3004},
                memory_zero_read_callback_addresses={0x3004},
            )
            returned_to = executor.run(state, memory, max_steps=20)

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 9)
        self.assertEqual(state.get_register("ebx"), 5)
        self.assertEqual(state.get_register("ecx"), 11)
        self.assertEqual(memory.read_u32(0x3004), 11)
        self.assertEqual(memory.write_u32_call_count, 0)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["exact_read_u32_callback_count"], 2)
        self.assertEqual(performance["write_u32_callback_count"], 0)
        self.assertEqual(performance["zero_read_callback_bypass_count"], 1)
        self.assertEqual(
            performance["selective_dirty_sync_page_writeback_count"],
            1,
        )

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
                dispatch_host_calls_in_native=True,
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
        packed_batches: list[tuple[bytes, int, bool, int]] = []
        packed_texture_batches: list[tuple[bytes, list[int], int]] = []

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
            observed_range_end,
            packed_texture_payload,
            packed_texture_runs,
            packed_texture_run_count,
        ) -> None:
            packed_batches.append(
                (
                    bytes(packed_records[: count * 16]),
                    observed_range_start,
                    contiguous_u32,
                    observed_range_end,
                )
            )
            packed_texture_batches.append(
                (
                    bytes(packed_texture_payload[: count * 4]),
                    [
                        int(packed_texture_runs[index])
                        for index in range(packed_texture_run_count * 4)
                    ],
                    packed_texture_run_count,
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
            no_provenance_batches: list[tuple[list[int], bytes]] = []

            def observe_without_provenance(
                packed_records,
                addresses,
                _values,
                _sizes,
                _eips,
                _source_addresses,
                _steps,
                count,
                *_batch_metadata,
            ) -> None:
                no_provenance_batches.append(
                    (
                        [int(addresses[index]) for index in range(count)],
                        bytes(packed_records[: count * 16]),
                    )
                )

            second_state = CpuState.with_registers(esp=0x8000)
            second_memory = SparseMemory({0x8000: 0})
            self.assertEqual(
                executor.run(
                    second_state,
                    second_memory,
                    memory_write_batch_observer=observe_without_provenance,
                    memory_write_batch_address_base=0x80000000,
                    memory_write_observer_ranges_provider=lambda: ((0x3000, 0x4000),),
                    capture_observed_write_provenance=False,
                    max_steps=10,
                ),
                0,
            )
            no_provenance_performance = executor.last_run_summary["performance"]

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
        self.assertEqual(no_provenance_batches[0][0], [0x3000, 0x3004])
        self.assertEqual(len(no_provenance_batches[0][1]), 32)
        self.assertFalse(
            no_provenance_performance["observed_write_provenance_enabled"]
        )
        packed, range_start, contiguous, range_end = packed_batches[0]
        self.assertEqual(range_start, 0x3000)
        self.assertEqual(range_end, 0x4000)
        self.assertTrue(contiguous)
        self.assertEqual(
            [
                struct.unpack_from("<BBHI8s", packed, offset)[3]
                for offset in (0, 16)
            ],
            [0x80000000, 0x80000004],
        )
        texture_payload, texture_runs, texture_run_count = packed_texture_batches[0]
        self.assertEqual(texture_payload[:8], bytes.fromhex("1111111122222222"))
        self.assertEqual(texture_runs, [0x80000000, 0, 8, 0])
        self.assertEqual(texture_run_count, 1)

    def test_direct_observed_write_transport_skips_backing_ram(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "C7050030000011111111"
                "C7050430000022222222"
                "C3"
            ),
            base_address=0x1000,
            symbol="direct_observed_native_writes",
        )
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory(
            {
                0x3000: 0xAAAAAAAA,
                0x3004: 0xBBBBBBBB,
                0x8000: 0,
            }
        )
        spans: list[tuple[bytes, list[tuple[int, int, int, int, int]]]] = []

        def observe_spans(
            payload,
            payload_size,
            addresses,
            payload_offsets,
            payload_sizes,
            write_counts,
            flags,
            span_count,
            *_range_metadata,
        ) -> None:
            spans.append(
                (
                    bytes(payload[:payload_size]),
                    [
                        (
                            int(addresses[index]),
                            int(payload_offsets[index]),
                            int(payload_sizes[index]),
                            int(write_counts[index]),
                            int(flags[index]),
                        )
                        for index in range(span_count)
                    ],
                )
            )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
            )
            returned_to = executor.run(
                state,
                memory,
                memory_write_span_observer=observe_spans,
                memory_write_batch_address_base=0x80000000,
                memory_write_observer_ranges_provider=lambda: ((0x3000, 0x4000),),
                capture_observed_write_provenance=False,
                direct_observed_write_transport=True,
                max_steps=10,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(memory.read_u32(0x3000), 0xAAAAAAAA)
        self.assertEqual(memory.read_u32(0x3004), 0xBBBBBBBB)
        self.assertEqual(
            spans,
            [(
                bytes.fromhex("1111111122222222"),
                [(0x3000, 0, 8, 2, 0)],
            )],
        )
        performance = executor.last_run_summary["performance"]
        self.assertTrue(performance["direct_observed_write_transport"])
        self.assertEqual(performance["direct_observed_write_count"], 2)
        self.assertEqual(performance["direct_observed_write_byte_count"], 8)
        self.assertEqual(performance["dirty_byte_writeback_count"], 0)

    def test_native_write_packer_compacts_general_texture_runs(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "A100300000"
                "C7050030000011111111"
                "C6050430000022"
                "C7051030000033333333"
                "C3"
            ),
            base_address=0x1000,
            symbol="general_observed_native_writes",
        )
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory({0x8000: 0})
        packed_batches: list[tuple[bool, bytes, list[int], int]] = []

        def observe_batch(
            _packed_records,
            _addresses,
            _values,
            _sizes,
            _eips,
            _source_addresses,
            _steps,
            count,
            _observed_range_start,
            contiguous_u32,
            _observed_range_end,
            packed_texture_payload,
            packed_texture_runs,
            packed_texture_run_count,
        ) -> None:
            packed_batches.append(
                (
                    contiguous_u32,
                    bytes(packed_texture_payload[: count * 4]),
                    [
                        int(packed_texture_runs[index])
                        for index in range(packed_texture_run_count * 4)
                    ],
                    packed_texture_run_count,
                )
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
        self.assertEqual(len(packed_batches), 1)
        contiguous, payload, runs, run_count = packed_batches[0]
        self.assertFalse(contiguous)
        self.assertEqual(payload[:9], bytes.fromhex("111111112233333333"))
        self.assertEqual(
            runs,
            [
                0x80000000,
                0,
                5,
                0,
                0x80000010,
                5,
                4,
                2,
            ],
        )
        self.assertEqual(run_count, 2)

    def test_observer_independent_exact_read_defers_native_write_log(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "A100300000"  # mov eax, [0x3000]
                "C7050030000034120000"  # mov dword ptr [0x3000], 0x1234
                "8B1D04400000"  # mov ebx, [0x4004]
                "C7050430000078560000"  # mov dword ptr [0x3004], 0x5678
                "8B0D04400000"  # mov ecx, [0x4004]
                "C3"
            ),
            base_address=0x1000,
            symbol="deferred_observer_drain",
        )

        class ObserverIndependentMemory(SparseMemory):
            native_memory_callback_policy_static = True

            def __init__(self, initial) -> None:
                super().__init__(initial)
                self.dependency_query_count = 0
                self.observer_drain_query_count = 0

            def native_memory_callback_dependency_addresses(
                self,
                address: int,
                size: int,
                *,
                is_write: bool,
            ) -> tuple[int, ...] | None:
                self.dependency_query_count += 1
                if not is_write and address == 0x4004 and size == 4:
                    return ()
                return None

            def native_memory_callback_requires_observer_drain(
                self,
                address: int,
                size: int,
                *,
                is_write: bool,
            ) -> bool:
                self.observer_drain_query_count += 1
                return is_write or address != 0x4004 or size != 4

        memory = ObserverIndependentMemory({0x3000: 1, 0x4004: 9, 0x8000: 0})
        state = CpuState.with_registers(esp=0x8000)
        batches: list[list[tuple[int, int]]] = []

        def observe_batch(
            _packed_records,
            addresses,
            values,
            _sizes,
            _eips,
            _source_addresses,
            _steps,
            count,
            _observed_range_start,
            _contiguous_u32,
            _observed_range_end,
            _packed_texture_payload,
            _packed_texture_runs,
            _packed_texture_run_count,
        ) -> None:
            batches.append(
                [(int(addresses[index]), int(values[index])) for index in range(count)]
            )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                memory_callback_addresses={0x4004},
            )
            returned_to = executor.run(
                state,
                memory,
                memory_write_batch_observer=observe_batch,
                memory_write_observer_ranges_provider=lambda: ((0x3000, 0x4000),),
                max_steps=10,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("ebx"), 9)
        self.assertEqual(state.get_register("ecx"), 9)
        self.assertEqual(memory.read_u32(0x3000), 0x1234)
        self.assertEqual(memory.read_u32(0x3004), 0x5678)
        self.assertEqual(batches, [[(0x3000, 0x1234), (0x3004, 0x5678)]])
        self.assertEqual(memory.dependency_query_count, 1)
        self.assertEqual(memory.observer_drain_query_count, 1)
        performance = executor.last_run_summary["performance"]
        self.assertGreater(performance["observer_drain_deferred_count"], 0)
        self.assertGreater(performance["empty_dependency_sync_bypass_count"], 0)
        self.assertEqual(performance["memory_callback_policy_cache_miss_count"], 2)
        self.assertEqual(performance["memory_callback_policy_cache_hit_count"], 2)
        self.assertEqual(performance["native_observed_write_batch_count"], 1)

    def test_target_profiling_can_be_disabled_without_changing_execution(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("40C3"),
            base_address=0x1000,
            symbol="unprofiled_native_loop",
        )
        state = CpuState.with_registers(eax=4, esp=0x8000)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(function, build_dir=Path(temp_dir))
            returned_to = executor.run(
                state,
                SparseMemory({0x8000: 0}),
                max_steps=10,
                profile_hot_paths=False,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 5)
        performance = executor.last_run_summary["performance"]
        self.assertFalse(performance["hot_path_profiling_enabled"])
        self.assertEqual(performance["native_module_call_count"], 1)
        self.assertEqual(performance["native_dispatch_hot_targets"], [])
        self.assertFalse(performance["native_module_edge_profile"]["enabled"])
        self.assertFalse(performance["native_module_edge_profile"]["exact"])
        self.assertEqual(performance["native_module_edge_profile"]["edges"], [])
        self.assertNotIn("native_dispatch_self", performance["timings"])

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
        self.assertEqual(
            performance["native_module_exit_profile"]["reason_counts"][
                "step_budget"
            ],
            4,
        )
        self.assertGreaterEqual(performance["dirty_sync_call_count"], 4)
        self.assertIn("native_dispatch", performance["timings"])
        self.assertIn("yield_handler", performance["timings"])
        self.assertEqual(
            performance["hot_paths"][0]["total_us"],
            max(metric["total_us"] for metric in performance["timings"].values()),
        )

    def test_live_slice_provider_expands_native_dispatch_quantum(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("40EBFD"),  # inc eax; jmp 0x1000
            base_address=0x1000,
            symbol="adaptive_sliced_loop",
        )
        state = CpuState.with_registers(eax=0, esp=0x8000)
        yields: list[int] = []
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(function, build_dir=Path(temp_dir))
            executor.run(
                state,
                SparseMemory(),
                max_steps=200,
                slice_steps=25,
                slice_steps_provider=lambda steps: 25 if steps < 25 else 75,
                yield_handler=lambda _state, _memory, steps: yields.append(steps),
            )

        self.assertEqual(yields, [25, 100, 175])
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["initial_slice_steps"], 25)
        self.assertEqual(performance["final_slice_steps"], 75)
        self.assertEqual(performance["slice_quantum_change_count"], 1)

    def test_native_dispatch_keeps_multiple_host_calls_in_one_session(self) -> None:
        base_address = 0x1000
        handler_target = 0x2000
        code = bytearray()
        for _ in range(2):
            instruction_address = base_address + len(code)
            code.extend(b"\xE8")
            code.extend(
                struct.pack(
                    "<i",
                    handler_target - (instruction_address + 5),
                )
            )
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_host_call_session",
        )
        calls: list[int] = []

        def handler(
            state: CpuState,
            _memory: SparseMemory,
            target: int,
            _trace: ExecutionTrace,
        ) -> None:
            calls.append(target)
            state.set_register("eax", state.get_register("eax") + 1)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                callback_addresses={handler_target},
            )
            state = CpuState.with_registers(eax=0, esp=0x8000)
            returned_to = executor.run(
                state,
                SparseMemory({0x8000: 0}),
                call_handlers={handler_target: handler},
                dispatch_host_calls_in_native=True,
                max_steps=16,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(calls, [handler_target, handler_target])
        self.assertEqual(state.get_register("eax"), 2)
        performance = executor.last_run_summary["performance"]
        self.assertTrue(performance["native_host_call_dispatch_enabled"])
        self.assertEqual(performance["native_host_call_dispatch_count"], 2)
        self.assertEqual(performance["native_dispatch_count"], 1)

    def test_native_host_service_bypasses_python_handler(self) -> None:
        base_address = 0x1000
        handler_target = 0x2000
        relative = handler_target - (base_address + 5)
        function = lift_x86_function(
            b"\xE8" + struct.pack("<i", relative) + b"\xC3",
            base_address=base_address,
            symbol="native_constant_host_service",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    handler_target,
                    NATIVE_HOST_SERVICE_RETURN_CONSTANT,
                    0,
                    0x12345678,
                )
            ]
        )

        def unexpected_handler(*_args: object) -> None:
            self.fail("native host service crossed into Python")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                callback_addresses={handler_target},
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                SparseMemory({0x8000: 0}),
                call_handlers={handler_target: unexpected_handler},
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=8,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 0x12345678)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_host_service_call_count"], 1)
        self.assertEqual(performance["handler_call_count"], 0)

    def test_native_cold_service_owns_unwind_and_registers_worker(self) -> None:
        base_address = 0x1000
        handler_target = 0x2000
        handle_address = 0x3000
        arguments = (
            handle_address,
            0x20,
            0x4000,
            0x80,
            0x3010,
            0xAABBCCDD,
            0x11223344,
            0,
            0,
            0x5000,
        )
        code = bytearray()
        for argument in reversed(arguments):
            code.extend(b"\x68" + struct.pack("<I", argument))
        call_address = base_address + len(code)
        code.extend(
            b"\xE8" + struct.pack("<i", handler_target - (call_address + 5))
        )
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_cold_worker_create",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    handler_target,
                    NATIVE_HOST_SERVICE_COLD_CALLBACK,
                    40,
                    NATIVE_HOST_SERVICE_LIFECYCLE_CREATE_WORKER,
                )
            ]
        )
        lifecycle = NativeWorkerLifecycleState()
        services.attach_worker_lifecycle(lifecycle)
        memory = SparseMemory({0x8000: 0})
        observed_arguments: list[tuple[int, ...]] = []

        def create_worker_handler(
            state: CpuState,
            active_memory: SparseMemory,
            _target: int,
            _trace: ExecutionTrace,
        ) -> None:
            esp = state.get_register("esp")
            observed_arguments.append(
                tuple(active_memory.read_u32(esp + 4 + index * 4) for index in range(10))
            )
            return_address = active_memory.read_u32(esp)
            active_memory.write_u32(handle_address, 0x104)
            active_memory.write_u32(esp + 40, return_address)
            state.set_register("esp", esp + 40)
            state.set_register("eax", 0)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                callback_addresses={handler_target},
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                call_handlers={handler_target: create_worker_handler},
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=32,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(observed_arguments, [arguments])
        self.assertEqual(state.get_register("esp"), 0x8004)
        worker = lifecycle.entry_for_handle(0x104)
        self.assertIsNotNone(worker)
        assert worker is not None
        self.assertEqual(worker.start_address, 0x5000)
        self.assertEqual(worker.start_context1, 0xAABBCCDD)
        self.assertEqual(worker.start_context2, 0x11223344)
        self.assertEqual(worker.status, NATIVE_WORKER_READY)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_cold_host_call_count"], 1)
        self.assertEqual(performance["native_host_service_call_count"], 1)
        self.assertEqual(performance["native_dispatch_count"], 1)

    def test_native_worker_lifecycle_selects_and_transitions_workers(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("C3"),
            base_address=0x1000,
            symbol="native_worker_lifecycle",
        )
        workers = [
            {
                "handle": 0x104,
                "start_address": 0x2000,
                "start_context1": 1,
                "start_context2": 2,
                "suspended": False,
            },
            {
                "handle": 0x108,
                "start_address": 0x3000,
                "start_context1": 3,
                "start_context2": 4,
                "suspended": True,
            },
        ]
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(function, build_dir=Path(temp_dir))
            lifecycle = NativeWorkerLifecycleState()
            executor.synchronize_worker_lifecycle(
                lifecycle,
                workers,
                primary_handle=0x100,
            )
            first = executor.select_runnable_workers(lifecycle)
            executor.set_worker_lifecycle_status(
                lifecycle, 0x104, NATIVE_WORKER_COMPLETED
            )
            executor.set_worker_lifecycle_status(
                lifecycle, 0x108, NATIVE_WORKER_READY
            )
            second = executor.select_runnable_workers(lifecycle)

        self.assertEqual(first, (0x104,))
        self.assertEqual(second, (0x108,))
        self.assertEqual(
            lifecycle.entry_for_handle(0x104).status,
            NATIVE_WORKER_COMPLETED,
        )
        self.assertEqual(
            lifecycle.entry_for_handle(0x108).status,
            NATIVE_WORKER_RUNNING,
        )
        self.assertEqual(lifecycle.entry_count, 2)
        self.assertEqual(lifecycle.created_count, 2)
        self.assertGreaterEqual(lifecycle.transition_count, 5)

    def test_cooperative_scheduler_policy_runs_in_native_dispatcher(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("C3"),
            base_address=0x1000,
            symbol="native_cooperative_scheduler",
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(function, build_dir=Path(temp_dir))
            state = NativeCooperativeSchedulerState()
            first = executor.update_cooperative_scheduler(
                state,
                steps=250,
                completed_flips=0,
                instruction_quantum=100,
            )
            video = executor.update_cooperative_scheduler(
                state,
                steps=275,
                completed_flips=3,
                instruction_quantum=100,
            )
            resumed = executor.update_cooperative_scheduler(
                state,
                steps=425,
                completed_flips=3,
                instruction_quantum=100,
            )
            forced_after_video = executor.update_cooperative_scheduler(
                state,
                steps=450,
                completed_flips=3,
                instruction_quantum=100,
                force_instruction_tick=True,
            )
            forced_state = NativeCooperativeSchedulerState()
            forced_before_video = executor.update_cooperative_scheduler(
                forced_state,
                steps=25,
                completed_flips=0,
                instruction_quantum=100,
                force_instruction_tick=True,
            )
            retained_cadence = executor.update_cooperative_scheduler(
                forced_state,
                steps=100,
                completed_flips=0,
                instruction_quantum=100,
            )

        self.assertEqual(first, (2, 0))
        self.assertEqual(video, (0, 3))
        self.assertEqual(resumed, (0, 0))
        self.assertEqual(forced_after_video, (1, 0))
        self.assertEqual(forced_before_video, (1, 0))
        self.assertEqual(retained_cadence, (1, 0))

    def test_native_system_time_service_writes_guest_memory(self) -> None:
        base_address = 0x1000
        handler_target = 0x2000
        output_address = 0x3000
        call_address = base_address + 5
        function = lift_x86_function(
            b"\x68" + struct.pack("<I", output_address)
            + b"\xE8" + struct.pack("<i", handler_target - (call_address + 5))
            + b"\xC3",
            base_address=base_address,
            symbol="native_system_time_service",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    handler_target,
                    NATIVE_HOST_SERVICE_SYSTEM_TIME,
                    4,
                    0,
                )
            ]
        )
        services.system_time_filetime = 0x1122334455667788
        memory = SparseMemory({0x8000: 0})

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                callback_addresses={handler_target},
            )
            state = CpuState.with_registers(eax=0xAABBCCDD, esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                call_handlers={handler_target: lambda *_args: None},
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=8,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 0xAABBCCDD)
        self.assertEqual(
            int.from_bytes(memory.read(output_address, 8), "little"),
            0x1122334455667788,
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

    def test_observed_flip_packet_yields_after_data_write(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "A100300000"  # mov eax, [0x3000]; populate the native page cache
                "C705FC3F00002C010400"  # mov [0x3ffc], 0x0004012c
                "C7050030000007000000"  # mov [0x3000], 7; wrapped packet data
                "40"  # inc eax
                "C3"
            ),
            base_address=0x1000,
            symbol="observed_flip_packet_yield",
        )
        memory = SparseMemory({0x3000: 0, 0x3FFC: 0, 0x8000: 0})
        state = CpuState.with_registers(esp=0x8000)
        batches: list[list[tuple[int, int]]] = []
        yields: list[tuple[int, int, int, int, int]] = []

        def observe_batch(
            _packed_records,
            addresses,
            values,
            _sizes,
            _eips,
            _source_addresses,
            _steps,
            count,
            _observed_range_start,
            _contiguous_u32,
            _observed_range_end,
            _packed_texture_payload,
            _packed_texture_runs,
            _packed_texture_run_count,
        ) -> None:
            batches.append(
                [(int(addresses[index]), int(values[index])) for index in range(count)]
            )

        def handle_yield(
            yielded_state: CpuState,
            yielded_memory: SparseMemory,
            steps: int,
        ) -> None:
            yields.append(
                (
                    steps,
                    yielded_state.eip,
                    yielded_state.get_register("eax"),
                    yielded_memory.read_u32(0x3FFC),
                    yielded_memory.read_u32(0x3000),
                )
            )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(function, build_dir=Path(temp_dir))
            returned_to = executor.run(
                state,
                memory,
                memory_write_batch_observer=observe_batch,
                memory_write_observer_yield_header=0x0004012C,
                memory_write_observer_ranges_provider=lambda: ((0x3000, 0x4000),),
                max_steps=20,
                yield_handler=handle_yield,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(
            batches,
            [[(0x3FFC, 0x0004012C), (0x3000, 7)]],
        )
        self.assertEqual(yields, [(3, 0x1019, 0, 0x0004012C, 7)])
        self.assertEqual(state.get_register("eax"), 1)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["observed_write_packet_yield_count"], 1)
        self.assertEqual(performance["predicate_yield_count"], 1)
        self.assertEqual(
            performance["native_module_exit_profile"]["reason_counts"]["yield"],
            1,
        )

    def test_direct_observed_flip_seals_exact_span_boundary(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "A100300000"
                "C705FC3F00002C010400"
                "C7050030000007000000"
                "40"
                "C3"
            ),
            base_address=0x1000,
            symbol="direct_observed_flip_span_boundary",
        )
        memory = SparseMemory({0x3000: 0, 0x3FFC: 0, 0x8000: 0})
        state = CpuState.with_registers(esp=0x8000)
        spans: list[tuple[bytes, list[tuple[int, int, int, int, int]]]] = []
        yields: list[tuple[int, int, int]] = []

        def observe_spans(
            payload,
            payload_size,
            addresses,
            payload_offsets,
            payload_sizes,
            write_counts,
            flags,
            span_count,
            *_range_metadata,
        ) -> None:
            spans.append(
                (
                    bytes(payload[:payload_size]),
                    [
                        (
                            int(addresses[index]),
                            int(payload_offsets[index]),
                            int(payload_sizes[index]),
                            int(write_counts[index]),
                            int(flags[index]),
                        )
                        for index in range(span_count)
                    ],
                )
            )

        def handle_yield(
            _yielded_state: CpuState,
            yielded_memory: SparseMemory,
            steps: int,
        ) -> None:
            yields.append(
                (
                    steps,
                    yielded_memory.read_u32(0x3FFC),
                    yielded_memory.read_u32(0x3000),
                )
            )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(function, build_dir=Path(temp_dir))
            returned_to = executor.run(
                state,
                memory,
                memory_write_span_observer=observe_spans,
                memory_write_observer_yield_header=0x0004012C,
                memory_write_observer_ranges_provider=lambda: ((0x3000, 0x4000),),
                direct_observed_write_transport=True,
                max_steps=20,
                yield_handler=handle_yield,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(
            spans,
            [
                (
                    bytes.fromhex("2C01040007000000"),
                    [
                        (0x3FFC, 0, 4, 1, 0),
                        (0x3000, 4, 4, 1, 1),
                    ],
                )
            ],
        )
        self.assertEqual(yields, [(3, 0, 0)])
        self.assertEqual(state.get_register("eax"), 1)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_observed_write_count"], 2)
        self.assertEqual(performance["native_observed_write_span_count"], 2)
        self.assertEqual(performance["native_observed_write_span_byte_count"], 8)
        self.assertEqual(performance["observed_write_packet_yield_count"], 1)

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
