from __future__ import annotations

import ctypes
import hashlib
import hmac
import math
import os
import struct
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from tools.playability import live_transport
from tools.recomp.native_executor import (
    NATIVE_HOST_SERVICE_AUDIO,
    NATIVE_HOST_SERVICE_AUDIO_BUFFER_CREATE,
    NATIVE_HOST_SERVICE_AUDIO_BUFFER_PLAY,
    NATIVE_HOST_SERVICE_AUDIO_BUFFER_SET_DATA,
    NATIVE_HOST_SERVICE_AUDIO_BUFFER_SET_FORMAT,
    NATIVE_HOST_SERVICE_AUDIO_BUFFER_SET_FREQUENCY,
    NATIVE_HOST_SERVICE_AUDIO_BUFFER_GET_POSITION,
    NATIVE_HOST_SERVICE_AUDIO_BUFFER_GET_STATUS,
    NATIVE_HOST_SERVICE_AUDIO_BUFFER_SET_POSITION,
    NATIVE_HOST_SERVICE_AUDIO_BUFFER_SET_VOLUME,
    NATIVE_HOST_SERVICE_AUDIO_BUFFER_STOP_EX,
    NATIVE_HOST_SERVICE_AUDIO_DIRECTSOUND_EFFECT_IMAGE,
    NATIVE_HOST_SERVICE_AUDIO_STREAM_CREATE,
    NATIVE_HOST_SERVICE_AUDIO_STREAM_FLUSH,
    NATIVE_HOST_SERVICE_AUDIO_STREAM_PROCESS,
    NATIVE_HOST_SERVICE_BOOTSTRAP,
    NATIVE_HOST_SERVICE_BOOTSTRAP_HAL_GET_INTERRUPT_VECTOR,
    NATIVE_HOST_SERVICE_BOOTSTRAP_AV_GET_SAVED_DATA_ADDRESS,
    NATIVE_HOST_SERVICE_BOOTSTRAP_AV_SET_DISPLAY_MODE,
    NATIVE_HOST_SERVICE_BOOTSTRAP_AV_SET_SAVED_DATA_ADDRESS,
    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_CLOSE,
    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_CREATE_FILE,
    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_DEVICE_IO_CONTROL_FILE,
    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_FS_CONTROL_FILE,
    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_OPEN_FILE,
    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_OPEN_SYMBOLIC_LINK_OBJECT,
    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_QUERY_INFORMATION_FILE,
    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_QUERY_DIRECTORY_FILE,
    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_READ_FILE,
    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_SET_INFORMATION_FILE,
    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_WRITE_FILE,
    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_QUERY_SYMBOLIC_LINK_OBJECT,
    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_QUERY_VOLUME_INFORMATION_FILE,
    NATIVE_HOST_SERVICE_BOOTSTRAP_RTL_TIME_FIELDS_TO_TIME,
    NATIVE_HOST_SERVICE_BOOTSTRAP_RTL_TIME_TO_TIME_FIELDS,
    NATIVE_HOST_SERVICE_BOOTSTRAP_XC_HMAC,
    NATIVE_HOST_SERVICE_BOOTSTRAP_XC_RC4_CRYPT,
    NATIVE_HOST_SERVICE_BOOTSTRAP_XC_RC4_KEY,
    NATIVE_HOST_SERVICE_BOOTSTRAP_XC_SHA_FINAL,
    NATIVE_HOST_SERVICE_BOOTSTRAP_XC_SHA_INIT,
    NATIVE_HOST_SERVICE_BOOTSTRAP_XC_SHA_UPDATE,
    NATIVE_HOST_SERVICE_COLD_CALLBACK,
    NATIVE_HOST_SERVICE_IRQL,
    NATIVE_HOST_SERVICE_IRQL_GET_CURRENT,
    NATIVE_HOST_SERVICE_IRQL_LOWER,
    NATIVE_HOST_SERVICE_IRQL_RAISE,
    NATIVE_HOST_SERVICE_MEMORY,
    NATIVE_HOST_SERVICE_MEMORY_ALLOCATE_CONTIGUOUS_EX,
    NATIVE_HOST_SERVICE_MEMORY_GET_PHYSICAL_ADDRESS,
    NATIVE_HOST_SERVICE_MEMORY_VALIDATE_RANGE,
    NATIVE_HOST_SERVICE_SEMAPHORE,
    NATIVE_HOST_SERVICE_SEMAPHORE_RELEASE,
    NATIVE_HOST_SERVICE_SEMAPHORE_KERNEL_WAIT,
    NATIVE_HOST_SERVICE_SEMAPHORE_WAIT,
    NATIVE_HOST_SERVICE_RUNTIME,
    NATIVE_HOST_SERVICE_RUNTIME_ANSI_STRING_TO_UNICODE_STRING,
    NATIVE_HOST_SERVICE_RUNTIME_EQUAL_STRING,
    NATIVE_HOST_SERVICE_RUNTIME_INIT_ANSI_STRING,
    NATIVE_HOST_SERVICE_RUNTIME_NT_STATUS_TO_DOS_ERROR,
    NATIVE_HOST_SERVICE_RUNTIME_UNICODE_STRING_TO_ANSI_STRING,
    NATIVE_HOST_SERVICE_LIFECYCLE_CREATE_SEMAPHORE,
    NATIVE_HOST_SERVICE_LIFECYCLE_CREATE_WORKER,
    NATIVE_HOST_SERVICE_LIFECYCLE_REFERENCE_WORKER,
    NATIVE_HOST_SERVICE_LIFECYCLE_SET_BASE_PRIORITY,
    NATIVE_HOST_SERVICE_RETURN_CONSTANT,
    NATIVE_HOST_SERVICE_SYSTEM_TIME,
    NATIVE_HOST_SERVICE_TITLE,
    NATIVE_HOST_SERVICE_TITLE_ASSET_CLOSE,
    NATIVE_HOST_SERVICE_TITLE_ASSET_OPEN,
    NATIVE_HOST_SERVICE_TITLE_ASSET_READ,
    NATIVE_HOST_SERVICE_TITLE_FRONTEND_SPECIAL_AUDIO_CREATE,
    NATIVE_HOST_SERVICE_TITLE_MUSIC_MODE_SET,
    NATIVE_HOST_SERVICE_TITLE_SPIN_DELAY,
    NATIVE_HOST_SERVICE_TITLE_TEXT_DRAW,
    NATIVE_WORKER_COMPLETED,
    NATIVE_WORKER_READY,
    NATIVE_WORKER_RUNNING,
    NATIVE_DISPATCH_EDGE_CAPACITY_LIMIT,
    NATIVE_DISPATCH_EDGE_REPORT_LIMIT,
    NATIVE_TARGET_TIMING_SAMPLE_INTERVAL,
    AOT_OPTIMIZATION_MODES,
    NativeCooperativeSchedulerState,
    NativeExecutorError,
    NativeHostServiceEntry,
    NativeHostServiceState,
    NativeModuleManifest,
    NativeResumableExecutor,
    NativeWorkerLifecycleState,
    _partition_instructions_by_address,
    _partition_instructions_for_call_fusion,
    aot_optimization_features,
)
from tools.recomp.x86_lifter import (
    CppEmitter,
    CpuState,
    ExecutionTrace,
    LiftedFunction,
    NativeFastPath,
    RESUMABLE_REPEAT_CHUNK_ITERATIONS,
    SparseMemory,
    lift_x86_function,
)


class NativeResumableExecutorTests(unittest.TestCase):
    def test_manual_capsule_restores_verified_instruction_bytes(self) -> None:
        function = lift_x86_function(b"\x40\xc3", base_address=0x1000)
        executor = object.__new__(NativeResumableExecutor)
        executor._capsule_functions = (function,)
        output = Path("reports/local/replay/checkpoint.b2rcap")

        with (
            patch("tools.playability.replay_capsule.require_local_capsule_output"),
            patch(
                "tools.playability.replay_capsule.capture_replay_capsule",
                return_value=output,
            ) as capture,
        ):
            result = executor.capture_manual_replay_capsule(
                output,
                state=CpuState(),
                memory=SparseMemory(),
                scheduler_state={},
                service_state={},
                provenance={},
                instruction_bytes_reader=lambda address, size: b"\x90" * size,
            )

        self.assertEqual(result, output)
        captured = capture.call_args.kwargs["functions"]
        self.assertEqual([item.bytes_hex for item in captured[0].instructions], ["90", "90"])

    def test_manual_capsule_freezes_native_resident_memory(self) -> None:
        class NativeResidentMemory(SparseMemory):
            def __init__(self) -> None:
                super().__init__()
                self.snapshot = SparseMemory({0x3000: 0x5A})
                self.captured_page_table = None

            def replay_capsule_memory_snapshot(self) -> SparseMemory:
                return self.snapshot

            def replay_capsule_memory_snapshot_from_native_pages(
                self,
                page_table,
            ) -> SparseMemory:
                self.captured_page_table = page_table
                return self.snapshot

        function = lift_x86_function(b"\xc3", base_address=0x1000)
        executor = object.__new__(NativeResumableExecutor)
        executor._capsule_functions = (function,)
        executor._page_table = object()
        memory = NativeResidentMemory()

        with (
            patch("tools.playability.replay_capsule.require_local_capsule_output"),
            patch(
                "tools.playability.replay_capsule.capture_replay_capsule",
                return_value=Path("reports/local/replay/checkpoint.b2rcap"),
            ) as capture,
        ):
            executor.capture_manual_replay_capsule(
                Path("reports/local/replay/checkpoint.b2rcap"),
                state=CpuState(),
                memory=memory,
                scheduler_state={},
                service_state={},
                provenance={},
                memory_overlays={0x7000: b"phase5"},
            )

        self.assertIs(capture.call_args.kwargs["memory"], memory.snapshot)
        self.assertIs(memory.captured_page_table, executor._page_table)
        self.assertEqual(memory.snapshot.read(0x7000, 6), b"phase5")

    def test_manual_capsule_overlay_does_not_mutate_live_sparse_memory(self) -> None:
        function = lift_x86_function(b"\xc3", base_address=0x1000)
        executor = object.__new__(NativeResumableExecutor)
        executor._capsule_functions = (function,)
        executor._page_table = object()
        memory = SparseMemory({0x7000: 0x11})

        with (
            patch("tools.playability.replay_capsule.require_local_capsule_output"),
            patch(
                "tools.playability.replay_capsule.capture_replay_capsule",
                return_value=Path("reports/local/replay/checkpoint.b2rcap"),
            ) as capture,
        ):
            executor.capture_manual_replay_capsule(
                Path("reports/local/replay/checkpoint.b2rcap"),
                state=CpuState(),
                memory=memory,
                scheduler_state={},
                service_state={},
                provenance={},
                memory_overlays={0x7000: b"\x22"},
            )

        self.assertEqual(memory.read(0x7000, 1), b"\x11")
        self.assertEqual(
            capture.call_args.kwargs["memory"].read(0x7000, 1),
            b"\x22",
        )

    def test_aot_ab_modes_resolve_independent_features(self) -> None:
        expected = {
            "baseline": (False, False),
            "fusion-only": (True, False),
            "registerization-only": (False, True),
            "combined": (True, True),
        }

        self.assertEqual(set(AOT_OPTIMIZATION_MODES), set(expected))
        for mode, features in expected.items():
            with self.subTest(mode=mode):
                self.assertEqual(aot_optimization_features(mode), features)

        with self.assertRaisesRegex(ValueError, "unknown AOT optimization mode"):
            aot_optimization_features("invalid")

    def test_baseline_aot_mode_compiles_context_resident_guest_state(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("C3"),
            base_address=0x1000,
            symbol="baseline_aot_mode",
        )
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory({0x8000: 0})

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                preferred_fusion_edges={(0x1000, 0x2000)},
                aot_optimization_mode="baseline",
            )
            returned_to = executor.run(state, memory, max_steps=2)

        self.assertEqual(returned_to, 0)
        self.assertEqual(executor.cache_summary["aot_optimization_mode"], "baseline")
        self.assertFalse(executor.cache_summary["preferred_fusion_enabled"])
        self.assertFalse(
            executor.cache_summary["registerized_guest_state_enabled"]
        )
        self.assertFalse(
            executor.cache_summary["coalesced_memory_accesses_enabled"]
        )
        self.assertEqual(
            executor.cache_summary["configured_preferred_fusion_edge_count"],
            1,
        )
        self.assertEqual(executor.cache_summary["preferred_fusion_edge_count"], 0)
        self.assertFalse(executor.cache_summary["preserve_debug_symbols"])
        self.assertEqual(executor.cache_summary["generated_pdb_count"], 0)

    def test_fusion_aot_mode_enables_coalesced_memory_accesses(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("C3"),
            base_address=0x1100,
            symbol="fusion_coalesced_memory",
        )
        state = CpuState.with_registers(esp=0x8100)
        memory = SparseMemory({0x8100: 0})

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                aot_optimization_mode="fusion-only",
            )
            returned_to = executor.run(state, memory, max_steps=2)

        self.assertEqual(returned_to, 0)
        self.assertTrue(
            executor.cache_summary["coalesced_memory_accesses_enabled"]
        )

    def test_native_runtime_status_collision_maps_to_already_exists(self) -> None:
        base_address = 0x1000
        status_target = 0x2000
        code = bytearray(b"\x68\x35\x00\x00\xC0")
        call_address = base_address + len(code)
        code.extend(
            b"\xE8" + struct.pack("<i", status_target - (call_address + 5))
        )
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_status_collision_mapping",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    status_target,
                    NATIVE_HOST_SERVICE_RUNTIME,
                    4,
                    NATIVE_HOST_SERVICE_RUNTIME_NT_STATUS_TO_DOS_ERROR,
                )
            ]
        )
        memory = SparseMemory({0x8000: 0})

        def unexpected_handler(*_args: object) -> None:
            self.fail("native status conversion crossed into Python")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                callback_addresses={status_target},
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                call_handlers={status_target: unexpected_handler},
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=8,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 183)

    def test_native_runtime_status_path_not_found_maps_to_path_not_found(
        self,
    ) -> None:
        base_address = 0x1000
        status_target = 0x2000
        code = bytearray(b"\x68\x3A\x00\x00\xC0")
        call_address = base_address + len(code)
        code.extend(
            b"\xE8" + struct.pack("<i", status_target - (call_address + 5))
        )
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_status_path_not_found_mapping",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    status_target,
                    NATIVE_HOST_SERVICE_RUNTIME,
                    4,
                    NATIVE_HOST_SERVICE_RUNTIME_NT_STATUS_TO_DOS_ERROR,
                )
            ]
        )
        memory = SparseMemory({0x8000: 0})

        def unexpected_handler(*_args: object) -> None:
            self.fail("native status conversion crossed into Python")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                callback_addresses={status_target},
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                call_handlers={status_target: unexpected_handler},
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=8,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 3)
        self.assertEqual(
            executor.last_run_summary["performance"]["handler_call_count"],
            0,
        )

    def test_shld_cl_address_taken_block_executes_natively(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("0FA5C2C3"),
            base_address=0x001206AA,
            symbol="native_shld_cl_observed",
        )
        state = CpuState.with_registers(
            eax=0x80000000,
            ecx=2,
            edx=0x40000000,
            esp=0x8000,
        )
        memory = SparseMemory({0x3000: 0, 0x8000: 0})

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(function, build_dir=Path(temp_dir))
            returned_to = executor.run(state, memory, max_steps=4)

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("edx"), 2)
        self.assertTrue(state.flags.cf)

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
                (1 << 18) | 0x1B1C,
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

    def test_x87_stack_preserves_double_precision_between_instructions(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "D90500200000"  # fld dword [0x2000]
                "D80504200000"  # fadd dword [0x2004]
                "D82500200000"  # fsub dword [0x2000]
                "D91D08200000"  # fstp dword [0x2008]
                "C3"
            ),
            base_address=0x6080,
            symbol="native_x87_double_precision_stack",
        )
        state = CpuState.with_registers(esp=0x8000)
        state.fpu_control_word = 0x023F
        memory = SparseMemory(
            {
                0x2000: struct.pack("<f", 16_777_216.0),
                0x2004: struct.pack("<f", 1.0),
                0x8000: 0,
            }
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(function, build_dir=Path(temp_dir))
            returned_to = executor.run(state, memory, max_steps=8)

        self.assertEqual(returned_to, 0)
        self.assertEqual(struct.unpack("<f", memory.read(0x2008, 4))[0], 1.0)
        self.assertEqual(state.fpu_stack, [])
        self.assertEqual(state.fpu_control_word, 0x023F)

    def test_rsqrtss_uses_xbox_intel_approximation_natively(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "F30F100500200000"  # movss xmm0, [0x2000]
                "F30F52C0"  # rsqrtss xmm0, xmm0
                "F30F110504200000"  # movss [0x2004], xmm0
                "C3"
            ),
            base_address=0x60C0,
            symbol="native_rsqrtss_intel_approximation",
        )
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory(
            {
                0x2000: struct.pack("<f", 1.0),
                0x8000: 0,
            }
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(function, build_dir=Path(temp_dir))
            returned_to = executor.run(state, memory, max_steps=8)

        self.assertEqual(returned_to, 0)
        self.assertEqual(memory.read_u32(0x2004), 0x3F7FF000)

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
                        instructions, maximum_count=3000
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
        self.assertEqual(
            NATIVE_DISPATCH_EDGE_REPORT_LIMIT,
            NATIVE_DISPATCH_EDGE_CAPACITY_LIMIT,
        )
        self.assertLessEqual(
            edge_profile["table_capacity"],
            NATIVE_DISPATCH_EDGE_CAPACITY_LIMIT,
        )
        self.assertEqual(
            edge_profile["table_capacity"],
            min(
                (executor._dispatch_mask + 1) * 8,
                NATIVE_DISPATCH_EDGE_CAPACITY_LIMIT,
            ),
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
        timing_targets = performance["native_dispatch_hot_targets"]
        self.assertEqual(
            {item["target"] for item in timing_targets},
            {0x1000, 0x1005, 0x11000},
        )
        self.assertTrue(
            all(item["timing_sample_count"] == 1 for item in timing_targets)
        )
        self.assertTrue(
            all(item["sampled_min_ns"] is not None for item in timing_targets)
        )
        self.assertTrue(
            all(item["sampled_max_ns"] is not None for item in timing_targets)
        )
        self.assertTrue(
            all(item["sampled_guest_steps"] > 0 for item in timing_targets)
        )
        self.assertTrue(
            all(
                item["execution_lanes"]["primary"]["module_calls"] == 1
                and item["execution_lanes"]["worker"]["module_calls"] == 0
                and item["execution_lanes"]["vblank"]["module_calls"] == 0
                for item in timing_targets
            )
        )
        self.assertTrue(
            all(
                item["timing_sample_interval"]
                == NATIVE_TARGET_TIMING_SAMPLE_INTERVAL
                for item in timing_targets
            )
        )
        self.assertEqual(
            performance["profile_capture_window"][
                "profiling_overhead_estimate"
            ]["sample_count"],
            3,
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
                        instructions, maximum_count=3000
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
        self.assertEqual(performance["read_u8_callback_count"], 0)
        self.assertEqual(performance["read_u32_callback_count"], 0)
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

    def test_preferred_superblock_edge_overrides_fast_path_isolation(self) -> None:
        class Instruction:
            def __init__(self, address: int, mnemonic: str = "nop") -> None:
                self.address = address
                self.mnemonic = mnemonic
                self.target = None

        caller = Instruction(0x1000)
        fast_path_owner = Instruction(0x21000, "ret")
        unrelated = Instruction(0x41000)

        modules = _partition_instructions_for_call_fusion(
            [caller, fast_path_owner, unrelated],
            base_maximum_count=1,
            maximum_count=2,
            isolated_addresses={fast_path_owner.address},
            preferred_edges={(caller.address, fast_path_owner.address)},
        )

        self.assertEqual(
            [[item.address for item in module] for module in modules],
            [[0x1000, 0x21000], [0x41000]],
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

    def test_split_resumable_switch_preserves_cross_chunk_control_flow(self) -> None:
        caller = lift_x86_function(
            bytes.fromhex(
                "40"          # inc eax
                "E802000000"  # call 0x1008
                "40"          # inc eax
                "C3"          # ret
            ),
            base_address=0x1000,
            symbol="split_resumable_caller",
        )
        callee = lift_x86_function(
            bytes.fromhex("40C3"),  # inc eax; ret
            base_address=0x1008,
            symbol="split_resumable_callee",
        )
        function = LiftedFunction(
            symbol="split_resumable_switch",
            base_address=caller.base_address,
            code_size=callee.instructions[-1].next_address - caller.base_address,
            instructions=tuple((*caller.instructions, *callee.instructions)),
        )
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory({0x8000: 0})

        with (
            tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir,
            patch.object(CppEmitter, "MAX_RESUMABLE_SWITCH_INSTRUCTIONS", 2),
        ):
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
            )
            returned_to = executor.run(state, memory, max_steps=12)

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 3)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_module_call_count"], 1)
        self.assertEqual(
            performance["native_module_exit_profile"]["reason_counts"]["return"],
            1,
        )

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

    def test_native_callback_change_invalidates_only_owning_partition(self) -> None:
        first = lift_x86_function(
            bytes.fromhex("40C3"),
            base_address=0x1000,
            symbol="localized_callback_first",
        )
        second = lift_x86_function(
            bytes.fromhex("48C3"),
            base_address=0x2000,
            symbol="localized_callback_second",
        )
        function = LiftedFunction(
            symbol="localized_callback_frame",
            base_address=first.base_address,
            code_size=second.instructions[-1].next_address - first.base_address,
            instructions=tuple((*first.instructions, *second.instructions)),
        )

        with (
            tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir,
            patch.object(NativeResumableExecutor, "BASE_INSTRUCTIONS_PER_MODULE", 2),
            patch.object(NativeResumableExecutor, "MAX_INSTRUCTIONS_PER_MODULE", 2),
        ):
            build_dir = Path(temp_dir)
            cold = NativeResumableExecutor(function, build_dir=build_dir)
            changed = NativeResumableExecutor(
                function,
                build_dir=build_dir,
                callback_addresses={first.base_address},
            )

        self.assertEqual(cold.cache_summary["known_reachable_partition_count"], 2)
        self.assertEqual(cold.cache_summary["ahead_compiled_count"], 2)
        self.assertEqual(changed.cache_summary["warm_hit_count"], 1)
        self.assertEqual(changed.cache_summary["ahead_compiled_count"], 1)
        self.assertTrue(changed.cache_summary["localized_address_configuration"])

    def test_native_module_manifest_prunes_old_artifacts(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            build_dir = Path(temp_dir)
            manifest = NativeModuleManifest(build_dir, max_artifacts=1)
            first_source = build_dir / "native-loop-first.cpp"
            first_artifact = build_dir / "native-loop-first.dll"
            first_object = first_source.with_suffix(".obj")
            first_pdb = first_artifact.with_suffix(".pdb")
            first_metadata = first_source.with_suffix(".debug.json")
            second_source = build_dir / "native-loop-second.cpp"
            second_artifact = build_dir / "native-loop-second.dll"
            orphan_import_library = build_dir / "native-loop-legacy.lib"
            for path in (
                first_source,
                first_artifact,
                second_source,
                second_artifact,
                first_object,
                first_pdb,
                first_metadata,
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
            self.assertFalse(first_object.exists())
            self.assertFalse(first_pdb.exists())
            self.assertFalse(first_metadata.exists())
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
        # data and return-address page fills come from the bound native view.
        self.assertEqual(performance["read_u32_callback_count"], 1)
        self.assertEqual(performance["exact_read_u32_callback_count"], 1)
        self.assertEqual(performance["page_miss_read_u32_callback_count"], 0)
        self.assertEqual(performance["page_cache_fill_count"], 2)
        self.assertGreater(performance["dirty_sync_no_work_count"], 0)
        read_samples = performance["read_callback_sampling"]
        self.assertEqual(read_samples["interval"], 1024)
        self.assertEqual(read_samples["exact_interval"], 1)
        self.assertEqual(read_samples["sample_count"], 1)
        self.assertEqual(
            {
                (sample["kind"], sample["address_hex"])
                for sample in read_samples["hot_addresses"]
            },
            {
                ("exact_u32", "0x00003004"),
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
        self.assertEqual(performance["read_u32_callback_count"], 1)
        self.assertEqual(performance["exact_read_u32_callback_count"], 1)
        self.assertEqual(performance["page_miss_read_u32_callback_count"], 0)
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
        self.assertEqual(performance["exact_read_u32_callback_count"], 1)
        self.assertEqual(performance["write_u32_callback_count"], 0)
        self.assertEqual(performance["zero_read_callback_bypass_count"], 2)
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

    def test_native_observer_site_bypasses_python_callback(self) -> None:
        base_address = 0x1000
        function = lift_x86_function(
            b"\x90\xC3",
            base_address=base_address,
            symbol="native_observer_site",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    0x2000,
                    NATIVE_HOST_SERVICE_RETURN_CONSTANT,
                    0,
                    0,
                )
            ]
        )

        def unexpected_observer(*_args: object) -> None:
            self.fail("native observer site crossed into Python")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                observer_addresses={base_address},
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                SparseMemory({0x8000: 0}),
                step_observer=unexpected_observer,
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                dispatch_observers_in_native=True,
                profile_hot_paths=True,
                max_steps=4,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(services.native_observer_count, 1)
        self.assertEqual(
            executor.last_run_summary["performance"]["observer_callback_count"],
            0,
        )
        performance = executor.last_run_summary["performance"]
        self.assertTrue(performance["hot_path_profiling_enabled"])
        self.assertTrue(performance["native_observer_dispatch_enabled"])
        self.assertEqual(
            performance["profile_capture_window"]["captured_module_calls"],
            1,
        )
        self.assertTrue(performance["native_module_edge_profile"]["exact"])

    def test_native_world_draw_observer_records_mesh_submission(self) -> None:
        base_address = 0x000C4A70
        owner_address = 0x9000
        mesh_entry = 0xA000
        index_data = 0xB000
        function = lift_x86_function(
            b"\x90\xC3",
            base_address=base_address,
            symbol="native_world_draw_observer",
        )
        services = NativeHostServiceState([])
        services.enable_normal_runtime()
        memory = SparseMemory(
            {
                0x8000: 0,
                0x8004: mesh_entry,
                owner_address + 0x14: 3,
                mesh_entry + 0x0C: index_data,
                mesh_entry + 0x10: 18,
            }
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                observer_addresses={base_address},
            )
            state = CpuState.with_registers(esp=0x8000, ecx=owner_address)
            returned_to = executor.run(
                state,
                memory,
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                dispatch_observers_in_native=True,
                max_steps=4,
            )
            executor.shutdown_normal_runtime(services)

        self.assertEqual(returned_to, 0)
        self.assertEqual(services.native_traffic_world_draw_count, 1)
        self.assertEqual(services.native_traffic_world_zero_index_count, 0)
        self.assertEqual(services.native_traffic_mesh_sample_count, 1)
        sample = services.native_traffic_mesh_samples[0]
        self.assertEqual(sample.caller, 0)
        self.assertEqual(sample.owner, owner_address)
        self.assertEqual(sample.owner_mode, 3)
        self.assertEqual(sample.mesh_entry, mesh_entry)
        self.assertEqual(sample.index_data, index_data)
        self.assertEqual(sample.index_count, 18)

    def test_native_normal_runtime_repairs_frontend_record_table_count(self) -> None:
        base_address = 0x00112873
        table_address = 0x9000
        function = lift_x86_function(
            b"\x90\xC3",
            base_address=base_address,
            symbol="native_frontend_record_table_repair",
        )
        services = NativeHostServiceState([])
        services.enable_normal_runtime()
        memory = SparseMemory(
            {
                0x8000: 0,
                table_address + 4: 0xFFFFFFFF,
            }
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                observer_addresses={base_address},
            )
            state = CpuState.with_registers(esp=0x8000, edi=table_address)
            returned_to = executor.run(
                state,
                memory,
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                dispatch_observers_in_native=True,
                max_steps=4,
            )
            executor.shutdown_normal_runtime(services)

        self.assertEqual(returned_to, 0)
        self.assertEqual(memory.read_u32(table_address + 4), 0)
        self.assertEqual(services.native_frontend_record_table_repair_count, 1)
        self.assertEqual(
            services.native_frontend_record_table_last_address,
            table_address + 4,
        )
        self.assertEqual(
            executor.last_run_summary["performance"]["observer_callback_count"],
            0,
        )

    def test_native_irql_services_preserve_state_without_python(self) -> None:
        base_address = 0x1000
        raise_target = 0x2000
        lower_target = 0x2010
        current_target = 0x2020
        result_address = 0x3000
        code = bytearray()

        def append_call(target: int) -> None:
            call_address = base_address + len(code)
            code.extend(b"\xE8" + struct.pack("<i", target - (call_address + 5)))

        code.extend(b"\x6A\x02")
        append_call(raise_target)
        code.extend(b"\xA3" + struct.pack("<I", result_address))
        append_call(current_target)
        code.extend(b"\xA3" + struct.pack("<I", result_address + 4))
        code.extend(b"\x6A\x01")
        append_call(lower_target)
        code.extend(b"\xA3" + struct.pack("<I", result_address + 8))
        append_call(current_target)
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_irql_services",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    raise_target,
                    NATIVE_HOST_SERVICE_IRQL,
                    4,
                    NATIVE_HOST_SERVICE_IRQL_RAISE,
                ),
                NativeHostServiceEntry(
                    lower_target,
                    NATIVE_HOST_SERVICE_IRQL,
                    4,
                    NATIVE_HOST_SERVICE_IRQL_LOWER,
                ),
                NativeHostServiceEntry(
                    current_target,
                    NATIVE_HOST_SERVICE_IRQL,
                    0,
                    NATIVE_HOST_SERVICE_IRQL_GET_CURRENT,
                ),
            ]
        )

        def unexpected_handler(*_args: object) -> None:
            self.fail("native IRQL service crossed into Python")

        memory = SparseMemory({0x8000: 0})
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                callback_addresses={raise_target, lower_target, current_target},
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                call_handlers={
                    raise_target: unexpected_handler,
                    lower_target: unexpected_handler,
                    current_target: unexpected_handler,
                },
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=32,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(memory.read_u32(result_address), 0)
        self.assertEqual(memory.read_u32(result_address + 4), 2)
        self.assertEqual(memory.read_u32(result_address + 8), 0)
        self.assertEqual(state.get_register("eax"), 1)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_host_service_call_count"], 4)
        self.assertEqual(performance["handler_call_count"], 0)
        service_profiles = {
            item["target"]: item
            for item in performance["native_host_service_hot_targets"]
        }
        self.assertEqual(
            set(service_profiles),
            {raise_target, lower_target, current_target},
        )
        self.assertEqual(service_profiles[current_target]["calls"], 2)
        self.assertEqual(
            service_profiles[current_target]["execution_lanes"]["primary"][
                "calls"
            ],
            2,
        )
        self.assertEqual(
            service_profiles[current_target]["timing_sample_count"],
            1,
        )

    def test_native_semaphore_release_and_wait_bypass_python(self) -> None:
        base_address = 0x1000
        create_target = 0x1FF0
        release_target = 0x2000
        wait_target = 0x2010
        handle = 0x108
        handle_address = 0x3010
        previous_count_address = 0x3000
        code = bytearray()

        def append_call(target: int) -> None:
            call_address = base_address + len(code)
            code.extend(b"\xE8" + struct.pack("<i", target - (call_address + 5)))

        code.extend(b"\x6A\x01\x6A\x00\x6A\x00")
        code.extend(b"\x68" + struct.pack("<I", handle_address))
        append_call(create_target)
        code.extend(b"\x68" + struct.pack("<I", previous_count_address))
        code.extend(b"\x6A\x01")
        code.extend(b"\x68" + struct.pack("<I", handle))
        append_call(release_target)
        code.extend(b"\x6A\x00\x6A\x00\x6A\x00")
        code.extend(b"\x68" + struct.pack("<I", handle))
        append_call(wait_target)
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_semaphore_services",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    create_target,
                    NATIVE_HOST_SERVICE_COLD_CALLBACK,
                    16,
                    NATIVE_HOST_SERVICE_LIFECYCLE_CREATE_SEMAPHORE,
                ),
                NativeHostServiceEntry(
                    release_target,
                    NATIVE_HOST_SERVICE_SEMAPHORE,
                    12,
                    NATIVE_HOST_SERVICE_SEMAPHORE_RELEASE,
                ),
                NativeHostServiceEntry(
                    wait_target,
                    NATIVE_HOST_SERVICE_SEMAPHORE,
                    16,
                    NATIVE_HOST_SERVICE_SEMAPHORE_WAIT,
                ),
            ]
        )

        def unexpected_handler(*_args: object) -> None:
            self.fail("native semaphore service crossed into Python")

        memory = SparseMemory({0x8000: 0})
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                callback_addresses={create_target, release_target, wait_target},
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                call_handlers={
                    create_target: unexpected_handler,
                    release_target: unexpected_handler,
                    wait_target: unexpected_handler,
                },
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=32,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 0)
        self.assertEqual(memory.read_u32(handle_address), handle)
        self.assertEqual(memory.read_u32(previous_count_address), 0)
        self.assertEqual(services.semaphores[0].count, 0)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_host_service_call_count"], 3)
        self.assertEqual(performance["handler_call_count"], 0)

    def test_native_primary_semaphore_wait_yields_without_worker_transition(
        self,
    ) -> None:
        base_address = 0x1000
        create_target = 0x1FF0
        wait_target = 0x2010
        handle = 0x108
        handle_address = 0x3010
        code = bytearray()

        def append_call(target: int) -> None:
            call_address = base_address + len(code)
            code.extend(b"\xE8" + struct.pack("<i", target - (call_address + 5)))

        code.extend(b"\x6A\x01\x6A\x00\x6A\x00")
        code.extend(b"\x68" + struct.pack("<I", handle_address))
        append_call(create_target)
        code.extend(b"\x6A\x00\x6A\x00\x6A\x00")
        code.extend(b"\x68" + struct.pack("<I", handle))
        append_call(wait_target)
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_primary_semaphore_wait",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    create_target,
                    NATIVE_HOST_SERVICE_COLD_CALLBACK,
                    16,
                    NATIVE_HOST_SERVICE_LIFECYCLE_CREATE_SEMAPHORE,
                ),
                NativeHostServiceEntry(
                    wait_target,
                    NATIVE_HOST_SERVICE_SEMAPHORE,
                    16,
                    NATIVE_HOST_SERVICE_SEMAPHORE_WAIT,
                ),
            ]
        )
        lifecycle = NativeWorkerLifecycleState()
        services.attach_worker_lifecycle(lifecycle)
        services.current_worker_handle = handle
        memory = SparseMemory({0x8000: 0})

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                callback_addresses={create_target, wait_target},
            )
            executor.synchronize_worker_lifecycle(
                lifecycle,
                [
                    {
                        "handle": handle,
                        "start_address": base_address,
                        "start_context1": 0,
                        "start_context2": 0,
                        "suspended": False,
                    }
                ],
            )
            state = CpuState.with_registers(esp=0x8000)
            executor.run(
                state,
                memory,
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=32,
            )

        worker = lifecycle.entry_for_handle(handle)
        self.assertIsNotNone(worker)
        assert worker is not None
        self.assertEqual(worker.status, NATIVE_WORKER_READY)
        self.assertEqual(worker.wait_handle, 0)
        self.assertEqual(state.get_register("eax"), 0x00000102)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_host_service_call_count"], 2)
        self.assertEqual(performance["handler_call_count"], 0)

    def test_native_semaphore_invalid_handle_returns_ntstatus(self) -> None:
        base_address = 0x1000
        release_target = 0x2000
        invalid_handle = 0xDEAD
        code = bytearray()
        code.extend(b"\x6A\x00\x6A\x01")
        code.extend(b"\x68" + struct.pack("<I", invalid_handle))
        call_address = base_address + len(code)
        code.extend(
            b"\xE8"
            + struct.pack("<i", release_target - (call_address + 5))
        )
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_invalid_semaphore_release",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    release_target,
                    NATIVE_HOST_SERVICE_SEMAPHORE,
                    12,
                    NATIVE_HOST_SERVICE_SEMAPHORE_RELEASE,
                )
            ]
        )
        memory = SparseMemory({0x8000: 0})
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                callback_addresses={release_target},
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                call_handlers={release_target: lambda *_args: self.fail(
                    "invalid semaphore crossed into Python"
                )},
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=16,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 0xC0000008)
        self.assertEqual(services.last_service_target, release_target)
        self.assertEqual(services.last_service_argument0, invalid_handle)
        self.assertEqual(services.native_call_count, 1)

    def test_native_semaphore_table_reuses_closed_slots(self) -> None:
        base_address = 0x1000
        create_target = 0x100000
        close_target = 0x100010
        release_target = 0x100020
        handle_address = 0x3000
        cycle_count = 140
        code = bytearray()

        def emit_call(target: int, arguments: tuple[int, ...]) -> None:
            for argument in reversed(arguments):
                code.extend(b"\x68" + struct.pack("<I", argument))
            call_address = base_address + len(code)
            code.extend(
                b"\xE8" + struct.pack("<i", target - (call_address + 5))
            )

        def emit_handle_call(target: int, trailing: bytes = b"") -> None:
            code.extend(trailing)
            code.extend(b"\xFF\x35" + struct.pack("<I", handle_address))
            call_address = base_address + len(code)
            code.extend(
                b"\xE8" + struct.pack("<i", target - (call_address + 5))
            )

        for index in range(cycle_count):
            emit_call(create_target, (handle_address, 0, 0, 1))
            if index + 1 != cycle_count:
                emit_handle_call(close_target)
        emit_handle_call(release_target, b"\x6A\x00\x6A\x01")
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_repeated_semaphore_lifecycle",
            max_instructions=2000,
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    create_target,
                    NATIVE_HOST_SERVICE_COLD_CALLBACK,
                    16,
                    NATIVE_HOST_SERVICE_LIFECYCLE_CREATE_SEMAPHORE,
                ),
                NativeHostServiceEntry(
                    close_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    4,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_CLOSE,
                ),
                NativeHostServiceEntry(
                    release_target,
                    NATIVE_HOST_SERVICE_SEMAPHORE,
                    12,
                    NATIVE_HOST_SERVICE_SEMAPHORE_RELEASE,
                ),
            ]
        )
        memory = SparseMemory({0x9000: 0})
        targets = {create_target, close_target, release_target}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                callback_addresses=targets,
            )
            state = CpuState.with_registers(esp=0x9000)
            returned_to = executor.run(
                state,
                memory,
                call_handlers={target: lambda *_args: self.fail(
                    "semaphore lifecycle crossed into Python"
                ) for target in targets},
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=2000,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 0)
        self.assertEqual(
            memory.read_u32(handle_address),
            0x108 + (cycle_count - 1) * 4,
        )
        self.assertEqual(services.semaphore_count, 1)
        self.assertEqual(services.semaphore_overflow_count, 0)
        self.assertEqual(services.semaphores[0].count, 1)
        self.assertEqual(
            services.native_call_count,
            cycle_count + (cycle_count - 1) + 1,
        )

    def test_seeded_semaphore_advances_native_object_handles(self) -> None:
        services = NativeHostServiceState()

        services.seed_semaphores(
            ({"handle": 0x140, "count": 0, "limit": 1},)
        )

        self.assertEqual(services.semaphore_count, 1)
        self.assertEqual(services.next_object_handle, 0x144)

    def test_native_kernel_object_wait_bypasses_python(self) -> None:
        base_address = 0x1000
        wait_target = 0x2000
        code = bytearray(b"\x6A\x00\x6A\x00\x6A\x00\x6A\x00\x68\x00\x30\x00\x00")
        call_address = base_address + len(code)
        code.extend(
            b"\xE8" + struct.pack("<i", wait_target - (call_address + 5))
        )
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_kernel_object_wait",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    wait_target,
                    NATIVE_HOST_SERVICE_SEMAPHORE,
                    20,
                    NATIVE_HOST_SERVICE_SEMAPHORE_KERNEL_WAIT,
                )
            ]
        )

        def unexpected_handler(*_args: object) -> None:
            self.fail("native kernel object wait crossed into Python")

        memory = SparseMemory({0x8000: 0})
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                callback_addresses={wait_target},
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                call_handlers={wait_target: unexpected_handler},
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=16,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 0)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_host_service_call_count"], 1)
        self.assertEqual(performance["handler_call_count"], 0)

    def test_native_runtime_string_services_bypass_python(self) -> None:
        base_address = 0x1000
        init_target = 0x2000
        equal_target = 0x2010
        left_descriptor = 0x3000
        right_descriptor = 0x3010
        left_buffer = 0x3100
        right_buffer = 0x3120
        code = bytearray()

        def push_u32(value: int) -> None:
            code.extend(b"\x68" + struct.pack("<I", value))

        def append_call(target: int) -> None:
            call_address = base_address + len(code)
            code.extend(b"\xE8" + struct.pack("<i", target - (call_address + 5)))

        push_u32(left_buffer)
        push_u32(left_descriptor)
        append_call(init_target)
        push_u32(right_buffer)
        push_u32(right_descriptor)
        append_call(init_target)
        code.extend(b"\x6A\x01")
        push_u32(right_descriptor)
        push_u32(left_descriptor)
        append_call(equal_target)
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_runtime_string_services",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    init_target,
                    NATIVE_HOST_SERVICE_RUNTIME,
                    8,
                    NATIVE_HOST_SERVICE_RUNTIME_INIT_ANSI_STRING,
                ),
                NativeHostServiceEntry(
                    equal_target,
                    NATIVE_HOST_SERVICE_RUNTIME,
                    12,
                    NATIVE_HOST_SERVICE_RUNTIME_EQUAL_STRING,
                ),
            ]
        )

        def unexpected_handler(*_args: object) -> None:
            self.fail("native runtime string service crossed into Python")

        memory = SparseMemory({0x8000: 0})
        memory.write(left_buffer, b"Hello\0")
        memory.write(right_buffer, b"hELLo\0")
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                callback_addresses={init_target, equal_target},
            )
            state = CpuState.with_registers(eax=0xDEADBEEF, esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                call_handlers={
                    init_target: unexpected_handler,
                    equal_target: unexpected_handler,
                },
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=32,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(memory.read_u32(left_descriptor), 5 | (6 << 16))
        self.assertEqual(memory.read_u32(left_descriptor + 4), left_buffer)
        self.assertEqual(memory.read_u32(right_descriptor), 5 | (6 << 16))
        self.assertEqual(state.get_register("eax"), 1)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_host_service_call_count"], 3)
        self.assertEqual(performance["handler_call_count"], 0)

    def test_native_runtime_counted_string_conversions_bypass_python(
        self,
    ) -> None:
        base_address = 0x1000
        ansi_to_unicode_target = 0x2000
        unicode_to_ansi_target = 0x2010
        ansi_source_descriptor = 0x3000
        unicode_destination_descriptor = 0x3010
        ansi_destination_descriptor = 0x3020
        ansi_source_buffer = 0x3100
        unicode_destination_buffer = 0x3120
        ansi_destination_buffer = 0x3160
        ansi_to_unicode_status = 0x3200
        unicode_to_ansi_status = 0x3204
        ansi_payload = b"Save\x80"
        unicode_payload = "Save\N{EURO SIGN}".encode("utf-16-le")
        code = bytearray()

        def emit_call(target: int, arguments: tuple[int, ...]) -> None:
            for argument in reversed(arguments):
                code.extend(b"\x68" + struct.pack("<I", argument))
            call_address = base_address + len(code)
            code.extend(
                b"\xE8" + struct.pack("<i", target - (call_address + 5))
            )

        emit_call(
            ansi_to_unicode_target,
            (
                unicode_destination_descriptor,
                ansi_source_descriptor,
                0,
            ),
        )
        code.extend(b"\xA3" + struct.pack("<I", ansi_to_unicode_status))
        emit_call(
            unicode_to_ansi_target,
            (
                ansi_destination_descriptor,
                unicode_destination_descriptor,
                0,
            ),
        )
        code.extend(b"\xA3" + struct.pack("<I", unicode_to_ansi_status))
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_runtime_counted_string_conversions",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    ansi_to_unicode_target,
                    NATIVE_HOST_SERVICE_RUNTIME,
                    12,
                    NATIVE_HOST_SERVICE_RUNTIME_ANSI_STRING_TO_UNICODE_STRING,
                ),
                NativeHostServiceEntry(
                    unicode_to_ansi_target,
                    NATIVE_HOST_SERVICE_RUNTIME,
                    12,
                    NATIVE_HOST_SERVICE_RUNTIME_UNICODE_STRING_TO_ANSI_STRING,
                ),
            ]
        )
        memory = SparseMemory({0x8000: 0})
        memory.write(
            ansi_source_descriptor,
            struct.pack(
                "<HHI",
                len(ansi_payload),
                len(ansi_payload) + 1,
                ansi_source_buffer,
            ),
        )
        memory.write(
            unicode_destination_descriptor,
            struct.pack("<HHI", 0, 32, unicode_destination_buffer),
        )
        memory.write(
            ansi_destination_descriptor,
            struct.pack("<HHI", 0, 16, ansi_destination_buffer),
        )
        memory.write(ansi_source_buffer, ansi_payload + b"\0")
        targets = {ansi_to_unicode_target, unicode_to_ansi_target}

        def unexpected_handler(*_args: object) -> None:
            self.fail("native counted-string conversion crossed into Python")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                callback_addresses=targets,
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                call_handlers={target: unexpected_handler for target in targets},
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=40,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(memory.read_u32(ansi_to_unicode_status), 0)
        self.assertEqual(memory.read_u32(unicode_to_ansi_status), 0)
        self.assertEqual(
            memory.read_u32(unicode_destination_descriptor),
            len(unicode_payload) | (32 << 16),
        )
        self.assertEqual(
            memory.read(
                unicode_destination_buffer,
                len(unicode_payload) + 2,
            ),
            unicode_payload + b"\0\0",
        )
        self.assertEqual(
            memory.read_u32(ansi_destination_descriptor),
            len(ansi_payload) | (16 << 16),
        )
        self.assertEqual(
            memory.read(ansi_destination_buffer, len(ansi_payload) + 1),
            ansi_payload + b"\0",
        )
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_host_service_call_count"], 2)
        self.assertEqual(performance["handler_call_count"], 0)

    def test_native_memory_service_owns_allocated_pages(self) -> None:
        base_address = 0x1000
        allocate_target = 0x2000
        physical_target = 0x2010
        lock_target = 0x2020
        code = bytearray()

        def push_u32(value: int) -> None:
            code.extend(b"\x68" + struct.pack("<I", value))

        def append_call(target: int) -> None:
            call_address = base_address + len(code)
            code.extend(b"\xE8" + struct.pack("<i", target - (call_address + 5)))

        push_u32(4)
        push_u32(0)
        push_u32(0xFFFFFFFF)
        push_u32(0)
        push_u32(0x98)
        append_call(allocate_target)
        code.extend(b"\x89\xC3")
        push_u32(0)
        push_u32(0x1000)
        code.extend(b"\x53")
        append_call(lock_target)
        code.extend(b"\x89\xD8")
        code.extend(b"\xC7\x00" + struct.pack("<I", 0x12345678))
        code.extend(b"\x50")
        append_call(physical_target)
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_memory_services",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    allocate_target,
                    NATIVE_HOST_SERVICE_MEMORY,
                    20,
                    NATIVE_HOST_SERVICE_MEMORY_ALLOCATE_CONTIGUOUS_EX,
                ),
                NativeHostServiceEntry(
                    physical_target,
                    NATIVE_HOST_SERVICE_MEMORY,
                    4,
                    NATIVE_HOST_SERVICE_MEMORY_GET_PHYSICAL_ADDRESS,
                ),
                NativeHostServiceEntry(
                    lock_target,
                    NATIVE_HOST_SERVICE_MEMORY,
                    12,
                    NATIVE_HOST_SERVICE_MEMORY_VALIDATE_RANGE,
                ),
            ]
        )

        def unexpected_handler(*_args: object) -> None:
            self.fail("native memory service crossed into Python")

        memory = SparseMemory({0x8000: 0})
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                callback_addresses={allocate_target, physical_target, lock_target},
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                call_handlers={
                    allocate_target: unexpected_handler,
                    physical_target: unexpected_handler,
                    lock_target: unexpected_handler,
                },
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=32,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 0x20000000)
        self.assertEqual(memory.read_u32(0x20000000), 0x12345678)
        self.assertEqual(services.allocation_count, 1)
        self.assertEqual(services.native_allocated_page_count, 2)
        self.assertEqual(services.native_page_allocation_failure_count, 0)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_host_service_call_count"], 3)
        self.assertEqual(performance["handler_call_count"], 0)

    def test_native_contiguous_memory_honors_fixed_physical_range(self) -> None:
        base_address = 0x1000
        allocate_target = 0x2000
        physical_target = 0x2010
        size = 0x5D000
        lowest_physical = 0x02877000
        highest_physical = lowest_physical + size - 1
        code = bytearray()

        for value in (4, 0, highest_physical, lowest_physical, size):
            code.extend(b"\x68" + struct.pack("<I", value))
        call_address = base_address + len(code)
        code.extend(
            b"\xE8" + struct.pack("<i", allocate_target - (call_address + 5))
        )
        code.extend(b"\x89\xC3")
        code.extend(b"\xC7\x00" + struct.pack("<I", 0x12345678))
        code.extend(b"\x50")
        call_address = base_address + len(code)
        code.extend(
            b"\xE8" + struct.pack("<i", physical_target - (call_address + 5))
        )
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_fixed_contiguous_memory",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    allocate_target,
                    NATIVE_HOST_SERVICE_MEMORY,
                    20,
                    NATIVE_HOST_SERVICE_MEMORY_ALLOCATE_CONTIGUOUS_EX,
                ),
                NativeHostServiceEntry(
                    physical_target,
                    NATIVE_HOST_SERVICE_MEMORY,
                    4,
                    NATIVE_HOST_SERVICE_MEMORY_GET_PHYSICAL_ADDRESS,
                ),
            ]
        )
        memory = SparseMemory({0x8000: 0})

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                callback_addresses={allocate_target, physical_target},
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=24,
            )

        guest_address = lowest_physical | 0x80000000
        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("ebx"), guest_address)
        self.assertEqual(state.get_register("eax"), lowest_physical)
        self.assertEqual(memory.read_u32(guest_address), 0x12345678)
        self.assertEqual(services.allocations[0].address, guest_address)
        self.assertEqual(services.allocations[0].size, size)

    def test_native_contiguous_memory_finds_space_in_broad_range(self) -> None:
        base_address = 0x1000
        allocate_target = 0x2000
        code = bytearray()

        def append_allocation(size: int) -> None:
            for value in (4, 0, 0x003FFFFF, 0, size):
                code.extend(b"\x68" + struct.pack("<I", value))
            call_address = base_address + len(code)
            code.extend(
                b"\xE8"
                + struct.pack("<i", allocate_target - (call_address + 5))
            )

        append_allocation(0x1800)
        code.extend(b"\x89\xC3")
        append_allocation(0x1000)
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_broad_contiguous_memory",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    allocate_target,
                    NATIVE_HOST_SERVICE_MEMORY,
                    20,
                    NATIVE_HOST_SERVICE_MEMORY_ALLOCATE_CONTIGUOUS_EX,
                )
            ]
        )
        memory = SparseMemory({0x8000: 0})

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                callback_addresses={allocate_target},
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=24,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("ebx"), 0x80000000)
        self.assertEqual(state.get_register("eax"), 0x80002000)

    def test_native_audio_effect_image_service_bypasses_python(self) -> None:
        base_address = 0x1000
        handler_target = 0x2000
        output_address = 0x3000
        workspace_address = 0x31FF0000
        code = bytearray()

        def push_u32(value: int) -> None:
            code.extend(b"\x68" + struct.pack("<I", value))

        push_u32(output_address)
        push_u32(0x54FC)
        push_u32(0x81890030)
        relative = handler_target - (base_address + len(code) + 5)
        code.extend(b"\xE8" + struct.pack("<i", relative))
        code.extend(b"\xB8" + struct.pack("<I", 0x54FC))
        code.extend(
            b"\xA3" + struct.pack("<I", workspace_address) + b"\x31\xC0\xC3"
        )
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_audio_effect_image",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    handler_target,
                    NATIVE_HOST_SERVICE_AUDIO,
                    12,
                    NATIVE_HOST_SERVICE_AUDIO_DIRECTSOUND_EFFECT_IMAGE,
                )
            ]
        )

        def unexpected_handler(*_args: object) -> None:
            self.fail("native audio service crossed into Python")

        memory = SparseMemory({0x8000: 0})
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                callback_addresses={handler_target},
                memory_write_callback_addresses={workspace_address},
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                call_handlers={handler_target: unexpected_handler},
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=16,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 0)
        self.assertEqual(memory.read_u32(output_address), workspace_address)
        self.assertEqual(memory.read_u32(workspace_address), 0x54FC)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_host_service_call_count"], 1)
        self.assertEqual(performance["handler_call_count"], 0)
        self.assertEqual(performance["write_u32_callback_count"], 0)

    def test_native_title_spin_delay_service_bypasses_python(self) -> None:
        base_address = 0x1000
        handler_target = 0x2000
        function = lift_x86_function(
            b"\xE8" + struct.pack("<i", handler_target - (base_address + 5)) + b"\xC3",
            base_address=base_address,
            symbol="native_title_spin_delay",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    handler_target,
                    NATIVE_HOST_SERVICE_TITLE,
                    0,
                    NATIVE_HOST_SERVICE_TITLE_SPIN_DELAY,
                )
            ]
        )

        def unexpected_handler(*_args: object) -> None:
            self.fail("native title service crossed into Python")

        memory = SparseMemory({0x8000: 0})
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                callback_addresses={handler_target},
            )
            state = CpuState.with_registers(eax=0xFFFFFFFF, esp=0x8000)
            state.timestamp_counter = 100
            returned_to = executor.run(
                state,
                memory,
                call_handlers={handler_target: unexpected_handler},
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=16,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 0)
        self.assertEqual(state.timestamp_counter, 500)
        self.assertTrue(state.flags.pf)
        self.assertFalse(state.flags.af)
        self.assertTrue(state.flags.zf)
        self.assertFalse(state.flags.sf)
        self.assertFalse(state.flags.of)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_host_service_call_count"], 1)
        self.assertEqual(performance["handler_call_count"], 0)

    def test_native_gpu_command_kick_publishes_completion_without_python(self) -> None:
        base_address = 0x1000
        submission_address = 0x002256C0
        dma_pointer_address = submission_address + 0x17F4
        dma_state_address = 0x00300000
        completion_address = dma_state_address + 0x44
        interrupt_status_address = 0xFD400100
        master_interrupt_status_address = 0xFD000100
        master_interrupt_result_address = 0x00301000
        submitted = 0x21B8C123
        code = bytearray()

        def store_u32(address: int, value: int) -> None:
            code.extend(b"\xC7\x05" + struct.pack("<II", address, value))

        store_u32(submission_address, submitted)
        store_u32(dma_pointer_address, dma_state_address)
        store_u32(0x80000000, 0xDEADBEEF)
        code.extend(b"\xA1" + struct.pack("<I", master_interrupt_status_address))
        code.extend(b"\xA3" + struct.pack("<I", master_interrupt_result_address))
        code.extend(b"\xA1" + struct.pack("<I", interrupt_status_address))
        store_u32(interrupt_status_address, 0xFFFFFFFF)
        code.extend(b"\xA1" + struct.pack("<I", completion_address) + b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_gpu_command_kick",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    0x2000,
                    NATIVE_HOST_SERVICE_RETURN_CONSTANT,
                    0,
                    0,
                )
            ]
        )
        memory = SparseMemory({0x8000: 0, interrupt_status_address: 0xFFFFFFFF})
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                memory_write_callback_addresses={
                    0x80000000,
                    interrupt_status_address,
                },
                memory_read_callback_addresses={master_interrupt_status_address},
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=16,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), submitted & 0x0FFFFFFF)
        self.assertEqual(memory.read_u32(completion_address), submitted & 0x0FFFFFFF)
        self.assertEqual(memory.read_u32(master_interrupt_result_address), 0x01000000)
        self.assertEqual(memory.read_u32(interrupt_status_address), 0)
        self.assertEqual(services.native_memory_write_count, 3)
        self.assertEqual(services.native_gpu_command_kick_count, 1)
        self.assertEqual(services.native_gpu_completion_count, 1)
        self.assertEqual(services.native_memory_read_count, 1)
        self.assertEqual(services.native_gpu_master_interrupt_count, 1)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["handler_call_count"], 0)
        self.assertEqual(performance["write_u32_callback_count"], 0)

    def test_native_pfifo_status_reads_publish_idle_without_python(self) -> None:
        base_address = 0x1000
        runout_status_address = 0xFD002400
        cache1_status_address = 0xFD003214
        first_result_address = 0x3000
        second_result_address = 0x3004
        first_byte_result_address = 0x3008
        second_byte_result_address = 0x300C
        code = (
            b"\xA1"
            + struct.pack("<I", runout_status_address)
            + b"\xA3"
            + struct.pack("<I", first_result_address)
            + b"\xA1"
            + struct.pack("<I", cache1_status_address)
            + b"\xA3"
            + struct.pack("<I", second_result_address)
            + b"\x0F\xB6\x05"
            + struct.pack("<I", runout_status_address)
            + b"\xA3"
            + struct.pack("<I", first_byte_result_address)
            + b"\x0F\xB6\x05"
            + struct.pack("<I", cache1_status_address)
            + b"\xA3"
            + struct.pack("<I", second_byte_result_address)
            + b"\xC3"
        )
        function = lift_x86_function(
            code,
            base_address=base_address,
            symbol="native_pfifo_idle_status",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    0x2000,
                    NATIVE_HOST_SERVICE_RETURN_CONSTANT,
                    0,
                    0,
                )
            ]
        )
        memory = SparseMemory(
            {
                0x8000: 0,
                runout_status_address: 0,
                cache1_status_address: 0,
            }
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                memory_read_callback_addresses={
                    runout_status_address,
                    cache1_status_address,
                },
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=16,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(memory.read_u32(first_result_address), 0x10)
        self.assertEqual(memory.read_u32(second_result_address), 0x10)
        self.assertEqual(memory.read_u32(first_byte_result_address), 0x10)
        self.assertEqual(memory.read_u32(second_byte_result_address), 0x10)
        self.assertEqual(services.native_memory_read_count, 4)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["handler_call_count"], 0)
        self.assertEqual(performance["read_u32_callback_count"], 0)

    def test_native_audio_voice_command_byte_bypasses_python(self) -> None:
        base_address = 0x1000
        command_address = 0xFEC0011B
        code = (
            b"\xC6\x05"
            + struct.pack("<I", command_address)
            + b"\x02"
            + b"\x0F\xB6\x05"
            + struct.pack("<I", command_address)
            + b"\xC3"
        )
        function = lift_x86_function(
            code,
            base_address=base_address,
            symbol="native_audio_voice_command_byte",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    0x2000,
                    NATIVE_HOST_SERVICE_RETURN_CONSTANT,
                    0,
                    0,
                )
            ]
        )
        memory = SparseMemory({0x8000: 0})
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                memory_read_callback_addresses={command_address},
                memory_write_callback_addresses={command_address},
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=8,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 0)
        self.assertEqual(memory.read(command_address, 1), b"\x00")
        self.assertEqual(services.native_memory_write_count, 1)
        self.assertEqual(services.native_memory_read_count, 1)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["read_u8_callback_count"], 0)
        self.assertEqual(performance["write_u8_callback_count"], 0)

    def test_native_page_misses_allocate_without_python(self) -> None:
        base_address = 0x1000
        read_address = 0x00900000
        write_address = 0x00901000
        code = (
            b"\xA1"
            + struct.pack("<I", read_address)
            + b"\xA3"
            + struct.pack("<I", write_address)
            + b"\xC3"
        )
        function = lift_x86_function(
            code,
            base_address=base_address,
            symbol="native_page_miss_allocation",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    0x2000,
                    NATIVE_HOST_SERVICE_RETURN_CONSTANT,
                    0,
                    0,
                )
            ]
        )
        memory = SparseMemory({0x8000: 0})
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=8,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(memory.read_u32(write_address), 0)
        self.assertGreaterEqual(services.native_page_miss_count, 2)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["read_u32_callback_count"], 0)
        self.assertEqual(performance["write_u32_callback_count"], 0)

    def test_native_title_asset_open_and_read_bypass_python(self) -> None:
        base_address = 0x1000
        open_target = 0x2000
        read_target = 0x2010
        path_address = 0x3000
        destination = 0x4000
        payload = b"native-title-asset"
        code = bytearray()

        def emit_call(target: int) -> None:
            call_address = base_address + len(code)
            code.extend(
                b"\xE8" + struct.pack("<i", target - (call_address + 5))
            )

        code.extend(b"\x6A\x01")
        code.extend(b"\x68" + struct.pack("<I", path_address))
        emit_call(open_target)
        code.extend(b"\x89\xC3\x89\xC1")
        code.extend(b"\x68" + struct.pack("<I", len(payload)))
        code.extend(b"\x68" + struct.pack("<I", destination))
        emit_call(read_target)
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_title_asset_open_and_read",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    open_target,
                    NATIVE_HOST_SERVICE_TITLE,
                    8,
                    NATIVE_HOST_SERVICE_TITLE_ASSET_OPEN,
                ),
                NativeHostServiceEntry(
                    read_target,
                    NATIVE_HOST_SERVICE_TITLE,
                    8,
                    NATIVE_HOST_SERVICE_TITLE_ASSET_READ,
                ),
            ]
        )
        memory = SparseMemory(
            {0x8000: 0, path_address: b"D:\\frontend\\global.dic\0"}
        )

        def unexpected_handler(*_args: object) -> None:
            self.fail("native title asset service crossed into Python")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            asset = root / "frontend" / "global.dic"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(payload)
            services.set_extracted_root(root)
            executor = NativeResumableExecutor(
                function,
                build_dir=root / "build",
                callback_addresses={open_target, read_target},
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                call_handlers={
                    open_target: unexpected_handler,
                    read_target: unexpected_handler,
                },
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=16,
            )

        object_address = state.get_register("ebx")
        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), len(payload))
        self.assertEqual(object_address, 0x31F10000)
        self.assertEqual(memory.read(destination, len(payload)), payload)
        self.assertEqual(memory.read_u32(object_address + 0x10), len(payload))
        self.assertEqual(memory.read_u32(object_address + 0x18), len(payload))
        self.assertEqual(memory.read_u32(object_address + 0x2C), 1)
        self.assertEqual(services.title_asset_stream_count, 1)
        self.assertEqual(services.title_asset_open_count, 1)
        self.assertEqual(services.title_asset_open_failure_count, 0)
        self.assertEqual(services.title_asset_payload_bytes, len(payload))
        self.assertEqual(services.title_asset_open_event_count, 1)
        self.assertEqual(services.title_asset_open_event_overflow_count, 0)
        open_event = services.title_asset_open_events[0]
        self.assertEqual(os.fsdecode(open_event.guest_path), "D:\\frontend\\global.dic")
        self.assertEqual(open_event.flip_count, 0)
        self.assertEqual(open_event.object, object_address)
        self.assertEqual(open_event.payload_size, len(payload))
        self.assertEqual(open_event.failure_stage, 0)
        self.assertEqual(open_event.flags, 3)
        self.assertEqual(open_event.read_call_count, 1)
        self.assertEqual(open_event.read_requested_bytes, len(payload))
        self.assertEqual(open_event.read_returned_bytes, len(payload))
        self.assertEqual(open_event.close_count, 0)
        self.assertEqual(
            bytes(open_event.header_words)[: len(payload)],
            payload,
        )
        self.assertEqual(services.title_track_pss_candidate_count, 0)
        self.assertEqual(services.title_track_pss_publication_count, 0)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_host_service_call_count"], 2)
        self.assertEqual(performance["handler_call_count"], 0)

    def test_native_title_asset_open_close_reuses_stream_beyond_capacity(
        self,
    ) -> None:
        base_address = 0x1000
        open_target = 0x2000
        close_target = 0x31F10200
        path_address = 0x3000
        payload = b"reusable-native-title-asset"
        code = bytearray()

        def emit_call(target: int) -> None:
            call_address = base_address + len(code)
            code.extend(
                b"\xE8" + struct.pack("<i", target - (call_address + 5))
            )

        for _ in range(129):
            code.extend(b"\x6A\x01")
            code.extend(b"\x68" + struct.pack("<I", path_address))
            emit_call(open_target)
            code.extend(b"\x89\xC1")
            emit_call(close_target)
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_title_asset_open_close_reuse",
            max_instructions=2048,
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    open_target,
                    NATIVE_HOST_SERVICE_TITLE,
                    8,
                    NATIVE_HOST_SERVICE_TITLE_ASSET_OPEN,
                ),
                NativeHostServiceEntry(
                    close_target,
                    NATIVE_HOST_SERVICE_TITLE,
                    0,
                    NATIVE_HOST_SERVICE_TITLE_ASSET_CLOSE,
                ),
            ]
        )
        memory = SparseMemory(
            {0x8000: 0, path_address: b"D:\\frontend\\global.dic\0"}
        )

        def unexpected_handler(*_args: object) -> None:
            self.fail("native title asset service crossed into Python")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            asset = root / "frontend" / "global.dic"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(payload)
            services.set_extracted_root(root)
            executor = NativeResumableExecutor(
                function,
                build_dir=root / "build",
                callback_addresses={open_target, close_target},
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                call_handlers={
                    open_target: unexpected_handler,
                    close_target: unexpected_handler,
                },
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=2048,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(services.title_asset_stream_count, 1)
        self.assertEqual(services.title_asset_open_count, 129)
        self.assertEqual(services.title_asset_close_count, 129)
        self.assertEqual(services.title_asset_reuse_count, 128)
        self.assertEqual(services.title_asset_open_failure_count, 0)
        self.assertEqual(services.title_asset_active_stream_count, 0)
        self.assertEqual(services.title_asset_peak_active_stream_count, 1)
        self.assertEqual(services.title_asset_open_event_count, 128)
        self.assertEqual(services.title_asset_open_event_overflow_count, 1)
        self.assertTrue(
            all(
                event.close_count == 1
                for event in services.title_asset_open_events
            )
        )
        self.assertEqual(memory.read_u32(0x31F10000), 0x31F10100)
        self.assertEqual(memory.read_u32(0x31F10104), close_target)
        self.assertEqual(
            services.title_asset_streams[0].payload_capacity,
            len(payload),
        )

    def test_native_title_asset_read_publishes_track_pss_image(self) -> None:
        base_address = 0x1000
        open_target = 0x2000
        read_target = 0x2010
        path_address = 0x3000
        destination = 0x4000
        image_base = 0x8381C000
        table_a_address = image_base + 0x200
        table_b_address = image_base + 0x220
        scene_record_address = image_base + 0x240
        descriptors = (
            (0x00000000, 0x0003C000),
            (0x0003C000, 0x00012345),
        )
        descriptor_payload = b"".join(
            struct.pack("<II", stream_offset, stream_length)
            for stream_offset, stream_length in descriptors
        )
        payload = bytearray(0x300)
        struct.pack_into("<I", payload, 0x14, image_base + 0x1C0)
        struct.pack_into("<II", payload, 0x18, 1, scene_record_address)
        struct.pack_into("<I", payload, 0x5C, len(descriptors))
        struct.pack_into("<I", payload, 0x60, table_a_address)
        struct.pack_into("<I", payload, 0x64, table_b_address)
        payload[0x200 : 0x200 + len(descriptor_payload)] = descriptor_payload
        payload[0x220 : 0x220 + len(descriptor_payload)] = descriptor_payload
        payload = bytes(payload)
        code = bytearray(b"\x6A\x01")
        code.extend(b"\x68" + struct.pack("<I", path_address))
        call_address = base_address + len(code)
        code.extend(
            b"\xE8" + struct.pack("<i", open_target - (call_address + 5))
        )
        code.extend(b"\x89\xC3\x89\xC1")
        code.extend(b"\x68" + struct.pack("<I", len(payload)))
        code.extend(b"\x68" + struct.pack("<I", destination))
        call_address = base_address + len(code)
        code.extend(
            b"\xE8" + struct.pack("<i", read_target - (call_address + 5))
        )
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_title_track_pss_publication",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    open_target,
                    NATIVE_HOST_SERVICE_TITLE,
                    8,
                    NATIVE_HOST_SERVICE_TITLE_ASSET_OPEN,
                ),
                NativeHostServiceEntry(
                    read_target,
                    NATIVE_HOST_SERVICE_TITLE,
                    8,
                    NATIVE_HOST_SERVICE_TITLE_ASSET_READ,
                ),
            ]
        )
        memory = SparseMemory(
            {0x8000: 0, path_address: b"D:\\trackpss\\forward\\test.pss\0"}
        )

        def unexpected_handler(*_args: object) -> None:
            self.fail("native track asset service crossed into Python")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            asset = root / "trackpss" / "forward" / "test.pss"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(payload)
            services.set_extracted_root(root)
            executor = NativeResumableExecutor(
                function,
                build_dir=root / "build",
                callback_addresses={open_target, read_target},
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                call_handlers={
                    open_target: unexpected_handler,
                    read_target: unexpected_handler,
                },
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=16,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), len(payload))
        self.assertEqual(memory.read(destination, len(payload)), payload)
        self.assertEqual(memory.read(image_base, len(payload)), payload)
        self.assertEqual(
            memory.read(table_a_address, len(descriptor_payload)),
            descriptor_payload,
        )
        self.assertEqual(
            memory.read(table_b_address, len(descriptor_payload)),
            descriptor_payload,
        )
        self.assertEqual(services.title_track_pss_candidate_count, 1)
        self.assertEqual(services.title_track_pss_publication_count, 1)
        self.assertEqual(services.title_track_pss_validation_failure_count, 0)
        self.assertEqual(services.title_track_pss_published_bytes, len(payload))
        self.assertEqual(
            services.title_track_pss_descriptor_entry_count,
            len(descriptors) * 2,
        )
        self.assertEqual(services.title_track_pss_scene_record_entry_count, 1)
        self.assertEqual(services.title_asset_streams[0].image_base, image_base)
        self.assertEqual(services.title_asset_streams[0].flags, 0x17)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_host_service_call_count"], 2)
        self.assertEqual(performance["handler_call_count"], 0)

    def test_native_title_asset_read_uses_fixed_traffic_allocation(self) -> None:
        base_address = 0x1000
        allocate_target = 0x1FF0
        open_target = 0x2000
        read_target = 0x2010
        path_address = 0x3000
        lowest_physical = 0x02877000
        payload = bytes(range(256)) * 48
        highest_physical = lowest_physical + len(payload) - 1
        guest_destination = lowest_physical | 0x80000000
        code = bytearray()
        for value in (4, 0, highest_physical, lowest_physical, len(payload)):
            code.extend(b"\x68" + struct.pack("<I", value))
        call_address = base_address + len(code)
        code.extend(
            b"\xE8" + struct.pack("<i", allocate_target - (call_address + 5))
        )
        code.extend(b"\x89\xC7")
        code.extend(b"\x6A\x01")
        code.extend(b"\x68" + struct.pack("<I", path_address))
        call_address = base_address + len(code)
        code.extend(
            b"\xE8" + struct.pack("<i", open_target - (call_address + 5))
        )
        code.extend(b"\x89\xC3\x89\xC1")
        code.extend(b"\x68" + struct.pack("<I", len(payload)))
        code.extend(b"\x57")
        call_address = base_address + len(code)
        code.extend(
            b"\xE8" + struct.pack("<i", read_target - (call_address + 5))
        )
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_title_traffic_fixed_allocation",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    allocate_target,
                    NATIVE_HOST_SERVICE_MEMORY,
                    20,
                    NATIVE_HOST_SERVICE_MEMORY_ALLOCATE_CONTIGUOUS_EX,
                ),
                NativeHostServiceEntry(
                    open_target,
                    NATIVE_HOST_SERVICE_TITLE,
                    8,
                    NATIVE_HOST_SERVICE_TITLE_ASSET_OPEN,
                ),
                NativeHostServiceEntry(
                    read_target,
                    NATIVE_HOST_SERVICE_TITLE,
                    8,
                    NATIVE_HOST_SERVICE_TITLE_ASSET_READ,
                ),
            ]
        )
        memory = SparseMemory(
            {0x8000: 0, path_address: b"D:\\Traffic\\Traffic.TRA\0"}
        )

        def unexpected_handler(*_args: object) -> None:
            self.fail("native traffic asset service crossed into Python")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            asset = root / "Traffic" / "Traffic.TRA"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(payload)
            services.set_extracted_root(root)
            executor = NativeResumableExecutor(
                function,
                build_dir=root / "build",
                callback_addresses={allocate_target, open_target, read_target},
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                call_handlers={
                    allocate_target: unexpected_handler,
                    open_target: unexpected_handler,
                    read_target: unexpected_handler,
                },
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=32,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(memory.read(guest_destination, len(payload)), payload)
        self.assertEqual(services.title_traffic_tra_candidate_count, 1)
        self.assertEqual(services.title_asset_streams[0].image_base, 0)
        self.assertEqual(services.title_asset_streams[0].flags, 0x18)

    def test_native_title_audio_state_services_bypass_python(self) -> None:
        base_address = 0x1000
        special_target = 0x2000
        music_target = 0x2010
        music_holder = 0x3000
        code = bytearray(b"\xB9" + struct.pack("<I", 0x443FA0))
        code.extend(b"\x6A\x00")
        call_address = base_address + len(code)
        code.extend(
            b"\xE8" + struct.pack("<i", special_target - (call_address + 5))
        )
        code.extend(b"\x89\xC3")
        code.extend(b"\xB9" + struct.pack("<I", music_holder))
        code.extend(b"\x6A\x02")
        call_address = base_address + len(code)
        code.extend(
            b"\xE8" + struct.pack("<i", music_target - (call_address + 5))
        )
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_title_audio_state_services",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    special_target,
                    NATIVE_HOST_SERVICE_TITLE,
                    4,
                    NATIVE_HOST_SERVICE_TITLE_FRONTEND_SPECIAL_AUDIO_CREATE,
                ),
                NativeHostServiceEntry(
                    music_target,
                    NATIVE_HOST_SERVICE_TITLE,
                    4,
                    NATIVE_HOST_SERVICE_TITLE_MUSIC_MODE_SET,
                ),
            ]
        )
        memory = SparseMemory({0x8000: 0, music_holder: 0})

        def unexpected_handler(*_args: object) -> None:
            self.fail("native title audio state service crossed into Python")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                callback_addresses={special_target, music_target},
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                call_handlers={
                    special_target: unexpected_handler,
                    music_target: unexpected_handler,
                },
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=20,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("ebx"), 0x31F10400)
        self.assertEqual(memory.read_u32(0x31F10400), 1)
        self.assertEqual(memory.read_u32(0x31F10410), 0x31F1040C)
        self.assertEqual(memory.read_u32(music_holder), 0x31FE0000)
        self.assertEqual(memory.read_u32(0x31FE0038), 2)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_host_service_call_count"], 2)
        self.assertEqual(performance["handler_call_count"], 0)

    def test_normal_runtime_title_audio_shortcuts_do_not_synthesize_playback(self) -> None:
        base_address = 0x1000
        special_target = 0x2000
        music_target = 0x2010
        music_holder = 0x3000
        code = bytearray(b"\xB9" + struct.pack("<I", 0x443FA0))
        code.extend(b"\x6A\x00")
        call_address = base_address + len(code)
        code.extend(
            b"\xE8" + struct.pack("<i", special_target - (call_address + 5))
        )
        code.extend(b"\xB9" + struct.pack("<I", music_holder))
        code.extend(b"\x6A\x02")
        call_address = base_address + len(code)
        code.extend(
            b"\xE8" + struct.pack("<i", music_target - (call_address + 5))
        )
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_normal_runtime_audio_decode",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    special_target,
                    NATIVE_HOST_SERVICE_TITLE,
                    4,
                    NATIVE_HOST_SERVICE_TITLE_FRONTEND_SPECIAL_AUDIO_CREATE,
                ),
                NativeHostServiceEntry(
                    music_target,
                    NATIVE_HOST_SERVICE_TITLE,
                    4,
                    NATIVE_HOST_SERVICE_TITLE_MUSIC_MODE_SET,
                ),
            ]
        )
        services.enable_normal_runtime(scheduler_quantum=100)
        memory = SparseMemory({0x8000: 0, music_holder: 0})

        pcm = struct.pack("<hhhh", -32768, -1, 0, 32767)
        file_header_data = bytearray(0x44)
        file_header = struct.pack(
            "<III", 0x80A, len(file_header_data), 0x1003FFFF
        ) + file_header_data
        stream_header = bytearray(0x0C + 0xA0)
        struct.pack_into("<III", stream_header, 0, 0x803, 0xA0, 0x1003FFFF)
        struct.pack_into("<I", stream_header, 0x10, 22050)
        struct.pack_into("<I", stream_header, 0x18, len(pcm))
        stream_header[0x1D] = 1
        struct.pack_into("<I", stream_header, 0x2C, 0xD01BD217)
        stream_data = struct.pack(
            "<III", 0x804, len(pcm), 0x1003FFFF
        ) + pcm
        stream_payload = bytes(stream_header) + stream_data
        stream = struct.pack(
            "<III", 0x802, len(stream_payload), 0x1003FFFF
        ) + stream_payload
        file_data_payload = struct.pack("<I", 1) + stream
        file_data = struct.pack(
            "<III", 0x80C, len(file_data_payload), 0x1003FFFF
        ) + file_data_payload
        special_payload = struct.pack(
            "<III", 0x809, len(file_header + file_data), 0x1003FFFF
        ) + file_header + file_data

        music_header_payload = bytearray(0xE8)
        struct.pack_into("<I", music_header_payload, 0x40 - 0x18, 1)
        struct.pack_into("<I", music_header_payload, 0x4C - 0x18, 0xA0)
        struct.pack_into("<I", music_header_payload, 0x90 - 0x18, 0xA0)
        struct.pack_into("<I", music_header_payload, 0x98 - 0x18, 72)
        struct.pack_into(
            "<7I",
            music_header_payload,
            0xC8 - 0x18,
            7,
            0xA0,
            0,
            0x00040004,
            0,
            72,
            0,
        )
        struct.pack_into("<I", music_header_payload, 0xE4 - 0x18, 48000)
        music_header = struct.pack(
            "<III", 0x80E, len(music_header_payload), 0x1003FFFF
        ) + music_header_payload
        encoded = struct.pack("<hBB", 0, 0, 0) * 2 + bytes(64)
        music_packet = encoded + bytes(0xA0 - len(encoded))
        music_data = struct.pack(
            "<III", 0x80F, len(music_packet), 0x1003FFFF
        ) + music_packet
        music_body = music_header + music_data
        music_payload = struct.pack(
            "<III", 0x80D, len(music_body), 0x1003FFFF
        ) + music_body

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            (root / "audio").mkdir()
            (root / "music0").mkdir()
            (root / "audio" / "special.rws").write_bytes(special_payload)
            (root / "music0" / "trk07menust.rws").write_bytes(music_payload)
            services.set_extracted_root(root)
            executor = NativeResumableExecutor(
                function,
                build_dir=root / "build",
                callback_addresses={special_target, music_target},
            )
            returned_to = executor.run(
                CpuState.with_registers(esp=0x8000),
                memory,
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=20,
            )
            executor.shutdown_normal_runtime(services)

        self.assertEqual(returned_to, 0)
        self.assertEqual(services.native_audio_decoded_special_clip_count, 0)
        self.assertEqual(services.native_audio_decoded_music_track_count, 0)
        self.assertEqual(services.native_audio_decode_failure_count, 0)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["handler_call_count"], 0)

    def test_native_audio_buffer_play_completes_guest_abi_without_sdk_forward(self) -> None:
        base_address = 0x1000
        set_data_target = 0x2000
        set_format_target = 0x2010
        play_target = 0x2020
        set_volume_target = 0x2030
        set_frequency_target = 0x2040
        buffer = 0x5000
        buffer_descriptor = 0x5400
        voice = 0x5800
        data_address = 0x6000
        format_address = 0x7000
        code = bytearray()

        def push(value: int) -> None:
            code.extend(b"\x68" + struct.pack("<I", value))

        def call(target: int) -> None:
            call_address = base_address + len(code)
            code.extend(b"\xE8" + struct.pack("<i", target - (call_address + 5)))

        push(16)
        push(data_address)
        push(buffer)
        call(set_data_target)
        push(format_address)
        push(buffer)
        call(set_format_target)
        push(0xFFFFFDA8)
        push(buffer)
        call(set_volume_target)
        push(11025)
        push(buffer)
        call(set_frequency_target)
        push(0)
        push(0)
        push(0)
        push(buffer)
        call(play_target)
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_audio_buffer_observer_caller",
        )
        target_functions = (
            lift_x86_function(
                b"\xB8\x11\x11\x00\x00\xC2\x0C\x00",
                base_address=set_data_target,
                symbol="native_audio_set_data_original",
            ),
            lift_x86_function(
                b"\xB8\x22\x22\x00\x00\xC2\x08\x00",
                base_address=set_format_target,
                symbol="native_audio_set_format_original",
            ),
            lift_x86_function(
                b"\xB8\x33\x33\x00\x00\xC2\x10\x00",
                base_address=play_target,
                symbol="native_audio_play_original",
            ),
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    set_data_target,
                    NATIVE_HOST_SERVICE_AUDIO,
                    12,
                    NATIVE_HOST_SERVICE_AUDIO_BUFFER_SET_DATA,
                ),
                NativeHostServiceEntry(
                    set_format_target,
                    NATIVE_HOST_SERVICE_AUDIO,
                    8,
                    NATIVE_HOST_SERVICE_AUDIO_BUFFER_SET_FORMAT,
                ),
                NativeHostServiceEntry(
                    play_target,
                    NATIVE_HOST_SERVICE_AUDIO,
                    16,
                    NATIVE_HOST_SERVICE_AUDIO_BUFFER_PLAY,
                ),
                NativeHostServiceEntry(
                    set_volume_target,
                    NATIVE_HOST_SERVICE_AUDIO,
                    8,
                    NATIVE_HOST_SERVICE_AUDIO_BUFFER_SET_VOLUME,
                ),
                NativeHostServiceEntry(
                    set_frequency_target,
                    NATIVE_HOST_SERVICE_AUDIO,
                    8,
                    NATIVE_HOST_SERVICE_AUDIO_BUFFER_SET_FREQUENCY,
                ),
            ]
        )
        services.enable_normal_runtime(scheduler_quantum=100)
        memory = SparseMemory({0x8000: 0})
        memory.write_u32(buffer, buffer_descriptor)
        memory.write_u32(buffer + 4, voice)
        memory.write_u32(buffer_descriptor + 0xC0, 4)
        memory.write_u32(buffer_descriptor + 0xC4, 8)
        memory.write_u32(buffer_descriptor + 0xC8, 0)
        memory.write_u32(buffer_descriptor + 0xCC, 8)
        memory.write(voice + 0x11, b"\xA5\x01\x80\x5A")
        memory.write(
            data_address,
            struct.pack("<hhhhhhhh", -32768, -1, 100, 200, 300, 400, 0, 32767),
        )
        memory.write(
            format_address,
            struct.pack("<HHIIHHH", 1, 1, 22050, 44100, 2, 16, 0),
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                callback_addresses={
                    set_data_target,
                    set_format_target,
                    set_volume_target,
                    set_frequency_target,
                    play_target,
                },
                forwarded_callback_addresses={
                    set_data_target,
                    set_format_target,
                },
                module_functions=(function, *target_functions),
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=100,
            )
            executor.shutdown_normal_runtime(services)

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 0)
        self.assertEqual(state.get_register("esp"), 0x8004)
        self.assertEqual(memory.read(voice + 0x11, 4), b"\xA5\x03\x00\x5A")
        self.assertEqual(services.native_audio_buffer_data_count, 1)
        self.assertEqual(services.native_audio_buffer_format_count, 1)
        self.assertEqual(services.native_audio_buffer_volume_count, 1)
        self.assertEqual(services.native_audio_buffer_frequency_count, 1)
        self.assertEqual(services.native_audio_buffer_play_count, 1)
        self.assertEqual(services.native_audio_decoded_buffer_count, 1)
        self.assertEqual(services.native_audio_decode_failure_count, 0)
        self.assertEqual(services.native_audio_last_data, data_address + 4)
        self.assertEqual(services.native_audio_last_size, 8)
        self.assertEqual(services.native_audio_last_volume, -600)
        self.assertEqual(services.native_audio_last_frequency, 11025)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_host_service_call_count"], 5)
        self.assertEqual(performance["handler_call_count"], 0)

    def test_native_audio_failed_play_does_not_leave_voice_permanently_playing(
        self,
    ) -> None:
        base_address = 0x1000
        set_data_target = 0x2000
        set_format_target = 0x2010
        play_target = 0x2020
        get_status_target = 0x2030
        buffer = 0x5000
        buffer_descriptor = 0x5400
        voice = 0x5800
        invalid_data_address = 0x6000
        valid_data_address = 0x6100
        adpcm_format_address = 0x7000
        pcm_format_address = 0x7100
        status_output = 0x7200
        code = bytearray()

        def push(value: int) -> None:
            code.extend(b"\x68" + struct.pack("<I", value))

        def call(target: int) -> None:
            call_address = base_address + len(code)
            code.extend(b"\xE8" + struct.pack("<i", target - (call_address + 5)))

        push(36)
        push(invalid_data_address)
        push(buffer)
        call(set_data_target)
        push(adpcm_format_address)
        push(buffer)
        call(set_format_target)
        push(1)
        push(0)
        push(0)
        push(buffer)
        call(play_target)
        push(36)
        push(valid_data_address)
        push(buffer)
        call(set_data_target)
        push(pcm_format_address)
        push(buffer)
        call(set_format_target)
        push(1)
        push(0)
        push(0)
        push(buffer)
        call(play_target)
        push(status_output)
        push(buffer)
        call(get_status_target)
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_audio_failed_play_retry_caller",
        )
        target_functions = (
            lift_x86_function(
                b"\x31\xC0\xC2\x0C\x00",
                base_address=set_data_target,
                symbol="native_audio_failed_play_set_data_original",
            ),
            lift_x86_function(
                b"\x31\xC0\xC2\x08\x00",
                base_address=set_format_target,
                symbol="native_audio_failed_play_set_format_original",
            ),
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    set_data_target,
                    NATIVE_HOST_SERVICE_AUDIO,
                    12,
                    NATIVE_HOST_SERVICE_AUDIO_BUFFER_SET_DATA,
                ),
                NativeHostServiceEntry(
                    set_format_target,
                    NATIVE_HOST_SERVICE_AUDIO,
                    8,
                    NATIVE_HOST_SERVICE_AUDIO_BUFFER_SET_FORMAT,
                ),
                NativeHostServiceEntry(
                    play_target,
                    NATIVE_HOST_SERVICE_AUDIO,
                    16,
                    NATIVE_HOST_SERVICE_AUDIO_BUFFER_PLAY,
                ),
                NativeHostServiceEntry(
                    get_status_target,
                    NATIVE_HOST_SERVICE_AUDIO,
                    8,
                    NATIVE_HOST_SERVICE_AUDIO_BUFFER_GET_STATUS,
                ),
            ]
        )
        services.enable_normal_runtime(scheduler_quantum=100)
        memory = SparseMemory({0x8000: 0})
        memory.write_u32(buffer, buffer_descriptor)
        memory.write_u32(buffer + 4, voice)
        memory.write_u32(buffer_descriptor + 0xC0, 0)
        memory.write_u32(buffer_descriptor + 0xC4, 36)
        memory.write_u32(buffer_descriptor + 0xC8, 0)
        memory.write_u32(buffer_descriptor + 0xCC, 36)
        memory.write_u32(voice + 0x12, 1)
        invalid_adpcm = bytearray(36)
        invalid_adpcm[2] = 89
        memory.write(invalid_data_address, invalid_adpcm)
        memory.write(valid_data_address, struct.pack("<18h", *range(18)))
        memory.write(
            adpcm_format_address,
            struct.pack("<HHIIHHH", 0x69, 1, 48000, 24000, 36, 4, 64),
        )
        memory.write(
            pcm_format_address,
            struct.pack("<HHIIHHH", 1, 1, 22050, 44100, 2, 16, 0),
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                callback_addresses={
                    set_data_target,
                    set_format_target,
                    play_target,
                    get_status_target,
                },
                forwarded_callback_addresses={set_data_target, set_format_target},
                module_functions=(function, *target_functions),
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=200,
            )
            executor.shutdown_normal_runtime(services)

        self.assertEqual(returned_to, 0)
        self.assertEqual(memory.read_u32(status_output), 0)
        self.assertEqual(memory.read_u32(voice + 0x12), 1)
        self.assertEqual(services.native_audio_buffer_play_count, 2)
        self.assertEqual(services.native_audio_decoded_buffer_count, 1)
        self.assertEqual(services.native_audio_decode_failure_count, 1)
        self.assertEqual(services.native_audio_buffer_repeated_play_count, 0)
        self.assertEqual(services.native_audio_buffer_get_status_count, 1)
        self.assertEqual(services.native_audio_stale_playing_repair_count, 1)

    def test_native_audio_buffer_position_boundaries_complete_without_sdk_forward(
        self,
    ) -> None:
        base_address = 0x1000
        set_data_target = 0x2000
        set_format_target = 0x2010
        set_position_target = 0x2020
        get_position_target = 0x2030
        buffer = 0x5000
        buffer_descriptor = 0x5400
        data_address = 0x6000
        format_address = 0x7000
        play_cursor_output = 0x9000
        write_cursor_output = 0x9004
        code = bytearray()

        def push(value: int) -> None:
            code.extend(b"\x68" + struct.pack("<I", value))

        def call(target: int) -> None:
            call_address = base_address + len(code)
            code.extend(b"\xE8" + struct.pack("<i", target - (call_address + 5)))

        push(8)
        push(data_address)
        push(buffer)
        call(set_data_target)
        push(format_address)
        push(buffer)
        call(set_format_target)
        push(4)
        push(buffer)
        call(set_position_target)
        push(write_cursor_output)
        push(play_cursor_output)
        push(buffer)
        call(get_position_target)
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_audio_buffer_position_caller",
        )
        target_functions = (
            lift_x86_function(
                b"\xB8\x11\x11\x00\x00\xC2\x0C\x00",
                base_address=set_data_target,
                symbol="native_audio_position_set_data_original",
            ),
            lift_x86_function(
                b"\xB8\x22\x22\x00\x00\xC2\x08\x00",
                base_address=set_format_target,
                symbol="native_audio_position_set_format_original",
            ),
            lift_x86_function(
                b"\xB8\x33\x33\x00\x00\xC2\x08\x00",
                base_address=set_position_target,
                symbol="native_audio_set_position_original",
            ),
            lift_x86_function(
                b"\xB8\x44\x44\x00\x00\xC2\x0C\x00",
                base_address=get_position_target,
                symbol="native_audio_get_position_original",
            ),
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    set_data_target,
                    NATIVE_HOST_SERVICE_AUDIO,
                    12,
                    NATIVE_HOST_SERVICE_AUDIO_BUFFER_SET_DATA,
                ),
                NativeHostServiceEntry(
                    set_format_target,
                    NATIVE_HOST_SERVICE_AUDIO,
                    8,
                    NATIVE_HOST_SERVICE_AUDIO_BUFFER_SET_FORMAT,
                ),
                NativeHostServiceEntry(
                    set_position_target,
                    NATIVE_HOST_SERVICE_AUDIO,
                    8,
                    NATIVE_HOST_SERVICE_AUDIO_BUFFER_SET_POSITION,
                ),
                NativeHostServiceEntry(
                    get_position_target,
                    NATIVE_HOST_SERVICE_AUDIO,
                    12,
                    NATIVE_HOST_SERVICE_AUDIO_BUFFER_GET_POSITION,
                ),
            ]
        )
        services.enable_normal_runtime(scheduler_quantum=100)
        memory = SparseMemory({0x8000: 0})
        memory.write_u32(buffer, buffer_descriptor)
        memory.write_u32(buffer_descriptor + 0xC4, 8)
        memory.write(
            format_address,
            struct.pack("<HHIIHHH", 1, 1, 22050, 44100, 2, 16, 0),
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                callback_addresses={
                    set_data_target,
                    set_format_target,
                    set_position_target,
                    get_position_target,
                },
                forwarded_callback_addresses={set_data_target, set_format_target},
                module_functions=(function, *target_functions),
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=100,
            )
            executor.shutdown_normal_runtime(services)

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 0)
        self.assertEqual(state.get_register("esp"), 0x8004)
        self.assertEqual(memory.read_u32(play_cursor_output), 4)
        self.assertEqual(memory.read_u32(write_cursor_output), 6)
        self.assertEqual(services.native_audio_buffer_set_position_count, 1)
        self.assertEqual(services.native_audio_buffer_get_position_count, 1)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_host_service_call_count"], 4)
        self.assertEqual(performance["handler_call_count"], 0)

    def test_native_audio_buffer_create_captures_initial_descriptor_format(self) -> None:
        base_address = 0x1000
        create_target = 0x2000
        play_target = 0x2020
        buffer = 0x9000
        voice = 0x9100
        buffer_descriptor = 0x9200
        descriptor = 0x5000
        output_address = 0x5100
        format_address = 0x5200
        data_address = 0x5300
        code = bytearray()

        def push(value: int) -> None:
            code.extend(b"\x68" + struct.pack("<I", value))

        def call(target: int) -> None:
            call_address = base_address + len(code)
            code.extend(b"\xE8" + struct.pack("<i", target - (call_address + 5)))

        push(0)
        push(output_address)
        push(descriptor)
        push(0)
        call(create_target)
        push(1)
        push(0)
        push(0)
        push(buffer)
        call(play_target)
        push(1)
        push(0)
        push(0)
        push(buffer)
        call(play_target)
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_audio_buffer_create_observer_caller",
        )
        target_functions = (
            lift_x86_function(
                b"\x8B\x44\x24\x0C"
                b"\xC7\x00\x00\x90\x00\x00"
                b"\x31\xC0\xC2\x10\x00",
                base_address=create_target,
                symbol="native_audio_buffer_create_original",
            ),
            lift_x86_function(
                b"\xB8\x22\x22\x00\x00\xC2\x10\x00",
                base_address=play_target,
                symbol="native_audio_buffer_play_original",
            ),
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    create_target,
                    NATIVE_HOST_SERVICE_AUDIO,
                    16,
                    NATIVE_HOST_SERVICE_AUDIO_BUFFER_CREATE,
                ),
                NativeHostServiceEntry(
                    play_target,
                    NATIVE_HOST_SERVICE_AUDIO,
                    16,
                    NATIVE_HOST_SERVICE_AUDIO_BUFFER_PLAY,
                ),
            ]
        )
        services.enable_normal_runtime(scheduler_quantum=100)
        memory = SparseMemory({0x8000: 0})
        memory.write_u32(descriptor, 0x18)
        memory.write_u32(buffer, buffer_descriptor)
        memory.write_u32(buffer + 4, voice)
        memory.write_u32(voice + 0x12, 1)
        memory.write_u32(buffer_descriptor + 0xC0, 0)
        memory.write_u32(buffer_descriptor + 0xC4, 8)
        memory.write_u32(buffer_descriptor + 0xC8, 2)
        memory.write_u32(buffer_descriptor + 0xCC, 4)
        memory.write_u32(descriptor + 8, data_address)
        memory.write_u32(descriptor + 0xC, 8)
        memory.write_u32(descriptor + 0x10, format_address)
        memory.write(
            format_address,
            struct.pack("<HHIIHHH", 1, 1, 22050, 44100, 2, 16, 0),
        )
        memory.write(data_address, struct.pack("<hhhh", -32768, -1, 0, 32767))

        callback_addresses = {create_target, play_target}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                callback_addresses=callback_addresses,
                forwarded_callback_addresses={create_target},
                module_functions=(function, *target_functions),
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=100,
            )
            executor.shutdown_normal_runtime(services)

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 0)
        self.assertEqual(memory.read_u32(output_address), buffer)
        self.assertEqual(memory.read_u32(voice + 0x12), 3)
        self.assertEqual(services.native_audio_buffer_create_count, 1)
        self.assertEqual(services.native_audio_buffer_play_count, 2)
        self.assertEqual(services.native_audio_buffer_repeated_play_count, 0)
        self.assertEqual(services.native_audio_decoded_buffer_count, 2)
        self.assertEqual(services.native_audio_decode_failure_count, 0)
        self.assertEqual(services.native_audio_last_data, data_address)
        self.assertEqual(services.native_audio_last_size, 8)
        self.assertEqual(services.native_audio_largest_loop_play_length, 8)
        self.assertEqual(services.native_audio_largest_loop_start, 2)
        self.assertEqual(services.native_audio_largest_loop_length, 4)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_host_service_call_count"], 3)
        self.assertEqual(performance["handler_call_count"], 0)

    def test_native_audio_buffer_stop_ex_completes_without_sdk_forward(
        self,
    ) -> None:
        base_address = 0x1000
        stop_ex_target = 0x2000
        buffer = 0x5000
        voice = 0x5800
        code = bytearray()

        def push(value: int) -> None:
            code.extend(b"\x68" + struct.pack("<I", value))

        push(0)
        push(0x11223344)
        push(0x55667788)
        push(buffer)
        call_address = base_address + len(code)
        code.extend(
            b"\xE8" + struct.pack("<i", stop_ex_target - (call_address + 5))
        )
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_audio_buffer_stop_ex_caller",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    stop_ex_target,
                    NATIVE_HOST_SERVICE_AUDIO,
                    16,
                    NATIVE_HOST_SERVICE_AUDIO_BUFFER_STOP_EX,
                )
            ]
        )
        services.enable_normal_runtime(scheduler_quantum=100)
        memory = SparseMemory({0x8000: 0})
        memory.write_u32(buffer + 4, voice)
        memory.write(voice + 0x11, b"\xA5\x03\x80\x5A")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                callback_addresses={stop_ex_target},
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=20,
            )
            executor.shutdown_normal_runtime(services)

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 0)
        self.assertEqual(state.get_register("esp"), 0x8004)
        self.assertEqual(memory.read(voice + 0x11, 4), b"\xA5\x01\x00\x5A")
        self.assertEqual(services.native_audio_buffer_stop_count, 1)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_host_service_call_count"], 1)
        self.assertEqual(performance["handler_call_count"], 0)

    def test_native_audio_stream_observers_follow_created_guest_stream(self) -> None:
        base_address = 0x1000
        create_target = 0x2000
        process_target = 0x2020
        flush_target = 0x2040
        stream = 0x9000
        descriptor = 0x5000
        format_address = 0x5100
        output_address = 0x5200
        packet = 0x5300
        data_address = 0x5400
        code = bytearray()

        def push(value: int) -> None:
            code.extend(b"\x68" + struct.pack("<I", value))

        def call(target: int) -> None:
            call_address = base_address + len(code)
            code.extend(b"\xE8" + struct.pack("<i", target - (call_address + 5)))

        push(0)
        push(output_address)
        push(descriptor)
        push(0)
        call(create_target)
        push(0)
        push(packet)
        push(stream)
        call(process_target)
        push(stream)
        call(flush_target)
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_audio_stream_observer_caller",
        )
        target_functions = (
            lift_x86_function(
                b"\x8B\x44\x24\x0C"
                b"\xC7\x00\x00\x90\x00\x00"
                b"\x31\xC0\xC2\x10\x00",
                base_address=create_target,
                symbol="native_audio_stream_create_original",
            ),
            lift_x86_function(
                b"\xB8\x22\x22\x00\x00\xC2\x0C\x00",
                base_address=process_target,
                symbol="native_audio_stream_process_original",
            ),
            lift_x86_function(
                b"\xB8\x44\x44\x00\x00\xC2\x04\x00",
                base_address=flush_target,
                symbol="native_audio_stream_flush_original",
            ),
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    create_target,
                    NATIVE_HOST_SERVICE_AUDIO,
                    16,
                    NATIVE_HOST_SERVICE_AUDIO_STREAM_CREATE,
                ),
                NativeHostServiceEntry(
                    process_target,
                    NATIVE_HOST_SERVICE_AUDIO,
                    12,
                    NATIVE_HOST_SERVICE_AUDIO_STREAM_PROCESS,
                ),
                NativeHostServiceEntry(
                    flush_target,
                    NATIVE_HOST_SERVICE_AUDIO,
                    4,
                    NATIVE_HOST_SERVICE_AUDIO_STREAM_FLUSH,
                ),
            ]
        )
        services.enable_normal_runtime(scheduler_quantum=100)
        memory = SparseMemory({0x8000: 0})
        memory.write_u32(descriptor + 8, format_address)
        memory.write(
            format_address,
            struct.pack("<HHIIHHH", 1, 1, 22050, 44100, 2, 16, 0),
        )
        memory.write_u32(packet, data_address)
        memory.write_u32(packet + 4, 8)
        memory.write(data_address, struct.pack("<hhhh", -32768, -1, 0, 32767))

        callback_addresses = {create_target, process_target, flush_target}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                callback_addresses=callback_addresses,
                forwarded_callback_addresses=callback_addresses,
                module_functions=(function, *target_functions),
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=100,
            )
            executor.shutdown_normal_runtime(services)

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 0x4444)
        self.assertEqual(memory.read_u32(output_address), stream)
        self.assertEqual(services.native_audio_stream_create_count, 1)
        self.assertEqual(services.native_audio_stream_process_count, 1)
        self.assertEqual(services.native_audio_stream_flush_count, 1)
        self.assertEqual(services.native_audio_stream_packet_byte_count, 8)
        self.assertEqual(services.native_audio_decoded_stream_packet_count, 1)
        self.assertEqual(services.native_audio_decode_failure_count, 0)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_host_service_call_count"], 3)
        self.assertEqual(performance["handler_call_count"], 0)

    def test_native_bootstrap_interrupt_vector_bypasses_python(self) -> None:
        base_address = 0x1000
        handler_target = 0x2000
        code = bytearray(b"\x6A\x00\x6A\x01")
        code.extend(
            b"\xE8"
            + struct.pack("<i", handler_target - (base_address + len(code) + 5))
        )
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_bootstrap_interrupt_vector",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    handler_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    8,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_HAL_GET_INTERRUPT_VECTOR,
                )
            ]
        )

        def unexpected_handler(*_args: object) -> None:
            self.fail("native bootstrap service crossed into Python")

        memory = SparseMemory({0x8000: 0})
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
                call_handlers={handler_target: unexpected_handler},
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=8,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 0x21)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_host_service_call_count"], 1)
        self.assertEqual(performance["handler_call_count"], 0)

    def test_native_av_state_services_bypass_python(self) -> None:
        base_address = 0x1000
        get_target = 0x2000
        display_target = 0x2010
        set_target = 0x2020
        saved_data_address = 0x12345000
        code = bytearray()

        def emit_call(target: int) -> None:
            call_address = base_address + len(code)
            code.extend(
                b"\xE8" + struct.pack("<i", target - (call_address + 5))
            )

        code.extend(b"\x68" + struct.pack("<I", saved_data_address))
        emit_call(set_target)
        emit_call(get_target)
        code.extend(b"\x89\xC3")
        display_arguments = (
            0xFD000000,
            0,
            0x040F0D0F,
            0x12,
            0xA00,
            0x1D94000,
        )
        for argument in reversed(display_arguments):
            code.extend(b"\x68" + struct.pack("<I", argument))
        emit_call(display_target)
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_av_state_services",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    get_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    0,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_AV_GET_SAVED_DATA_ADDRESS,
                ),
                NativeHostServiceEntry(
                    display_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    24,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_AV_SET_DISPLAY_MODE,
                ),
                NativeHostServiceEntry(
                    set_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    4,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_AV_SET_SAVED_DATA_ADDRESS,
                ),
            ]
        )

        def unexpected_handler(*_args: object) -> None:
            self.fail("native AV service crossed into Python")

        memory = SparseMemory({0x8000: 0})
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                callback_addresses={get_target, display_target, set_target},
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                call_handlers={
                    get_target: unexpected_handler,
                    display_target: unexpected_handler,
                    set_target: unexpected_handler,
                },
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=20,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 0)
        self.assertEqual(state.get_register("ebx"), saved_data_address)
        self.assertEqual(services.av_saved_data_address, saved_data_address)
        self.assertEqual(services.av_display_mode_set_count, 1)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_host_service_call_count"], 3)
        self.assertEqual(performance["handler_call_count"], 0)

    def test_native_bootstrap_file_services_bypass_python(self) -> None:
        base_address = 0x1000
        open_target = 0x2000
        volume_target = 0x2010
        link_open_target = 0x2020
        link_query_target = 0x2030
        create_target = 0x2040
        file_query_target = 0x2050
        read_target = 0x2060
        close_target = 0x2070
        open_handle_address = 0x3000
        link_handle_address = 0x3004
        file_handle_address = 0x3008
        volume_output = 0x3100
        link_output = 0x3200
        link_buffer = 0x3300
        file_output = 0x3400
        network_file_output = 0x3480
        io_status = 0x3500
        read_output = 0x3600
        open_attributes = 0x4000
        open_descriptor = 0x4100
        open_path_buffer = 0x4200
        link_attributes = 0x4300
        link_name_descriptor = 0x4400
        link_name_buffer = 0x4500
        file_attributes = 0x4600
        file_descriptor = 0x4700
        file_path_buffer = 0x4800

        code = bytearray()

        def emit_call(target: int, arguments: tuple[int, ...]) -> None:
            for argument in reversed(arguments):
                code.extend(b"\x68" + struct.pack("<I", argument))
            call_address = base_address + len(code)
            code.extend(
                b"\xE8" + struct.pack("<i", target - (call_address + 5))
            )

        emit_call(
            open_target,
            (
                open_handle_address,
                0x80100000,
                open_attributes,
                io_status,
                1,
                0x20,
            ),
        )
        emit_call(
            volume_target,
            (0x108, io_status, volume_output, 24, 3),
        )
        emit_call(
            link_open_target,
            (link_handle_address, link_attributes),
        )
        emit_call(
            link_query_target,
            (0x10C, link_output, 0),
        )
        emit_call(
            create_target,
            (
                file_handle_address,
                0x00100001,
                file_attributes,
                io_status,
                0,
                0,
                3,
                1,
                0,
            ),
        )
        emit_call(
            file_query_target,
            (0x110, io_status, file_output, 8, 20),
        )
        emit_call(
            file_query_target,
            (0x110, io_status, network_file_output, 56, 34),
        )
        emit_call(
            read_target,
            (0x110, 0, 0, 0, io_status, read_output, 32, 0),
        )
        emit_call(close_target, (0x110,))
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_bootstrap_file_services",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    open_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    24,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_OPEN_FILE,
                ),
                NativeHostServiceEntry(
                    volume_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    20,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_QUERY_VOLUME_INFORMATION_FILE,
                ),
                NativeHostServiceEntry(
                    link_open_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    8,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_OPEN_SYMBOLIC_LINK_OBJECT,
                ),
                NativeHostServiceEntry(
                    link_query_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    12,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_QUERY_SYMBOLIC_LINK_OBJECT,
                ),
                NativeHostServiceEntry(
                    create_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    36,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_CREATE_FILE,
                ),
                NativeHostServiceEntry(
                    file_query_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    20,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_QUERY_INFORMATION_FILE,
                ),
                NativeHostServiceEntry(
                    read_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    32,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_READ_FILE,
                ),
                NativeHostServiceEntry(
                    close_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    4,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_CLOSE,
                ),
            ]
        )
        memory = SparseMemory({0x8000: 0})

        def write_ansi_descriptor(
            attributes: int,
            descriptor: int,
            buffer: int,
            value: bytes,
        ) -> None:
            memory.write_u32(attributes + 4, descriptor)
            memory.write(descriptor, struct.pack("<HHI", len(value), len(value), buffer))
            memory.write(buffer, value)

        write_ansi_descriptor(
            open_attributes,
            open_descriptor,
            open_path_buffer,
            b"\\Device\\Harddisk0\\partition1\\",
        )
        write_ansi_descriptor(
            link_attributes,
            link_name_descriptor,
            link_name_buffer,
            b"\\??\\D:",
        )
        write_ansi_descriptor(
            file_attributes,
            file_descriptor,
            file_path_buffer,
            b"D:\\data\\b2distfx.bin",
        )
        memory.write(link_output, struct.pack("<HHI", 0, 32, link_buffer))

        def unexpected_handler(*_args: object) -> None:
            self.fail("native file service crossed into Python")

        targets = {
            open_target,
            volume_target,
            link_open_target,
            link_query_target,
            create_target,
            file_query_target,
            read_target,
            close_target,
        }
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            extracted_root = Path(temp_dir) / "disc"
            asset_path = extracted_root / "data" / "b2distfx.bin"
            asset_path.parent.mkdir(parents=True)
            asset_prefix = b"native-kernel-file-read"
            asset_path.write_bytes(
                asset_prefix + bytes(21756 - len(asset_prefix))
            )
            save_root = Path(temp_dir) / "save"
            save_root.mkdir()
            services.set_filesystem_roots(
                extracted_disc_root=extracted_root,
                save_data_root=save_root,
                dashboard_data_root=None,
                cache_data_root=None,
                title_id=0x41430019,
            )
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir) / "build",
                callback_addresses=targets,
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                call_handlers={target: unexpected_handler for target in targets},
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=160,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(memory.read_u32(open_handle_address), 0x108)
        self.assertEqual(struct.unpack("<QQII", memory.read(volume_output, 24)), (
            312501,
            312501,
            32,
            512,
        ))
        self.assertEqual(memory.read_u32(link_handle_address), 0x10C)
        self.assertEqual(memory.read(link_buffer, 14), b"\\Device\\Cdrom0")
        self.assertEqual(memory.read_u32(file_handle_address), 0x110)
        self.assertEqual(struct.unpack("<Q", memory.read(file_output, 8))[0], 21756)
        self.assertEqual(
            struct.unpack("<Q", memory.read(network_file_output + 40, 8))[0],
            21756,
        )
        self.assertEqual(memory.read_u32(network_file_output + 48), 0x80)
        self.assertEqual(memory.read(read_output, len(asset_prefix)), asset_prefix)
        self.assertEqual(memory.read_u32(io_status), 0)
        self.assertEqual(memory.read_u32(io_status + 4), 32)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_host_service_call_count"], 9)
        self.assertEqual(performance["handler_call_count"], 0)

    def test_native_raw_partition_zero_is_persistent_and_bypasses_python(self) -> None:
        base_address = 0x1000
        open_target = 0x2000
        read_target = 0x2010
        write_target = 0x2020
        close_target = 0x2030
        handle_address = 0x3000
        io_status = 0x3010
        offset_address = 0x3020
        read_buffer = 0x3100
        write_buffer = 0x3300
        attributes = 0x4000
        descriptor = 0x4010
        path_buffer = 0x4100
        payload = b"B2R raw partition table"

        code = bytearray()

        def emit_call(target: int, arguments: tuple[int, ...]) -> None:
            for argument in reversed(arguments):
                code.extend(b"\x68" + struct.pack("<I", argument))
            call_address = base_address + len(code)
            code.extend(
                b"\xE8" + struct.pack("<i", target - (call_address + 5))
            )

        emit_call(
            open_target,
            (handle_address, 0xC0100000, attributes, io_status, 3, 0x10),
        )
        emit_call(
            read_target,
            (0x108, 0, 0, 0, io_status, read_buffer, 0x200, offset_address),
        )
        emit_call(
            write_target,
            (
                0x108,
                0,
                0,
                0,
                io_status,
                write_buffer,
                len(payload),
                offset_address,
            ),
        )
        emit_call(close_target, (0x108,))
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_raw_partition_zero",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    open_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    24,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_OPEN_FILE,
                ),
                NativeHostServiceEntry(
                    read_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    32,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_READ_FILE,
                ),
                NativeHostServiceEntry(
                    write_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    32,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_WRITE_FILE,
                ),
                NativeHostServiceEntry(
                    close_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    4,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_CLOSE,
                ),
            ]
        )
        memory = SparseMemory({0x8000: 0})
        raw_path = b"\\Device\\Harddisk0\\partition0"
        memory.write_u32(attributes + 4, descriptor)
        memory.write(
            descriptor,
            struct.pack("<HHI", len(raw_path), len(raw_path), path_buffer),
        )
        memory.write(path_buffer, raw_path)
        memory.write(offset_address, struct.pack("<Q", 0x800))
        memory.write(write_buffer, payload)

        def unexpected_handler(*_args: object) -> None:
            self.fail("native raw-partition service crossed into Python")

        targets = {open_target, read_target, write_target, close_target}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            cache_root = Path(temp_dir) / "cache"
            services.set_filesystem_roots(
                extracted_disc_root=None,
                save_data_root=Path(temp_dir) / "save",
                dashboard_data_root=None,
                cache_data_root=cache_root,
                title_id=0x41430019,
            )
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir) / "build",
                callback_addresses=targets,
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                call_handlers={target: unexpected_handler for target in targets},
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=80,
            )
            raw_partition = (cache_root / "partition0.bin").read_bytes()

        self.assertEqual(returned_to, 0)
        self.assertEqual(memory.read_u32(handle_address), 0x108)
        self.assertEqual(memory.read(read_buffer, 0x200), bytes(0x200))
        self.assertEqual(raw_partition[0x800 : 0x800 + len(payload)], payload)
        self.assertEqual(len(raw_partition), 0xA00)
        self.assertEqual(memory.read_u32(io_status), 0)
        self.assertEqual(memory.read_u32(io_status + 4), len(payload))
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_host_service_call_count"], 4)
        self.assertEqual(performance["handler_call_count"], 0)

    def test_native_cache_partition_device_supports_cold_format_probe(self) -> None:
        base_address = 0x1000
        open_target = 0x2000
        device_io_target = 0x2010
        query_volume_target = 0x2020
        fs_control_target = 0x2030
        close_target = 0x2040
        handle_address = 0x3000
        open_io_status = 0x3010
        geometry_io_status = 0x3020
        partition_io_status = 0x3030
        volume_io_status = 0x3040
        fs_io_status = 0x3050
        geometry_output = 0x3100
        partition_output = 0x3200
        volume_output = 0x3300
        attributes = 0x4000
        descriptor = 0x4010
        path_buffer = 0x4100

        code = bytearray()

        def emit_call(target: int, arguments: tuple[int, ...]) -> None:
            for argument in reversed(arguments):
                code.extend(b"\x68" + struct.pack("<I", argument))
            call_address = base_address + len(code)
            code.extend(
                b"\xE8" + struct.pack("<i", target - (call_address + 5))
            )

        emit_call(
            open_target,
            (handle_address, 0x00100003, attributes, open_io_status, 3, 0x18),
        )
        emit_call(
            device_io_target,
            (
                0x108,
                0,
                0,
                0,
                geometry_io_status,
                0x00070000,
                0,
                0,
                geometry_output,
                24,
            ),
        )
        emit_call(
            device_io_target,
            (
                0x108,
                0,
                0,
                0,
                partition_io_status,
                0x00074004,
                0,
                0,
                partition_output,
                32,
            ),
        )
        emit_call(
            query_volume_target,
            (0x108, volume_io_status, volume_output, 24, 3),
        )
        emit_call(
            fs_control_target,
            (0x108, 0, 0, 0, fs_io_status, 0x00090020, 0, 0, 0, 0x4000),
        )
        emit_call(close_target, (0x108,))
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_cache_partition_device",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    open_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    24,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_OPEN_FILE,
                ),
                NativeHostServiceEntry(
                    device_io_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    40,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_DEVICE_IO_CONTROL_FILE,
                ),
                NativeHostServiceEntry(
                    query_volume_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    20,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_QUERY_VOLUME_INFORMATION_FILE,
                ),
                NativeHostServiceEntry(
                    fs_control_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    40,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_FS_CONTROL_FILE,
                ),
                NativeHostServiceEntry(
                    close_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    4,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_CLOSE,
                ),
            ]
        )
        memory = SparseMemory({0x8000: 0})
        raw_path = b"\\Device\\Harddisk0\\Partition5"
        memory.write_u32(attributes + 4, descriptor)
        memory.write(
            descriptor,
            struct.pack("<HHI", len(raw_path), len(raw_path), path_buffer),
        )
        memory.write(path_buffer, raw_path)
        targets = {
            open_target,
            device_io_target,
            query_volume_target,
            fs_control_target,
            close_target,
        }

        def unexpected_handler(*_args: object) -> None:
            self.fail("native cache-device service crossed into Python")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            cache_root = Path(temp_dir) / "cache"
            services.set_filesystem_roots(
                extracted_disc_root=None,
                save_data_root=Path(temp_dir) / "save",
                dashboard_data_root=None,
                cache_data_root=cache_root,
                title_id=0x41430019,
            )
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir) / "build",
                callback_addresses=targets,
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                call_handlers={target: unexpected_handler for target in targets},
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=100,
            )
            raw_partition_size = (cache_root / "partition5.bin").stat().st_size

        self.assertEqual(returned_to, 0)
        self.assertEqual(memory.read_u32(handle_address), 0x108)
        self.assertEqual(
            struct.unpack("<qIIII", memory.read(geometry_output, 24)),
            (128, 12, 32, 16, 512),
        )
        self.assertEqual(
            struct.unpack("<QQII", memory.read(partition_output, 24)),
            (0, 32 * 1024 * 1024, 0, 5),
        )
        self.assertEqual(memory.read(partition_output + 24, 8), bytes(8))
        self.assertEqual(
            struct.unpack("<QQII", memory.read(volume_output, 24)),
            (312501, 312501, 32, 512),
        )
        self.assertEqual(raw_partition_size, 32 * 1024 * 1024)
        for io_status in (
            open_io_status,
            geometry_io_status,
            partition_io_status,
            volume_io_status,
            fs_io_status,
        ):
            self.assertEqual(memory.read_u32(io_status), 0)
        self.assertEqual(memory.read_u32(geometry_io_status + 4), 24)
        self.assertEqual(memory.read_u32(partition_io_status + 4), 32)
        self.assertEqual(memory.read_u32(volume_io_status + 4), 24)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_host_service_call_count"], 6)
        self.assertEqual(performance["handler_call_count"], 0)

    def test_native_save_file_create_write_seek_and_read_bypass_python(self) -> None:
        base_address = 0x1000
        create_target = 0x2000
        write_target = 0x2010
        set_target = 0x2020
        read_target = 0x2030
        close_target = 0x2040
        directory_handle = 0x3000
        file_handle = 0x3004
        io_status = 0x3010
        position = 0x3020
        write_buffer = 0x3100
        read_buffer = 0x3200
        directory_attributes = 0x4000
        directory_descriptor = 0x4010
        directory_path = 0x4100
        file_attributes = 0x4200
        file_descriptor = 0x4210
        file_path = 0x4300
        payload = b"native-save-file"

        code = bytearray()

        def emit_call(target: int, arguments: tuple[int, ...]) -> None:
            for argument in reversed(arguments):
                code.extend(b"\x68" + struct.pack("<I", argument))
            call_address = base_address + len(code)
            code.extend(
                b"\xE8" + struct.pack("<i", target - (call_address + 5))
            )

        emit_call(
            create_target,
            (
                directory_handle,
                0x00100001,
                directory_attributes,
                io_status,
                0,
                0x10,
                3,
                3,
                0x4021,
                0,
                0,
            ),
        )
        emit_call(close_target, (0x108,))
        emit_call(
            create_target,
            (
                file_handle,
                0x40100000,
                file_attributes,
                io_status,
                0,
                4,
                1,
                3,
                0x22,
            ),
        )
        emit_call(
            write_target,
            (0x10C, 0, 0, 0, io_status, write_buffer, len(payload), 0),
        )
        emit_call(
            set_target,
            (0x10C, io_status, position, 8, 14),
        )
        emit_call(
            read_target,
            (0x10C, 0, 0, 0, io_status, read_buffer, len(payload), 0),
        )
        emit_call(close_target, (0x10C,))
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_save_file_services",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    create_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    36,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_CREATE_FILE,
                ),
                NativeHostServiceEntry(
                    write_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    32,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_WRITE_FILE,
                ),
                NativeHostServiceEntry(
                    set_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    20,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_SET_INFORMATION_FILE,
                ),
                NativeHostServiceEntry(
                    read_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    32,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_READ_FILE,
                ),
                NativeHostServiceEntry(
                    close_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    4,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_CLOSE,
                ),
            ]
        )
        memory = SparseMemory({0x8000: 0})

        def write_ansi_descriptor(
            attributes: int,
            descriptor: int,
            buffer: int,
            value: bytes,
        ) -> None:
            memory.write_u32(attributes + 4, descriptor)
            memory.write(descriptor, struct.pack("<HHI", len(value), len(value), buffer))
            memory.write(buffer, value)

        write_ansi_descriptor(
            directory_attributes,
            directory_descriptor,
            directory_path,
            b"\\Device\\Harddisk0\\Partition1\\TDATA",
        )
        write_ansi_descriptor(
            file_attributes,
            file_descriptor,
            file_path,
            b"T:\\TitleMeta.xbx",
        )
        memory.write(write_buffer, payload)
        memory.write(position, struct.pack("<Q", 0))
        targets = {
            create_target,
            write_target,
            set_target,
            read_target,
            close_target,
        }

        def unexpected_handler(*_args: object) -> None:
            self.fail("native save service crossed into Python")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            save_root = Path(temp_dir) / "save"
            (save_root / "TDATA" / "41430019").mkdir(parents=True)
            services.set_filesystem_roots(
                extracted_disc_root=None,
                save_data_root=save_root,
                dashboard_data_root=None,
                cache_data_root=Path(temp_dir) / "cache",
                title_id=0x41430019,
            )
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir) / "build",
                callback_addresses=targets,
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                call_handlers={target: unexpected_handler for target in targets},
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=120,
            )
            saved_payload = (
                save_root / "TDATA" / "41430019" / "TitleMeta.xbx"
            ).read_bytes()

        self.assertEqual(returned_to, 0)
        self.assertEqual(memory.read_u32(directory_handle), 0x108)
        self.assertEqual(memory.read_u32(file_handle), 0x10C)
        self.assertEqual(memory.read(read_buffer, len(payload)), payload)
        self.assertEqual(saved_payload, payload)
        self.assertEqual(memory.read_u32(io_status), 0)
        self.assertEqual(memory.read_u32(io_status + 4), len(payload))
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_host_service_call_count"], 7)
        self.assertEqual(performance["handler_call_count"], 0)

    def test_native_autosave_delete_recreate_and_truncate_bypass_python(self) -> None:
        base_address = 0x1000
        open_target = 0x2000
        set_target = 0x2010
        close_target = 0x2020
        create_target = 0x2030
        write_target = 0x2040
        delete_handle = 0x3000
        create_handle = 0x3004
        io_status = 0x3010
        disposition = 0x3020
        end_of_file = 0x3030
        write_buffer = 0x3100
        attributes = 0x4000
        descriptor = 0x4010
        path_buffer = 0x4100
        old_payload = b"old autosave payload that must be replaced"
        fresh_payload = b"fresh autosave payload"
        host_write_payload = fresh_payload + b"obsolete tail"

        code = bytearray()

        def emit_call(target: int, arguments: tuple[int, ...]) -> None:
            for argument in reversed(arguments):
                code.extend(b"\x68" + struct.pack("<I", argument))
            call_address = base_address + len(code)
            code.extend(
                b"\xE8" + struct.pack("<i", target - (call_address + 5))
            )

        emit_call(
            open_target,
            (delete_handle, 0x00110000, attributes, io_status, 7, 0x4020),
        )
        emit_call(set_target, (0x108, io_status, disposition, 1, 13))
        emit_call(close_target, (0x108,))
        emit_call(
            create_target,
            (
                create_handle,
                0x40100000,
                attributes,
                io_status,
                0,
                4,
                1,
                2,
                0x22,
            ),
        )
        emit_call(
            write_target,
            (
                0x10C,
                0,
                0,
                0,
                io_status,
                write_buffer,
                len(host_write_payload),
                0,
            ),
        )
        emit_call(set_target, (0x10C, io_status, end_of_file, 8, 20))
        emit_call(close_target, (0x10C,))
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_autosave_delete_recreate",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    open_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    24,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_OPEN_FILE,
                ),
                NativeHostServiceEntry(
                    set_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    20,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_SET_INFORMATION_FILE,
                ),
                NativeHostServiceEntry(
                    close_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    4,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_CLOSE,
                ),
                NativeHostServiceEntry(
                    create_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    36,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_CREATE_FILE,
                ),
                NativeHostServiceEntry(
                    write_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    32,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_WRITE_FILE,
                ),
            ]
        )
        memory = SparseMemory({0x9000: 0})
        path = b"U:\\0F71DA412427\\Profile 1"
        memory.write_u32(attributes + 4, descriptor)
        memory.write(
            descriptor,
            struct.pack("<HHI", len(path), len(path), path_buffer),
        )
        memory.write(path_buffer, path)
        memory.write(disposition, b"\x01")
        memory.write(end_of_file, struct.pack("<Q", len(fresh_payload)))
        memory.write(write_buffer, host_write_payload)
        targets = {
            open_target,
            set_target,
            close_target,
            create_target,
            write_target,
        }

        def unexpected_handler(*_args: object) -> None:
            self.fail("native autosave service crossed into Python")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            save_root = Path(temp_dir) / "save"
            profile = (
                save_root
                / "UDATA"
                / "41430019"
                / "0F71DA412427"
                / "Profile 1"
            )
            profile.parent.mkdir(parents=True)
            profile.write_bytes(old_payload)
            services.set_filesystem_roots(
                extracted_disc_root=None,
                save_data_root=save_root,
                dashboard_data_root=None,
                cache_data_root=None,
                title_id=0x41430019,
            )
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir) / "build",
                callback_addresses=targets,
            )
            state = CpuState.with_registers(esp=0x9000)
            returned_to = executor.run(
                state,
                memory,
                call_handlers={target: unexpected_handler for target in targets},
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=160,
            )
            saved_payload = profile.read_bytes()

        self.assertEqual(returned_to, 0)
        self.assertEqual(memory.read_u32(delete_handle), 0x108)
        self.assertEqual(memory.read_u32(create_handle), 0x10C)
        self.assertEqual(memory.read_u32(io_status), 0)
        self.assertEqual(saved_payload, fresh_payload)
        self.assertEqual(services.filesystem_event_count, 7)
        self.assertEqual(
            [services.filesystem_events[index].service_value for index in range(7)],
            [12, 21, 9, 11, 22, 21, 9],
        )
        self.assertEqual(services.filesystem_events[1].arguments[4], 13)
        self.assertEqual(services.filesystem_events[5].arguments[4], 20)
        self.assertEqual(
            os.fsdecode(services.filesystem_events[1].guest_path),
            "U:\\0F71DA412427\\Profile 1",
        )
        self.assertEqual(services.save_filesystem_event_count, 7)
        self.assertEqual(
            [
                services.save_filesystem_events[index].service_value
                for index in range(7)
            ],
            [12, 21, 9, 11, 22, 21, 9],
        )
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_host_service_call_count"], 7)
        self.assertEqual(performance["handler_call_count"], 0)

    def test_native_failed_save_container_cleanup_deletes_file_and_directory(
        self,
    ) -> None:
        base_address = 0x1000
        open_target = 0x2000
        set_target = 0x2010
        close_target = 0x2020
        file_handle = 0x3000
        directory_handle = 0x3004
        io_status = 0x3010
        disposition = 0x3020
        file_attributes = 0x4000
        file_descriptor = 0x4010
        file_path_buffer = 0x4100
        directory_attributes = 0x4200
        directory_descriptor = 0x4210
        directory_path_buffer = 0x4300

        code = bytearray()

        def emit_call(target: int, arguments: tuple[int, ...]) -> None:
            for argument in reversed(arguments):
                code.extend(b"\x68" + struct.pack("<I", argument))
            call_address = base_address + len(code)
            code.extend(
                b"\xE8" + struct.pack("<i", target - (call_address + 5))
            )

        emit_call(
            open_target,
            (file_handle, 0x00110000, file_attributes, io_status, 7, 0x4020),
        )
        emit_call(set_target, (0x108, io_status, disposition, 1, 13))
        emit_call(close_target, (0x108,))
        emit_call(
            open_target,
            (
                directory_handle,
                0x00110000,
                directory_attributes,
                io_status,
                7,
                0x4021,
            ),
        )
        emit_call(set_target, (0x10C, io_status, disposition, 1, 13))
        emit_call(close_target, (0x10C,))
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_failed_save_container_cleanup",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    open_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    24,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_OPEN_FILE,
                ),
                NativeHostServiceEntry(
                    set_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    20,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_SET_INFORMATION_FILE,
                ),
                NativeHostServiceEntry(
                    close_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    4,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_CLOSE,
                ),
            ]
        )
        memory = SparseMemory({0x9000: 0})
        file_path = b"U:\\0F71DA412428\\SaveMeta.xbx"
        directory_path = b"U:\\0F71DA412428"
        memory.write_u32(file_attributes + 4, file_descriptor)
        memory.write(
            file_descriptor,
            struct.pack("<HHI", len(file_path), len(file_path), file_path_buffer),
        )
        memory.write(file_path_buffer, file_path)
        memory.write_u32(directory_attributes + 4, directory_descriptor)
        memory.write(
            directory_descriptor,
            struct.pack(
                "<HHI",
                len(directory_path),
                len(directory_path),
                directory_path_buffer,
            ),
        )
        memory.write(directory_path_buffer, directory_path)
        memory.write(disposition, b"\x01")
        targets = {open_target, set_target, close_target}

        def unexpected_handler(*_args: object) -> None:
            self.fail("native save cleanup crossed into Python")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            save_root = Path(temp_dir) / "save"
            container = save_root / "UDATA" / "41430019" / "0F71DA412428"
            container.mkdir(parents=True)
            (container / "SaveMeta.xbx").write_bytes(b"")
            services.set_filesystem_roots(
                extracted_disc_root=None,
                save_data_root=save_root,
                dashboard_data_root=None,
                cache_data_root=None,
                title_id=0x41430019,
            )
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir) / "build",
                callback_addresses=targets,
            )
            state = CpuState.with_registers(esp=0x9000)
            returned_to = executor.run(
                state,
                memory,
                call_handlers={target: unexpected_handler for target in targets},
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=120,
            )
            container_exists = container.exists()

        self.assertEqual(returned_to, 0)
        self.assertEqual(memory.read_u32(file_handle), 0x108)
        self.assertEqual(memory.read_u32(directory_handle), 0x10C)
        self.assertEqual(memory.read_u32(io_status), 0)
        self.assertFalse(container_exists)
        self.assertEqual(services.filesystem_event_count, 6)
        self.assertEqual(
            [services.filesystem_events[index].service_value for index in range(6)],
            [12, 21, 9, 12, 21, 9],
        )
        self.assertEqual(services.save_filesystem_event_count, 6)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_host_service_call_count"], 6)
        self.assertEqual(performance["handler_call_count"], 0)

    def test_native_save_metadata_probe_does_not_create_missing_container(
        self,
    ) -> None:
        base_address = 0x1000
        create_target = 0x2000
        output_handle = 0x3000
        io_status = 0x3010
        attributes = 0x4000
        descriptor = 0x4010
        path_buffer = 0x4100
        path = b"U:\\0F71DA412428\\SaveMeta.xbx"

        code = bytearray()
        for argument in reversed(
            (
                output_handle,
                0x80100080,
                attributes,
                io_status,
                0,
                4,
                0,
                3,
                0x60,
            )
        ):
            code.extend(b"\x68" + struct.pack("<I", argument))
        call_address = base_address + len(code)
        code.extend(
            b"\xE8" + struct.pack("<i", create_target - (call_address + 5))
        )
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_missing_save_metadata_probe",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    create_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    36,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_CREATE_FILE,
                )
            ]
        )
        memory = SparseMemory({0x9000: 0})
        memory.write_u32(attributes + 4, descriptor)
        memory.write(
            descriptor,
            struct.pack("<HHI", len(path), len(path), path_buffer),
        )
        memory.write(path_buffer, path)

        def unexpected_handler(*_args: object) -> None:
            self.fail("native save metadata probe crossed into Python")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            save_root = Path(temp_dir) / "save"
            title_root = save_root / "UDATA" / "41430019"
            title_root.mkdir(parents=True)
            services.set_filesystem_roots(
                extracted_disc_root=None,
                save_data_root=save_root,
                dashboard_data_root=None,
                cache_data_root=None,
                title_id=0x41430019,
            )
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir) / "build",
                callback_addresses={create_target},
            )
            state = CpuState.with_registers(esp=0x9000)
            returned_to = executor.run(
                state,
                memory,
                call_handlers={create_target: unexpected_handler},
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=40,
            )
            container_exists = (title_root / "0F71DA412428").exists()

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 0xC000003A)
        self.assertEqual(memory.read_u32(output_handle), 0)
        self.assertEqual(memory.read_u32(io_status), 0xC000003A)
        self.assertFalse(container_exists)
        self.assertEqual(services.filesystem_event_count, 1)
        self.assertEqual(services.filesystem_events[0].result, 0xC000003A)
        self.assertEqual(services.save_filesystem_event_count, 1)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_host_service_call_count"], 1)
        self.assertEqual(performance["handler_call_count"], 0)

    def test_native_save_crypto_matches_xbox_sha_hmac_and_rc4(self) -> None:
        base_address = 0x1000
        sha_init_target = 0x2000
        sha_update_target = 0x2010
        sha_final_target = 0x2020
        hmac_target = 0x2030
        rc4_key_target = 0x2040
        rc4_crypt_target = 0x2050
        sha_context = 0x3000
        sha_payload_address = 0x4000
        sha_digest_address = 0x4100
        hmac_key_address = 0x4200
        hmac_left_address = 0x4300
        hmac_right_address = 0x4400
        hmac_digest_address = 0x4500
        rc4_context = 0x4600
        rc4_key_address = 0x4800
        rc4_payload_address = 0x4900
        sha_payload = b"Burnout 2 save metadata"
        hmac_key = bytes(range(80))
        hmac_left = b"burn"
        hmac_right = b"out"
        rc4_payload = b"Plaintext"

        code = bytearray()

        def emit_call(target: int, arguments: tuple[int, ...]) -> None:
            for argument in reversed(arguments):
                code.extend(b"\x68" + struct.pack("<I", argument))
            call_address = base_address + len(code)
            code.extend(
                b"\xE8" + struct.pack("<i", target - (call_address + 5))
            )

        emit_call(sha_init_target, (sha_context,))
        emit_call(
            sha_update_target,
            (sha_context, sha_payload_address, 7),
        )
        emit_call(
            sha_update_target,
            (
                sha_context,
                sha_payload_address + 7,
                len(sha_payload) - 7,
            ),
        )
        emit_call(sha_final_target, (sha_context, sha_digest_address))
        emit_call(
            hmac_target,
            (
                hmac_key_address,
                len(hmac_key),
                hmac_left_address,
                len(hmac_left),
                hmac_right_address,
                len(hmac_right),
                hmac_digest_address,
            ),
        )
        emit_call(
            rc4_key_target,
            (rc4_context, 3, rc4_key_address),
        )
        emit_call(
            rc4_crypt_target,
            (rc4_context, 5, rc4_payload_address),
        )
        emit_call(
            rc4_crypt_target,
            (rc4_context, 4, rc4_payload_address + 5),
        )
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_save_crypto",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    sha_init_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    4,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_XC_SHA_INIT,
                ),
                NativeHostServiceEntry(
                    sha_update_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    12,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_XC_SHA_UPDATE,
                ),
                NativeHostServiceEntry(
                    sha_final_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    8,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_XC_SHA_FINAL,
                ),
                NativeHostServiceEntry(
                    hmac_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    28,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_XC_HMAC,
                ),
                NativeHostServiceEntry(
                    rc4_key_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    12,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_XC_RC4_KEY,
                ),
                NativeHostServiceEntry(
                    rc4_crypt_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    12,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_XC_RC4_CRYPT,
                ),
            ]
        )
        memory = SparseMemory({0x8000: 0})
        memory.write(sha_context, b"\xA5" * 116)
        memory.write(sha_payload_address, sha_payload)
        memory.write(hmac_key_address, hmac_key)
        memory.write(hmac_left_address, hmac_left)
        memory.write(hmac_right_address, hmac_right)
        memory.write(rc4_key_address, b"Key")
        memory.write(rc4_payload_address, rc4_payload)
        targets = {
            sha_init_target,
            sha_update_target,
            sha_final_target,
            hmac_target,
            rc4_key_target,
            rc4_crypt_target,
        }

        def unexpected_handler(*_args: object) -> None:
            self.fail("native save crypto crossed into Python")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                callback_addresses=targets,
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                call_handlers={target: unexpected_handler for target in targets},
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=100,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(
            memory.read(sha_digest_address, 20),
            hashlib.sha1(sha_payload, usedforsecurity=False).digest(),
        )
        self.assertEqual(memory.read(sha_context, 24), b"\xA5" * 24)
        self.assertEqual(memory.read(sha_context + 24, 92), b"\x00" * 92)
        self.assertEqual(
            memory.read(hmac_digest_address, 20),
            hmac.new(
                hmac_key[:64],
                hmac_left + hmac_right,
                hashlib.sha1,
            ).digest(),
        )
        self.assertEqual(
            memory.read(rc4_payload_address, len(rc4_payload)),
            bytes.fromhex("BBF316E8D940AF0AD3"),
        )
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_host_service_call_count"], 8)
        self.assertEqual(performance["handler_call_count"], 0)

    def test_native_save_time_conversion_round_trips_xbox_time_fields(
        self,
    ) -> None:
        base_address = 0x1000
        time_fields_to_time_target = 0x2000
        time_to_time_fields_target = 0x2010
        file_time_address = 0x3000
        time_fields_address = 0x3010
        round_trip_address = 0x3030
        success_address = 0x3040
        file_time = 116_444_736_000_000_000 + 1_230_000

        code = bytearray()

        def emit_call(target: int, arguments: tuple[int, ...]) -> None:
            for argument in reversed(arguments):
                code.extend(b"\x68" + struct.pack("<I", argument))
            call_address = base_address + len(code)
            code.extend(
                b"\xE8" + struct.pack("<i", target - (call_address + 5))
            )

        emit_call(
            time_to_time_fields_target,
            (file_time_address, time_fields_address),
        )
        emit_call(
            time_fields_to_time_target,
            (time_fields_address, round_trip_address),
        )
        code.extend(b"\xA3" + struct.pack("<I", success_address))
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_save_time_conversion",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    time_fields_to_time_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    8,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_RTL_TIME_FIELDS_TO_TIME,
                ),
                NativeHostServiceEntry(
                    time_to_time_fields_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    8,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_RTL_TIME_TO_TIME_FIELDS,
                ),
            ]
        )
        memory = SparseMemory({0x8000: 0})
        memory.write(file_time_address, struct.pack("<Q", file_time))
        targets = {time_fields_to_time_target, time_to_time_fields_target}

        def unexpected_handler(*_args: object) -> None:
            self.fail("native save time conversion crossed into Python")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                callback_addresses=targets,
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                call_handlers={target: unexpected_handler for target in targets},
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=40,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(
            struct.unpack("<8h", memory.read(time_fields_address, 16)),
            (1970, 1, 1, 0, 0, 0, 123, 4),
        )
        self.assertEqual(
            struct.unpack("<Q", memory.read(round_trip_address, 8))[0],
            file_time,
        )
        self.assertEqual(memory.read_u32(success_address), 1)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_host_service_call_count"], 2)
        self.assertEqual(performance["handler_call_count"], 0)

    def test_native_directory_query_is_sorted_resumable_and_bypasses_python(
        self,
    ) -> None:
        base_address = 0x1000
        open_target = 0x2000
        query_target = 0x2010
        directory_handle = 0x3000
        open_status = 0x3010
        first_status = 0x3020
        second_status = 0x3030
        final_status = 0x3040
        first_output = 0x4000
        second_output = 0x4100
        final_output = 0x4200
        directory_attributes = 0x5000
        directory_descriptor = 0x5010
        directory_path = 0x5100
        pattern_descriptor = 0x5200
        pattern_buffer = 0x5210

        code = bytearray()

        def emit_call(target: int, arguments: tuple[int, ...]) -> None:
            for argument in reversed(arguments):
                code.extend(b"\x68" + struct.pack("<I", argument))
            call_address = base_address + len(code)
            code.extend(
                b"\xE8" + struct.pack("<i", target - (call_address + 5))
            )

        emit_call(
            open_target,
            (
                directory_handle,
                1,
                directory_attributes,
                open_status,
                0,
                1,
            ),
        )
        emit_call(
            query_target,
            (
                0x108,
                0,
                0,
                0,
                first_status,
                first_output,
                0x100,
                1,
                pattern_descriptor,
                1,
            ),
        )
        emit_call(
            query_target,
            (
                0x108,
                0,
                0,
                0,
                second_status,
                second_output,
                0x100,
                1,
                0,
                0,
            ),
        )
        emit_call(
            query_target,
            (
                0x108,
                0,
                0,
                0,
                final_status,
                final_output,
                0x100,
                1,
                0,
                0,
            ),
        )
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_directory_query_services",
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    open_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    24,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_OPEN_FILE,
                ),
                NativeHostServiceEntry(
                    query_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    40,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_QUERY_DIRECTORY_FILE,
                ),
            ]
        )
        memory = SparseMemory({0x9000: 0})
        directory_path_bytes = b"D:\\profiles"
        pattern = b"*.*"
        memory.write_u32(directory_attributes + 4, directory_descriptor)
        memory.write(
            directory_descriptor,
            struct.pack(
                "<HHI",
                len(directory_path_bytes),
                len(directory_path_bytes),
                directory_path,
            ),
        )
        memory.write(directory_path, directory_path_bytes)
        memory.write(
            pattern_descriptor,
            struct.pack("<HHI", len(pattern), len(pattern), pattern_buffer),
        )
        memory.write(pattern_buffer, pattern)
        targets = {open_target, query_target}

        def unexpected_handler(*_args: object) -> None:
            self.fail("native directory service crossed into Python")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            extracted_root = Path(temp_dir) / "disc"
            profiles = extracted_root / "profiles"
            profiles.mkdir(parents=True)
            (profiles / "zeta").mkdir()
            (profiles / "Alpha.bin").write_bytes(b"native-directory-entry")
            services.set_filesystem_roots(
                extracted_disc_root=extracted_root,
                save_data_root=None,
                dashboard_data_root=None,
                cache_data_root=None,
                title_id=0x41430019,
            )
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir) / "build",
                callback_addresses=targets,
            )
            state = CpuState.with_registers(esp=0x9000)
            returned_to = executor.run(
                state,
                memory,
                call_handlers={target: unexpected_handler for target in targets},
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=100,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(memory.read_u32(directory_handle), 0x108)
        self.assertEqual(memory.read_u32(first_status), 0)
        self.assertEqual(memory.read_u32(first_status + 4), 0x40 + len("Alpha.bin"))
        self.assertEqual(memory.read_u32(first_output + 4), 0)
        self.assertEqual(memory.read_u32(first_output + 56), 0x80)
        self.assertEqual(memory.read_u32(first_output + 60), len("Alpha.bin"))
        self.assertEqual(memory.read(first_output + 64, len("Alpha.bin")), b"Alpha.bin")
        self.assertEqual(memory.read_u32(second_status), 0)
        self.assertEqual(memory.read_u32(second_output + 4), 1)
        self.assertEqual(memory.read_u32(second_output + 56), 0x10)
        self.assertEqual(memory.read(second_output + 64, len("zeta")), b"zeta")
        self.assertEqual(memory.read_u32(final_status), 0x80000006)
        self.assertEqual(memory.read_u32(final_status + 4), 0)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_host_service_call_count"], 4)
        self.assertEqual(performance["handler_call_count"], 0)

    def test_native_file_table_reuses_closed_slots_during_volume_probes(
        self,
    ) -> None:
        base_address = 0x1000
        open_target = 0x100000
        close_target = 0x100010
        handle_address = 0x3000
        io_status = 0x3010
        attributes = 0x4000
        descriptor = 0x4010
        path_buffer = 0x4100
        cycle_count = 140

        code = bytearray()

        def emit_call(target: int, arguments: tuple[int, ...]) -> None:
            for argument in reversed(arguments):
                code.extend(b"\x68" + struct.pack("<I", argument))
            call_address = base_address + len(code)
            code.extend(
                b"\xE8" + struct.pack("<i", target - (call_address + 5))
            )

        def emit_close_from_memory() -> None:
            code.extend(b"\xFF\x35" + struct.pack("<I", handle_address))
            call_address = base_address + len(code)
            code.extend(
                b"\xE8"
                + struct.pack("<i", close_target - (call_address + 5))
            )

        for _index in range(cycle_count):
            emit_call(
                open_target,
                (handle_address, 0x00100001, attributes, io_status, 3, 0x800021),
            )
            emit_close_from_memory()
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_repeated_volume_probes",
            max_instructions=2000,
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    open_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    24,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_OPEN_FILE,
                ),
                NativeHostServiceEntry(
                    close_target,
                    NATIVE_HOST_SERVICE_BOOTSTRAP,
                    4,
                    NATIVE_HOST_SERVICE_BOOTSTRAP_NT_CLOSE,
                ),
            ]
        )
        memory = SparseMemory({0x9000: 0})
        path = b"U:\\"
        memory.write_u32(attributes + 4, descriptor)
        memory.write(
            descriptor,
            struct.pack("<HHI", len(path), len(path), path_buffer),
        )
        memory.write(path_buffer, path)
        targets = {open_target, close_target}

        def unexpected_handler(*_args: object) -> None:
            self.fail("native repeated file probe crossed into Python")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            save_root = Path(temp_dir) / "save"
            (save_root / "UDATA" / "41430019").mkdir(parents=True)
            services.set_filesystem_roots(
                extracted_disc_root=None,
                save_data_root=save_root,
                dashboard_data_root=None,
                cache_data_root=None,
                title_id=0x41430019,
            )
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir) / "build",
                callback_addresses=targets,
            )
            state = CpuState.with_registers(esp=0x9000)
            returned_to = executor.run(
                state,
                memory,
                call_handlers={target: unexpected_handler for target in targets},
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=2000,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(memory.read_u32(io_status), 0)
        self.assertEqual(memory.read_u32(handle_address), 0x108 + (cycle_count - 1) * 4)
        self.assertEqual(services.file_count, 1)
        self.assertEqual(services.file_overflow_count, 0)
        self.assertEqual(services.filesystem_event_count, cycle_count * 2)
        first_retained_sequence = cycle_count * 2 - len(
            services.filesystem_events
        )
        self.assertEqual(
            services.filesystem_events[
                first_retained_sequence % len(services.filesystem_events)
            ].sequence,
            first_retained_sequence,
        )
        self.assertEqual(
            services.filesystem_events[
                (cycle_count * 2 - 1) % len(services.filesystem_events)
            ].sequence,
            cycle_count * 2 - 1,
        )
        self.assertEqual(services.save_filesystem_event_count, 0)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(
            performance["native_host_service_call_count"],
            cycle_count * 2,
        )
        self.assertEqual(performance["handler_call_count"], 0)

    def test_native_service_owns_unwind_and_registers_worker(self) -> None:
        base_address = 0x1000
        handler_target = 0x2000
        reference_target = 0x2010
        priority_target = 0x2020
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
        code.extend(b"\x68\x68\x30\x00\x00")
        code.extend(b"\x68\x60\x06\x00\xE0")
        code.extend(b"\xFF\x35" + struct.pack("<I", handle_address))
        call_address = base_address + len(code)
        code.extend(
            b"\xE8" + struct.pack("<i", reference_target - (call_address + 5))
        )
        code.extend(b"\x6A\x0C")
        code.extend(b"\xFF\x35" + struct.pack("<I", handle_address))
        call_address = base_address + len(code)
        code.extend(
            b"\xE8" + struct.pack("<i", priority_target - (call_address + 5))
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
                ),
                NativeHostServiceEntry(
                    reference_target,
                    NATIVE_HOST_SERVICE_COLD_CALLBACK,
                    12,
                    NATIVE_HOST_SERVICE_LIFECYCLE_REFERENCE_WORKER,
                ),
                NativeHostServiceEntry(
                    priority_target,
                    NATIVE_HOST_SERVICE_COLD_CALLBACK,
                    8,
                    NATIVE_HOST_SERVICE_LIFECYCLE_SET_BASE_PRIORITY,
                ),
            ]
        )
        lifecycle = NativeWorkerLifecycleState()
        services.attach_worker_lifecycle(lifecycle)
        memory = SparseMemory({0x8000: 0})

        def unexpected_handler(*_args: object) -> None:
            self.fail("native worker lifecycle crossed into Python")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                callback_addresses={
                    handler_target,
                    reference_target,
                    priority_target,
                },
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                call_handlers={
                    handler_target: unexpected_handler,
                    reference_target: unexpected_handler,
                    priority_target: unexpected_handler,
                },
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                max_steps=32,
            )

        self.assertEqual(returned_to, 0)
        self.assertEqual(state.get_register("eax"), 8)
        self.assertEqual(state.get_register("esp"), 0x8004)
        self.assertEqual(memory.read_u32(handle_address), 0x108)
        self.assertEqual(memory.read_u32(0x3010), 0x108)
        worker = lifecycle.entry_for_handle(0x108)
        self.assertIsNotNone(worker)
        assert worker is not None
        self.assertEqual(worker.start_address, 0x5000)
        self.assertEqual(worker.start_context1, 0xAABBCCDD)
        self.assertEqual(worker.start_context2, 0x11223344)
        self.assertEqual(worker.status, NATIVE_WORKER_READY)
        self.assertEqual(worker.base_priority, 12)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_cold_host_call_count"], 0)
        self.assertEqual(performance["native_host_service_call_count"], 3)
        self.assertEqual(performance["handler_call_count"], 0)
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

    def test_normal_runtime_schedules_worker_without_python_slice(self) -> None:
        main = lift_x86_function(
            bytes.fromhex("B801000000A300300000EBF4"),
            base_address=0x1000,
            symbol="native_normal_runtime_main",
        )
        worker = lift_x86_function(
            bytes.fromhex(
                "8B442404A3004000008B442408A304400000C3"
            ),
            base_address=0x5000,
            symbol="native_normal_runtime_worker",
        )
        memory = SparseMemory({0x8000: 0})
        lifecycle = NativeWorkerLifecycleState()
        services = NativeHostServiceState()
        services.attach_worker_lifecycle(lifecycle)
        services.current_worker_handle = 0x100
        services.enable_normal_runtime(scheduler_quantum=5)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                main,
                build_dir=Path(temp_dir),
                module_functions=[main, worker],
            )
            executor.synchronize_worker_lifecycle(
                lifecycle,
                [
                    {
                        "handle": 0x108,
                        "start_address": 0x5000,
                        "start_context1": 0xAABBCCDD,
                        "start_context2": 0x11223344,
                        "suspended": False,
                    }
                ],
                primary_handle=0x100,
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                max_steps=20,
                dispatch_host_calls_in_native=True,
                native_host_services=services,
            )
            executor.shutdown_normal_runtime(services)

        self.assertIn(returned_to, (0x1000, 0x1005, 0x100A))
        self.assertEqual(
            lifecycle.entry_for_handle(0x108).status,
            NATIVE_WORKER_COMPLETED,
        )
        self.assertEqual(services.scheduler_worker_completion_count, 1)
        self.assertEqual(services.scheduler_worker_failure_count, 0)
        self.assertEqual(memory.read_u32(0x4000), 0xAABBCCDD)
        self.assertEqual(memory.read_u32(0x4004), 0x11223344)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["slice_yield_count"], 0)
        self.assertEqual(performance["handler_call_count"], 0)

    def test_normal_runtime_selects_one_python_observer_boundary(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("B801000000EBF9"),
            base_address=0x1000,
            symbol="native_normal_runtime_observer",
        )
        services = NativeHostServiceState()
        services.enable_normal_runtime(scheduler_quantum=5)
        observed: list[int] = []

        def observe_boundary(state, _memory, _trace, _steps) -> None:
            observed.append(state.eip)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                observer_addresses={0x1000, 0x1005},
            )
            executor.run(
                CpuState.with_registers(esp=0x8000),
                SparseMemory({0x8000: 0}),
                step_observer=observe_boundary,
                max_steps=20,
                dispatch_host_calls_in_native=True,
                native_host_services=services,
                dispatch_observers_in_native=True,
                python_observer_address=0x1000,
            )
            executor.shutdown_normal_runtime(services)

        self.assertTrue(observed)
        self.assertEqual(set(observed), {0x1000})
        self.assertGreater(services.native_observer_count, 0)

    def test_normal_runtime_reports_unhandled_target_with_pending_span(self) -> None:
        base_address = 0x1000
        missing_target = 0xE0000630
        program = bytearray.fromhex("C7050030000011111111E8")
        call_next = base_address + len(program) + 4
        program.extend(
            struct.pack("<I", (missing_target - call_next) & 0xFFFFFFFF)
        )
        program.append(0xC3)
        function = lift_x86_function(
            bytes(program),
            base_address=base_address,
            symbol="native_normal_runtime_missing_service",
        )
        services = NativeHostServiceState()
        services.enable_normal_runtime(scheduler_quantum=100)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
            )
            with self.assertRaisesRegex(
                NativeExecutorError,
                r"transport code 9 at 0xE0000630",
            ):
                executor.run(
                    CpuState.with_registers(esp=0x8000),
                    SparseMemory({0x8000: 0}),
                    memory_write_observer_ranges_provider=(
                        lambda: ((0x3000, 0x4000),)
                    ),
                    capture_observed_write_provenance=False,
                    direct_observed_write_transport=True,
                    max_steps=100,
                    dispatch_host_calls_in_native=True,
                    native_host_services=services,
                )

        self.assertEqual(services.normal_runtime_failure_code, 9)
        self.assertEqual(
            services.normal_runtime_failure_target,
            missing_target,
        )
        self.assertEqual(
            executor.last_run_summary["reason"],
            "native_runtime_failure",
        )
        self.assertEqual(executor.last_run_summary["target"], missing_target)
        self.assertEqual(
            executor.last_run_summary["performance"]["observer_callback_count"],
            0,
        )
        self.assertEqual(services.normal_runtime_opaque, 0)
        self.assertFalse(services.native_audio_output_open)

    def test_normal_runtime_advances_dynamic_d3d_get_pointer(self) -> None:
        context_global = 0x002256B8
        context_address = 0x3000
        get_pointer_address = 0x4000
        ring_start = 0x5000
        ring_end = 0x6000
        put_value = 8
        function = lift_x86_function(
            bytes.fromhex(
                "8B0D004000003B0D2C30000075F2"
                "C7050050000011111111C3"
            ),
            base_address=0x1000,
            symbol="native_normal_runtime_d3d_get_pointer",
        )
        memory = SparseMemory(
            {
                0x8000: 0,
                context_global: context_address,
                context_address + 0x2C: put_value,
                context_address + 0x30: get_pointer_address,
                context_address + 0x24: ring_start,
                context_address + 0x28: ring_end,
                get_pointer_address: 0xD752D3C8,
                ring_start: 0,
            }
        )
        services = NativeHostServiceState()
        services.enable_normal_runtime(scheduler_quantum=5)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
            )
            state = CpuState.with_registers(esp=0x8000)
            returned_to = executor.run(
                state,
                memory,
                memory_write_span_observer=lambda *_args: None,
                memory_write_observer_ranges_provider=lambda: (),
                capture_observed_write_provenance=False,
                direct_observed_write_transport=True,
                max_steps=24,
                dispatch_host_calls_in_native=True,
                native_host_services=services,
            )
            executor.shutdown_normal_runtime(services)

        self.assertEqual(returned_to, 0)
        self.assertEqual(memory.read_u32(get_pointer_address), put_value)
        self.assertEqual(services.native_gpu_get_pointer_sync_count, 1)
        self.assertEqual(services.native_gpu_observed_range_sync_count, 1)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["direct_observed_write_count"], 1)
        self.assertEqual(performance["slice_yield_count"], 0)
        self.assertEqual(performance["handler_call_count"], 0)

    def test_normal_runtime_dispatches_registered_d3d_vblank_callback(
        self,
    ) -> None:
        context_global = 0x002256B8
        context_address = 0x3000
        vblank_value_address = 0x005518FC
        callback_address = 0x5000
        main = lift_x86_function(
            bytes.fromhex("B801000000A300600000EBF4"),
            base_address=0x1000,
            symbol="native_normal_runtime_vblank_main",
        )
        callback = lift_x86_function(
            bytes.fromhex("8B4424048B08890DFC185500C3"),
            base_address=callback_address,
            symbol="native_normal_runtime_vblank_callback",
        )
        memory = SparseMemory(
            {
                0x8000: 0,
                context_global: context_address,
                context_address + 0x1988: callback_address,
                vblank_value_address: 0,
            }
        )
        services = NativeHostServiceState()
        services.enable_normal_runtime(scheduler_quantum=5)
        services.native_d3d_vblank_next_deadline_qpc = 1

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                main,
                build_dir=Path(temp_dir),
                module_functions=[main, callback],
            )
            state = CpuState.with_registers(esp=0x8000)
            try:
                returned_to = executor.run(
                    state,
                    memory,
                    max_steps=30,
                    dispatch_host_calls_in_native=True,
                    native_host_services=services,
                )
            finally:
                executor.shutdown_normal_runtime(services)

        self.assertIn(returned_to, (0x1000, 0x1005, 0x100A))
        self.assertEqual(memory.read_u32(vblank_value_address), 1)
        self.assertEqual(services.native_d3d_vblank_sequence, 1)
        self.assertEqual(services.native_d3d_vblank_tick_count, 1)
        self.assertEqual(services.native_d3d_vblank_callback_schedule_count, 1)
        self.assertEqual(
            services.native_d3d_vblank_tick_count,
            services.native_d3d_vblank_callback_schedule_count,
        )
        self.assertEqual(services.native_d3d_vblank_callback_completion_count, 1)
        self.assertEqual(services.native_d3d_vblank_callback_failure_count, 0)
        self.assertEqual(services.native_d3d_vblank_callback_context, 0)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["slice_yield_count"], 0)
        self.assertEqual(performance["handler_call_count"], 0)

    @unittest.skipUnless(os.name == "nt", "named pagefile mappings require Windows")
    def test_normal_runtime_publishes_command_spans_without_python(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("C7050030000011111111EBF4"),
            base_address=0x1000,
            symbol="native_normal_runtime_commands",
        )
        memory = SparseMemory({0x8000: 0})
        services = NativeHostServiceState()
        services.enable_normal_runtime(scheduler_quantum=5)
        name = live_transport.live_control_transport_name(str(uuid.uuid4()))

        with live_transport.LiveControlTransport.create(name) as control:
            with live_transport.LiveCommandTransport.create(name) as command:
                with live_transport.LiveResourceTransport.create(name) as resource:
                    services.attach_live_transports(
                        control,
                        command,
                        resource,
                        command_stream_generation=123,
                    )
                    with tempfile.TemporaryDirectory(
                        ignore_cleanup_errors=True
                    ) as temp_dir:
                        executor = NativeResumableExecutor(
                            function,
                            build_dir=Path(temp_dir),
                        )
                        returned_to = executor.run(
                            CpuState.with_registers(esp=0x8000),
                            memory,
                            memory_write_batch_address_base=0x80000000,
                            memory_write_observer_ranges_provider=(
                                lambda: ((0x3000, 0x4000),)
                            ),
                            capture_observed_write_provenance=False,
                            direct_observed_write_transport=True,
                            max_steps=20,
                            dispatch_host_calls_in_native=True,
                            native_host_services=services,
                        )
                        executor.shutdown_normal_runtime(services)

                    write_cursor, _read_cursor = command.cursors()
                    record = command._mapping[
                        live_transport._COMMAND_DATA_OFFSET :
                        live_transport._COMMAND_DATA_OFFSET + 20
                    ]

        self.assertIn(returned_to, (0x1000, 0x100A))
        self.assertGreater(write_cursor, 0)
        kind, flags, _reserved, address, size, write_count = struct.unpack_from(
            "<BBHIII", record
        )
        self.assertEqual(
            (kind, flags, address, size),
            (1, 0, 0x80000000, 4),
        )
        self.assertGreaterEqual(write_count, 1)
        self.assertEqual(record[16:20], bytes.fromhex("11111111"))
        self.assertGreaterEqual(services.live_published_record_count, 1)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["slice_yield_count"], 0)
        self.assertEqual(performance["handler_call_count"], 0)

    @unittest.skipUnless(os.name == "nt", "named pagefile mappings require Windows")
    def test_normal_runtime_keeps_exact_byte_access_native_with_pending_span(
        self,
    ) -> None:
        sentinel_address = 0x005A6E0C
        function = lift_x86_function(
            bytes.fromhex(
                "C7050030000011111111"
                "C6050C6E5A0022"
                "A00C6E5A00"
                "EBFE"
            ),
            base_address=0x1000,
            symbol="native_normal_runtime_span_exact_byte_access",
        )
        memory = SparseMemory({0x3000: 0, 0x8000: 0})
        memory.write_u32(sentinel_address, 0x11223344)
        state = CpuState.with_registers(esp=0x8000)
        services = NativeHostServiceState()
        services.enable_normal_runtime(scheduler_quantum=5)
        name = live_transport.live_control_transport_name(str(uuid.uuid4()))

        with live_transport.LiveControlTransport.create(name) as control:
            with live_transport.LiveCommandTransport.create(name) as command:
                with live_transport.LiveResourceTransport.create(name) as resource:
                    services.attach_live_transports(
                        control,
                        command,
                        resource,
                        command_stream_generation=123,
                    )
                    with tempfile.TemporaryDirectory(
                        ignore_cleanup_errors=True
                    ) as temp_dir:
                        executor = NativeResumableExecutor(
                            function,
                            build_dir=Path(temp_dir),
                            memory_write_callback_addresses={sentinel_address},
                        )
                        returned_to = executor.run(
                            state,
                            memory,
                            memory_write_batch_address_base=0x80000000,
                            memory_write_observer_ranges_provider=(
                                lambda: ((0x3000, 0x4000),)
                            ),
                            capture_observed_write_provenance=False,
                            direct_observed_write_transport=True,
                            max_steps=10,
                            dispatch_host_calls_in_native=True,
                            native_host_services=services,
                        )
                        executor.shutdown_normal_runtime(services)

                    write_cursor, _read_cursor = command.cursors()

        self.assertEqual(returned_to, 0x1016)
        self.assertEqual(memory.read_u32(sentinel_address), 0x11223322)
        self.assertEqual(state.get_register("eax") & 0xFF, 0x22)
        self.assertGreater(write_cursor, 0)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["read_u8_callback_count"], 0)
        self.assertEqual(performance["write_u8_callback_count"], 0)
        self.assertEqual(performance["write_u32_callback_count"], 0)
        self.assertEqual(performance["native_observed_write_span_count"], 0)
        self.assertGreaterEqual(services.native_memory_write_count, 1)
        self.assertGreaterEqual(services.live_published_record_count, 1)

    @unittest.skipUnless(os.name == "nt", "named pagefile mappings require Windows")
    def test_normal_runtime_releases_pending_spans_on_stop(self) -> None:
        program = bytearray()
        for index, value in enumerate((0x0004012C, 7)):
            program.extend(b"\xC7\x05")
            program.extend(struct.pack("<I", 0x3000 + index * 4))
            program.extend(struct.pack("<I", value))
        program.append(0xC3)
        function = lift_x86_function(
            bytes(program),
            base_address=0x1000,
            symbol="native_normal_runtime_stop",
        )
        memory = SparseMemory({0x8000: 0})
        services = NativeHostServiceState()
        services.enable_normal_runtime(scheduler_quantum=5)
        name = live_transport.live_control_transport_name(str(uuid.uuid4()))

        with live_transport.LiveControlTransport.create(name) as control:
            with live_transport.LiveCommandTransport.create(name) as command:
                with live_transport.LiveResourceTransport.create(name) as resource:
                    services.attach_live_transports(
                        control,
                        command,
                        resource,
                        command_stream_generation=123,
                    )
                    stop_poll = threading.Event()

                    def request_stop_after_manifest() -> None:
                        for _attempt in range(5000):
                            if control.manifest_available():
                                control.request_stop()
                                return
                            stop_poll.wait(0.001)

                    stopper = threading.Thread(target=request_stop_after_manifest)
                    stopper.start()
                    with tempfile.TemporaryDirectory(
                        ignore_cleanup_errors=True
                    ) as temp_dir:
                        executor = NativeResumableExecutor(
                            function,
                            build_dir=Path(temp_dir),
                        )
                        returned_to = executor.run(
                            CpuState.with_registers(esp=0x8000),
                            memory,
                            memory_write_batch_address_base=0x80000000,
                            memory_write_observer_yield_header=0x0004012C,
                            memory_write_observer_ranges_provider=(
                                lambda: ((0x3000, 0x4000),)
                            ),
                            capture_observed_write_provenance=False,
                            direct_observed_write_transport=True,
                            max_steps=20,
                            dispatch_host_calls_in_native=True,
                            native_host_services=services,
                        )
                        executor.shutdown_normal_runtime(services)
                    stop_poll.set()
                    stopper.join(timeout=5)
                    manifest_available = control.manifest_available()

        self.assertFalse(stopper.is_alive())
        self.assertTrue(manifest_available)
        self.assertEqual(returned_to, 0x1014)
        self.assertTrue(services.normal_runtime_stop_requested)
        self.assertEqual(services.normal_runtime_failure_code, 0)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["direct_observed_write_count"], 1)
        self.assertEqual(performance["observer_callback_count"], 0)

    @unittest.skipUnless(os.name == "nt", "named pagefile mappings require Windows")
    def test_normal_runtime_publishes_frame_resources_without_python(self) -> None:
        base_address = 0x1000
        text_target = 0x2000
        program = bytearray()
        for argument in (0x9100, 0x41C80000, 0x43AA0000, 0x43A00000, 0x9000):
            program.append(0x68)
            program.extend(struct.pack("<I", argument))
        call_address = base_address + len(program)
        program.append(0xE8)
        program.extend(struct.pack("<i", text_target - (call_address + 5)))
        push_words = (
            0x00041B00,
            0x00005000,
            0x00041B04,
            0x02210600,
            0x000417FC,
            1,
            0x000417FC,
            0,
            0x00041B00,
            0x00006000,
            0x00041B04,
            0x02210600,
            0x000417FC,
            1,
            0x00041B00,
            0x00010000,
            0x00041B04,
            0x07710529,
            0x000417FC,
            1,
            0x00041B40,
            0x00010000,
            0x00041B44,
            0x0771052D,
            0x000417FC,
            1,
            0x0004012C,
            7,
        )
        for index, value in enumerate(push_words):
            program.extend(b"\xC7\x05")
            program.extend(struct.pack("<I", 0x3000 + index * 4))
            program.extend(struct.pack("<I", value))
        program.append(0xC3)
        function = lift_x86_function(
            bytes(program),
            base_address=base_address,
            symbol="native_normal_runtime_resources",
        )
        texture = bytes(range(64))
        second_texture = bytes(reversed(range(64)))
        cubemap_face_size = 128 * 128 * 2
        cubemap = b"".join(
            bytes([face + 1]) * cubemap_face_size
            for face in range(6)
        )
        memory = SparseMemory({0x3000: 0, 0x8000: 0})
        memory.write(0x5000, texture)
        memory.write(0x6000, second_texture)
        memory.write(0x10000, cubemap)
        memory.write(0x9000, b"Loading - please wait\0")
        memory.write(
            0x9100,
            struct.pack("<ffff", 255.0, 128.0, 64.0, 255.0),
        )
        services = NativeHostServiceState(
            [
                NativeHostServiceEntry(
                    text_target,
                    NATIVE_HOST_SERVICE_TITLE,
                    0x14,
                    NATIVE_HOST_SERVICE_TITLE_TEXT_DRAW,
                )
            ]
        )
        services.enable_normal_runtime(scheduler_quantum=100)
        services.replay_capture_pending = True
        services.replay_capture_start_flip_count = 0
        name = live_transport.live_control_transport_name(str(uuid.uuid4()))

        with live_transport.LiveControlTransport.create(name) as control:
            with live_transport.LiveCommandTransport.create(name) as command:
                with live_transport.LiveResourceTransport.create(name) as resource:
                    struct.pack_into(
                        "<QQ",
                        control._mapping,
                        live_transport._PRESENTATION_PAYLOAD_OFFSET,
                        123,
                        999,
                    )
                    struct.pack_into(
                        "<I",
                        control._mapping,
                        live_transport._PRESENTATION_SEQUENCE_OFFSET,
                        2,
                    )
                    services.attach_live_transports(
                        control,
                        command,
                        resource,
                        command_stream_generation=123,
                    )
                    with tempfile.TemporaryDirectory(
                        ignore_cleanup_errors=True
                    ) as temp_dir:
                        executor = NativeResumableExecutor(
                            function,
                            build_dir=Path(temp_dir),
                            callback_addresses={text_target},
                        )
                        returned_to = executor.run(
                            CpuState.with_registers(esp=0x8000),
                            memory,
                            memory_write_batch_address_base=0x80000000,
                            memory_write_observer_yield_header=0x0004012C,
                            memory_write_observer_ranges_provider=(
                                lambda: ((0x3000, 0x4000),)
                            ),
                            capture_observed_write_provenance=False,
                            direct_observed_write_transport=True,
                            max_steps=100,
                            dispatch_host_calls_in_native=True,
                            native_host_services=services,
                        )
                        executor.shutdown_normal_runtime(services)

                    metadata_offset = live_transport._RESOURCE_SLOT_METADATA_OFFSET[
                        services.live_resource_slot
                    ]
                    _sequence, payload_size, generation = (
                        live_transport._RESOURCE_SLOT_METADATA.unpack_from(
                            resource._mapping, metadata_offset
                        )
                    )
                    slot_offset = (
                        live_transport._RESOURCE_HEADER_SIZE
                        + services.live_resource_slot * resource.slot_capacity
                    )
                    payload = bytes(
                        resource._mapping[
                            slot_offset : slot_offset + payload_size
                        ]
                    )
                    command_size = command.cursors()[0]
                    command_payload = bytes(
                        command._mapping[
                            live_transport._COMMAND_DATA_OFFSET :
                            live_transport._COMMAND_DATA_OFFSET + command_size
                        ]
                    )
                    manifest_available = control.manifest_available()
                    manifest_size = struct.unpack_from(
                        "<I",
                        control._mapping,
                        live_transport._MANIFEST_SIZE_OFFSET,
                    )[0]
                    manifest_payload = bytes(
                        control._mapping[
                            live_transport._MANIFEST_PAYLOAD_OFFSET :
                            live_transport._MANIFEST_PAYLOAD_OFFSET
                            + manifest_size
                        ]
                    )

        self.assertEqual(returned_to, base_address + len(program) - 1)
        self.assertFalse(services.replay_capture_pending)
        self.assertEqual(services.replay_capture_completed_flip_count, 1)
        self.assertTrue(services.normal_runtime_stop_requested)
        self.assertEqual(generation, 1)
        self.assertTrue(manifest_available)
        _magic, _schema, manifest_record_count = (
            live_transport._MANIFEST_HEADER.unpack_from(manifest_payload)
        )
        manifest_records: dict[int, tuple[int, bytes]] = {}
        cursor = live_transport._MANIFEST_HEADER.size
        for _index in range(manifest_record_count):
            field, kind, _reserved, size = (
                live_transport._MANIFEST_RECORD.unpack_from(
                    manifest_payload, cursor
                )
            )
            cursor += live_transport._MANIFEST_RECORD.size
            manifest_records[field] = (
                kind,
                manifest_payload[cursor : cursor + size],
            )
            cursor += size
        self.assertEqual(
            struct.unpack("<Q", manifest_records[32][1])[0],
            services.live_resource_slot,
        )
        self.assertEqual(
            struct.unpack("<Q", manifest_records[33][1])[0],
            payload_size,
        )
        self.assertEqual(
            manifest_records[30][1],
            b"b2_recomp_presented_000000000000007B",
        )
        self.assertEqual(
            manifest_records[31][1],
            b"b2_recomp_published_000000000000007B",
        )
        self.assertEqual(manifest_records[4][1], b"Loading - please wait")
        self.assertEqual(
            struct.unpack("<Q", manifest_records[5][1])[0], 0x43A00000
        )
        self.assertEqual(
            struct.unpack("<Q", manifest_records[6][1])[0], 0x43AA0000
        )
        self.assertEqual(
            struct.unpack("<Q", manifest_records[7][1])[0], 0x41C80000
        )
        self.assertEqual(
            struct.unpack("<Q", manifest_records[8][1])[0], 0xFFFF8040
        )
        self.assertEqual(manifest_records[34][1], b"shared_memory_slot_v1")
        self.assertEqual(payload[:8], b"B2TEX001")
        command_header = struct.unpack_from("<BBHIII", command_payload)
        self.assertEqual(command_header[3], 0x80000000)
        direct_payload = command_payload[16 : 16 + command_header[4]]
        scan_payload = (ctypes.c_uint8 * len(direct_payload)).from_buffer_copy(
            direct_payload
        )
        scan_result = executor.scan_resource_method_spans(
            scan_payload,
            len(direct_payload),
            (ctypes.c_uint32 * 1)(command_header[3]),
            (ctypes.c_uint32 * 1)(0),
            (ctypes.c_uint32 * 1)(len(direct_payload)),
            (ctypes.c_uint8 * 1)(command_header[1]),
            1,
        )
        self.assertGreater(
            scan_result[3],
            0,
            (command_header, direct_payload.hex(), scan_result[4:]),
        )
        self.assertEqual(struct.unpack_from("<I", payload, 8)[0], 3)
        (
            stage,
            address,
            width,
            height,
            format_size,
            texture_size,
            content_hash,
        ) = struct.unpack_from("<IIIIII32s", payload, 12)
        self.assertEqual((stage, address, width, height), (0, 0x5000, 4, 4))
        self.assertEqual(texture_size, len(texture))
        format_offset = 12 + struct.calcsize("<IIIIII32s")
        self.assertEqual(
            payload[format_offset : format_offset + format_size], b"A8R8G8B8"
        )
        self.assertEqual(
            payload[
                format_offset + format_size :
                format_offset + format_size + texture_size
            ],
            texture,
        )
        self.assertEqual(content_hash, hashlib.sha256(texture).digest())
        second_offset = format_offset + format_size + texture_size
        (
            second_stage,
            second_address,
            second_width,
            second_height,
            second_format_size,
            second_texture_size,
            second_content_hash,
        ) = struct.unpack_from("<IIIIII32s", payload, second_offset)
        self.assertEqual(
            (second_stage, second_address, second_width, second_height),
            (0, 0x6000, 4, 4),
        )
        second_format_offset = second_offset + struct.calcsize("<IIIIII32s")
        self.assertEqual(
            payload[
                second_format_offset : second_format_offset + second_format_size
            ],
            b"A8R8G8B8",
        )
        self.assertEqual(second_texture_size, len(second_texture))
        self.assertEqual(
            payload[
                second_format_offset + second_format_size :
                second_format_offset + second_format_size + second_texture_size
            ],
            second_texture,
        )
        self.assertEqual(
            second_content_hash, hashlib.sha256(second_texture).digest()
        )
        third_offset = (
            second_format_offset
            + second_format_size
            + second_texture_size
        )
        (
            third_stage,
            third_address,
            third_width,
            third_height,
            third_format_size,
            third_texture_size,
            third_content_hash,
        ) = struct.unpack_from("<IIIIII32s", payload, third_offset)
        self.assertEqual(
            (third_stage, third_address, third_width, third_height),
            (1, 0x10000, 128, 128),
        )
        third_format_offset = third_offset + struct.calcsize("<IIIIII32s")
        self.assertEqual(
            payload[
                third_format_offset : third_format_offset + third_format_size
            ],
            b"R5G6B5",
        )
        self.assertEqual(third_texture_size, len(cubemap))
        self.assertEqual(
            payload[
                third_format_offset + third_format_size :
                third_format_offset + third_format_size + third_texture_size
            ],
            cubemap,
        )
        self.assertEqual(
            third_content_hash, hashlib.sha256(cubemap).digest()
        )
        self.assertEqual(services.live_resource_publish_count, 1)
        self.assertEqual(services.live_manifest_publish_count, 1)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["slice_yield_count"], 0)
        self.assertEqual(performance["handler_call_count"], 0)

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
