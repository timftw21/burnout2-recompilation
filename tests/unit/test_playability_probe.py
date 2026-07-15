from __future__ import annotations

import json
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime.xbox.shims import (
    ControllerState,
    XboxRuntimeConfig,
    XboxRuntimeShims,
    XboxStatus,
)
from tests.unit.test_xbe_info import _synthetic_xbe
from tools.loader.xbe_loader import ImportResolver, load_xbe_bytes
from tools.playability.host_audio import PcmClip
from tools.playability.playability_probe import (
    DEFAULT_MAX_BLOCK_INSTRUCTIONS,
    GUEST_ARGUMENT_COUNT_OVERRIDES,
    DynamicBlockCache,
    RuntimeAbiBridge,
    RenderWriteWatchpoint,
    LiveHostBridge,
    TitleXInputFastPath,
    TitleSubsystemInitializerAudit,
    TITLE_XINPUT_HANDLE_BASE,
    _read_dynamic_block_window,
    _scan_render_texture_bindings,
    _snapshot_render_texture_resources,
    TITLE_ALLOCATION_LIST_COUNT_ADDRESS,
    TITLE_ALLOCATION_LIST_SENTINEL_ADDRESS,
    TITLE_AUDIO_DSP_CONTROL_ADDRESS,
    TITLE_AUDIO_DSP_RESET_READY_BIT,
    TITLE_AUDIO_DSP_RESET_REQUEST_BIT,
    TITLE_AUDIO_DSP_STATUS_ADDRESS,
    TITLE_AUDIO_DSP_VOICE_COMMAND_ADDRESSES,
    TITLE_AUDIO_DSP_VOICE_COMMAND_PENDING_BIT,
    TITLE_ASSET_STREAM_OPEN_ADDRESS,
    TITLE_ASSET_STREAM_SYNTHETIC_READ_TARGET_ADDRESS,
    TITLE_ASSET_STREAM_SYNTHETIC_OBJECT_ADDRESS,
    TITLE_CLEANUP_LIST_SENTINEL_ADDRESS,
    TITLE_D3D_CONTEXT_DMA_STATE_ADDRESS,
    TITLE_D3D_CONTEXT_GET_POINTER_ADDRESS,
    TITLE_D3D_CONTEXT_GLOBAL_ADDRESS,
    TITLE_D3D_CONTEXT_LIST_COUNT_OFFSET,
    TITLE_D3D_CONTEXT_LIST_FIRST_OFFSET,
    TITLE_D3D_CONTEXT_LIST_NODE0_ADDRESS,
    TITLE_D3D_CONTEXT_LIST_NODE1_ADDRESS,
    TITLE_D3D_CONTEXT_LIST_SECOND_OFFSET,
    TITLE_D3D_CONTEXT_LIST_SEEDED_COUNT,
    TITLE_D3D_CONTEXT_MARKER_QUEUE_ADDRESS,
    TITLE_D3D_CONTEXT_SURFACE_STATE_ADDRESS,
    TITLE_D3D_CONTEXT_SYNTHETIC_ADDRESS,
    TITLE_D3D_FLUSH_ADDRESS,
    TITLE_D3D_PACKET_ALLOC_ADDRESS,
    TITLE_D3D_PACKET_ALLOC_SIZE,
    TITLE_D3D_PRIMITIVE_DRAW_ADDRESS,
    TITLE_D3D_PRIMITIVE_DRAW_STACK_CLEANUP,
    TITLE_D3D_PUSH_BUFFER_BASE_ADDRESS,
    TITLE_D3D_PUSH_BUFFER_END_ADDRESS,
    TITLE_D3D_PUSH_BUFFER_SIZE,
    TITLE_D3D_RESERVE_ADDRESS,
    TITLE_D3D_RESERVE_LIMIT_MARGIN,
    TITLE_D3D_STATE_DESCRIPTOR_BASE_ADDRESS,
    TITLE_D3D_STATE_DESCRIPTOR_OBSERVED_INDEX,
    TITLE_D3D_STATE_DESCRIPTOR_STRIDE,
    TITLE_D3D_STATE_DESCRIPTOR_SWITCH_OFFSET,
    TITLE_D3D_STATE_DESCRIPTOR_SWITCH_VALUE,
    TITLE_DIRECTSOUND_BUFFER_SYNC_ADDRESS,
    TITLE_GLOBAL_LIST_HEAD_ADDRESS,
    TITLE_GLOBAL_LIST_OWNER_FLAG,
    TITLE_GLOBAL_LIST_ACTIVE_FLAG,
    TITLE_GLOBAL_LIST_CLEANUP_ADDRESS,
    TITLE_GLOBAL_LIST_REGISTER_ADDRESS,
    TITLE_GLOBAL_LIST_TAIL_ADDRESS,
    TITLE_FRONTEND_GLOBAL_DIC_IMPACT2_KEY_ADDRESS,
    TITLE_FRONTEND_SPECIAL_AUDIO_CREATE_ADDRESS,
    TITLE_MUSIC_MODE_SET_ADDRESS,
    TITLE_MUSIC_SYNTHETIC_MANAGER_ADDRESS,
    TITLE_FRONTEND_SYNTHETIC_AUDIO_HANDLE_ADDRESS,
    TITLE_FRONTEND_GLOBAL_RESOURCE_LIST_ADDRESS,
    TITLE_FRONTEND_COMPARE_SEARCH_ADDRESS,
    TITLE_FRONTEND_COMPARE_SEARCH_CHILD_LIST_OFFSET,
    TITLE_FRONTEND_COMPARE_SEARCH_GLOBAL_ROOT_ADDRESS,
    TITLE_FRONTEND_COMPARE_SEARCH_SYNTHETIC_KEY_ADDRESS,
    TITLE_FRONTEND_COMPARE_SEARCH_SYNTHETIC_ROOT_ADDRESS,
    TITLE_FRONTEND_ASSET_INIT_ADDRESS,
    TITLE_FRONTEND_ASSET_METHOD_TRAMPOLINE_ADDRESS,
    TITLE_FRONTEND_ASSET_SECOND_METHOD_TRAMPOLINE_ADDRESS,
    TITLE_FRONTEND_OBJECT_CONSTRUCTOR_ADDRESS,
    TITLE_FRONTEND_POST_AUDIO_LIST_ADVANCE_ADDRESS,
    TITLE_FRONTEND_POST_AUDIO_LIST_SENTINEL_ADDRESS,
    TITLE_FRONTEND_INITIALIZER_LIST_SENTINEL_ADDRESS,
    TITLE_FRONTEND_RECORD_TABLE_SCAN_ADDRESS,
    TITLE_FRONTEND_RECORD_TABLE_MAX_COUNT,
    TITLE_FRONTEND_STATIC_SINGLETON_OBJECT_ADDRESS,
    TITLE_FRONTEND_STATIC_SINGLETON_AUDIO_OBJECT_ADDRESS,
    TITLE_FRONTEND_STATIC_SINGLETON_AUDIO_VTABLE_ADDRESS,
    TITLE_FRONTEND_STATIC_SINGLETON_EFFECT_OBJECT_ADDRESS,
    TITLE_FRONTEND_STATIC_SINGLETON_EFFECT_VTABLE_ADDRESS,
    TITLE_FRONTEND_STATIC_SINGLETON_RECORD_COUNT,
    TITLE_FRONTEND_STATIC_SINGLETON_RECORDS_OFFSET,
    TITLE_FRONTEND_STATIC_SINGLETON_RECORD_SIZE,
    TITLE_FRONTEND_STATIC_SINGLETON_USE_ADDRESS,
    TITLE_FRONTEND_STATIC_SINGLETON_VTABLE_ADDRESS,
    TITLE_FRONTEND_CRT_VTABLE_INITIALIZER_ADDRESS,
    TITLE_FRONTEND_CRT_VTABLE_INITIALIZER_TABLE_ENTRY_ADDRESS,
    TITLE_FRONTEND_DYNAMIC_OBJECT_OWNER_ADDRESS,
    TITLE_FRONTEND_DYNAMIC_OBJECT_POINTER_OFFSET,
    TITLE_FRONTEND_DYNAMIC_OBJECT_RESET_USE_ADDRESS,
    TITLE_RUNTIME_OBJECT_CONSTRUCTOR_SPECS,
    TITLE_RUNTIME_OBJECT_TABLE_USE_ADDRESS,
    TITLE_RUNTIME_CALLBACK_DISPATCH_ADDRESSES,
    TITLE_RUNTIME_CALLBACK_GLOBAL_OBJECT_ADDRESS,
    TITLE_RUNTIME_CALLBACK_LIST_OFFSET,
    TITLE_FRONTEND_SYNTHETIC_CHILD_DESCRIPTOR_BASE_ADDRESS,
    TITLE_FRONTEND_SYNTHETIC_CHILD_DISPATCH_INDEX,
    TITLE_FRONTEND_SYNTHETIC_CHILD_DISPATCH_INDICES,
    TITLE_FRONTEND_SYNTHETIC_CHILD_METHOD_TABLE_ADDRESS,
    TITLE_FRONTEND_SYNTHETIC_CHILD_METHOD_TARGET_ADDRESS,
    TITLE_FRONTEND_SYNTHETIC_CHILD_OBJECT_BASE_ADDRESS,
    TITLE_FRONTEND_SYNTHETIC_CHILD_STATE_BASE_ADDRESS,
    TITLE_FIXED_WIDTH_COMPARE_ADDRESS,
    TITLE_FIXED_WIDTH_COMPARE_BYTES,
    TITLE_FRONTEND_RESOURCE_CACHE_SENTINEL_ADDRESS,
    TITLE_FRONTEND_REGISTRY_LIST_SENTINEL_ADDRESS,
    TITLE_FRONTEND_SYNTHETIC_ASSET_MANAGER_ADDRESS,
    TITLE_FRONTEND_SYNTHETIC_ASSET_METHOD_TARGET_ADDRESS,
    TITLE_FRONTEND_SYNTHETIC_BOOT_RESOURCE_HANDLE_ADDRESS,
    TITLE_FRONTEND_SYNTHETIC_GLOBAL_DIC_HANDLE_ADDRESS,
    TITLE_FRONTEND_SYNTHETIC_GLOBAL_DIC_LIST_ADDRESS,
    TITLE_FRONTEND_SYNTHETIC_GLOBAL_DIC_NODE_ADDRESS,
    TITLE_FRONTEND_SYNTHETIC_GLASSB_HANDLE_ADDRESS,
    TITLE_FRONTEND_SYNTHETIC_GLASS_HANDLE_ADDRESS,
    TITLE_FRONTEND_RESOURCE_LIST_FIND_LOOP_BRANCH,
    TITLE_FRONTEND_RESOURCE_LIST_FIND_LOOP_ENTRY,
    TITLE_GPU_COMPLETION_MASK,
    TITLE_GPU_COMPLETION_REGISTER_ADDRESS,
    TITLE_GPU_COMMAND_KICK_ADDRESS,
    TITLE_GPU_COMPLETION_DMA_POINTER_OFFSET,
    TITLE_GPU_COMPLETION_DMA_STATUS_OFFSET,
    TITLE_GPU_IDLE_PUMP_LOOP_BRANCH,
    TITLE_GPU_IDLE_PUMP_LOOP_ENTRY,
    TITLE_GPU_INTERRUPT_STATUS_ADDRESS,
    TITLE_HEAP_ALLOC_TRAMPOLINE_ADDRESS,
    TITLE_HEAP_FAST_PATH_ALIGNMENT,
    TITLE_HEAP_FAST_PATH_MIN_OBJECT_SIZE,
    TITLE_HEAP_FREE_TRAMPOLINE_ADDRESS,
    TITLE_STATIC_DRIVE_ARRAY_ELEMENT_SIZE,
    TITLE_STATIC_DRIVE_ARRAY_REGISTRY_COUNT_ADDRESS,
    TITLE_STATIC_DRIVE_ARRAY_REGISTRY_DESCRIPTORS_ADDRESS,
    TITLE_STATIC_DRIVE_ARRAY_REGISTRY_OBJECTS_ADDRESS,
    TITLE_STATIC_DRIVE_ARRAY_SETUP_ADDRESS,
    TITLE_TEXT_DRAW_ADDRESS,
    TITLE_TEXT_DRAW_STACK_CLEANUP,
    TITLE_QUAD_SUBMIT_ADDRESS,
    TITLE_QUAD_SUBMIT_END_ADDRESS,
    TITLE_VERTEX_APPEND_ADDRESS,
    TITLE_VERTEX_APPEND_COUNT_OFFSET,
    TITLE_VERTEX_APPEND_DEFAULT_Z_OFFSET,
    TITLE_VERTEX_APPEND_OBJECT_ADDRESS,
    TITLE_VERTEX_APPEND_STACK_CLEANUP,
    TITLE_VERTEX_APPEND_STRIDE,
    TITLE_GPU_PFIFO_CACHE1_STATUS_ADDRESS,
    TITLE_GPU_PFIFO_IDLE_BIT,
    TITLE_GPU_PFIFO_INTERRUPT_STATUS_ADDRESS,
    TITLE_GPU_PFIFO_RUNOUT_STATUS_ADDRESS,
    TITLE_GPU_PROGRESS_COUNTER_ADDRESS,
    TITLE_GPU_SOFTWARE_COMPLETION_FLAG_ADDRESS,
    TITLE_GPU_SOFTWARE_COMPLETION_PENDING_BIT,
    TITLE_GPU_SUBMISSION_BASE_ADDRESS,
    TITLE_GPU_SUBMISSION_LIMIT_ADDRESS,
    TITLE_MCPX_FRAME_COUNTER_ADDRESS,
    TITLE_MCPX_FRAME_COUNTER_INCREMENT,
    TITLE_SPIN_DELAY_ADDRESS,
    TITLE_SPIN_DELAY_ITERATIONS,
    TITLE_STARTUP_WORK_QUEUE_LINK_PAYLOAD_BACK_OFFSET,
    TITLE_STARTUP_WORK_QUEUE_LOOP_BRANCH,
    TITLE_STARTUP_WORK_QUEUE_LOOP_ENTRY,
    SCHEDULER_LOOP_CONVERGENCE_ERROR,
    SchedulerLoopConvergenceDetector,
    TitleAllocationListCountFastPath,
    TitleAssetStreamOpenFastPath,
    TitleGuestHeapFastPath,
    TitleFrontendAssetInitFastPath,
    TitleFrontendSpecialAudioFastPath,
    TitleMusicModeFastPath,
    TitleFrontendCompareSearchSentinelRepair,
    TitleFrontendObjectConstructorFastPath,
    TitleFrontendPostAudioListRepair,
    TitleFrontendRecordTableCountRepair,
    TitleFrontendStaticSingletonRepair,
    TitleFrontendCrtInitializerAudit,
    TitleRuntimeObjectTableConstructorRepair,
    TitleRuntimeCallbackListRepair,
    TitleFixedWidthCompareFastPath,
    TitleD3DFlushFastPath,
    TitleD3DPacketAllocFastPath,
    TitleD3DPrimitiveDrawFastPath,
    TitleD3DReserveFastPath,
    TitleImmediateDrawAudit,
    TitleDirectSoundBufferSyncFastPath,
    TitleGlobalListRegistrationFastPath,
    TitleStaticDriveArrayFastPath,
    TitleSpinDelayFastPath,
    TitleTextDrawFastPath,
    TitleVertexAppendFastPath,
    XbeBackedSparseMemory,
    _asset_io_summary_from_invocations,
    _deterministic_service_validation_summary,
    _frontend_resource_boundary_from_step_limit,
    _gpu_idle_pump_boundary_from_step_limit,
    _guest_thread_requested_live_stop,
    _heap_free_list_boundary_from_step_limit,
    _recover_missing_branch_targets,
    _playability_gaps,
    _recovered_render_command_stream,
    _scheduler_boundary_from_step_limit,
    _seed_guest_thread_fs_block,
    _title_render_loop_boundary_from_step_limit,
    build_playability_probe_summary,
)
from tools.recomp.x86_lifter import (
    CpuState,
    ExecutionTrace,
    LiftedFunction,
    Operand,
    SparseMemory,
    X86Instruction,
    X86ExecutionError,
    execute_lifted_function,
    lift_x86_block,
    lift_x86_function,
)


def _call_indirect_bytes(pointer_address: int) -> bytes:
    return b"\xFF\x15" + struct.pack("<I", pointer_address) + b"\xC3"


def _jmp_indirect_bytes(pointer_address: int) -> bytes:
    return b"\xFF\x25" + struct.pack("<I", pointer_address)


def _push_u32(value: int) -> bytes:
    return b"\x68" + struct.pack("<I", value & 0xFFFFFFFF)


def _call_relative_bytes(base_address: int, target_address: int) -> bytes:
    rel = target_address - (base_address + 5)
    return b"\xE8" + struct.pack("<i", rel)


class PlayabilityProbeTests(unittest.TestCase):
    def test_subsystem_initializer_audit_records_ordered_results(self) -> None:
        audit = TitleSubsystemInitializerAudit()
        state = CpuState.with_registers(esi=7, edi=0x3000)
        state.eip = 0x0010A221
        memory = SparseMemory({0x3000: 0x0010D8A0})
        trace = ExecutionTrace()

        audit.observer(state, memory, trace, 100)
        state.eip = 0x0010A223
        state.set_register("eax", 0)
        audit.observer(state, memory, trace, 120)

        summary = audit.summary()
        self.assertEqual(summary["attempt_count"], 1)
        self.assertEqual(summary["result_count"], 1)
        self.assertEqual(summary["records"][0]["index"], 7)
        self.assertEqual(
            summary["records"][0]["initializer_hex"],
            "0x0010D8A0",
        )
        self.assertEqual(len(summary["failed_initializers"]), 1)

    def test_dynamic_block_window_clips_at_mapped_region_end(self) -> None:
        loaded = load_xbe_bytes(_synthetic_xbe()[0])
        region = next(
            region for region in loaded.arena.regions if region.kind == "section"
        )
        target = region.virtual_end - 4

        window = _read_dynamic_block_window(
            loaded,
            target,
            minimum_size=512,
            preferred_size=15360,
        )

        self.assertEqual(len(window), 4)
        self.assertEqual(window, loaded.arena.read(target, 4))

    def test_title_xinput_enumeration_opens_connected_port_zero(self) -> None:
        runtime = XboxRuntimeShims()
        runtime.input.set_controller_state(0, ControllerState(connected=True))
        fast_path = TitleXInputFastPath(runtime.input)
        memory = SparseMemory()

        devices_esp = 0x7000
        memory.write_u32(devices_esp, 0x11111111)
        memory.write_u32(devices_esp + 4, 0x28C054)
        devices_cpu = CpuState.with_registers(esp=devices_esp)
        fast_path.get_devices_handler(
            devices_cpu, memory, 0x0028D282, ExecutionTrace(enabled=False)
        )
        self.assertEqual(devices_cpu.get_register("eax"), 1)
        self.assertEqual(devices_cpu.get_register("esp"), devices_esp + 4)

        open_esp = 0x7100
        memory.write_u32(open_esp, 0x22222222)
        for index, value in enumerate((0x28C054, 0, 0, 0)):
            memory.write_u32(open_esp + 4 + index * 4, value)
        open_cpu = CpuState.with_registers(esp=open_esp)
        fast_path.open_handler(
            open_cpu, memory, 0x0028CF40, ExecutionTrace(enabled=False)
        )
        self.assertEqual(open_cpu.get_register("eax"), TITLE_XINPUT_HANDLE_BASE)
        self.assertEqual(open_cpu.get_register("esp"), open_esp + 16)

    def test_title_xinput_fast_path_marshals_original_xbox_gamepad_state(self) -> None:
        runtime = XboxRuntimeShims()
        runtime.input.set_controller_state(
            0,
            ControllerState(
                connected=True,
                buttons=0x1001,
                left_trigger=64,
                right_trigger=96,
                thumb_lx=-1234,
                thumb_ly=2345,
                thumb_rx=-3456,
                thumb_ry=4567,
            ),
        )
        fast_path = TitleXInputFastPath(runtime.input)
        memory = SparseMemory()
        state_address = 0xA000
        esp = 0x8000
        memory.write_u32(esp, 0xDEADBEEF)
        memory.write_u32(esp + 4, TITLE_XINPUT_HANDLE_BASE)
        memory.write_u32(esp + 8, state_address)
        cpu = CpuState.with_registers(esp=esp)

        fast_path.get_state_handler(
            cpu, memory, 0x0028D180, ExecutionTrace(enabled=False)
        )

        self.assertEqual(cpu.get_register("eax"), 0)
        self.assertEqual(memory.read_u32(state_address), 1)
        self.assertEqual(memory.read(state_address + 4, 2), bytes.fromhex("0100"))
        self.assertEqual(memory.read(state_address + 6, 4), bytes((255, 0, 0, 0)))
        self.assertEqual(memory.read(state_address + 12, 2), bytes((64, 96)))
        self.assertEqual(
            struct.unpack("<hhhh", memory.read(state_address + 14, 8)),
            (-1234, 2345, -3456, 4567),
        )
        self.assertEqual(cpu.get_register("esp"), esp + 8)
        input_summary = fast_path.summary()
        self.assertEqual(input_summary["successful_get_state_count"], 1)
        self.assertEqual(input_summary["observed_button_mask_hex"], "0x00001001")
        self.assertEqual(input_summary["a_pressed_poll_count"], 1)

        runtime.input.set_controller_state(0, ControllerState(connected=True))
        released_address = 0xA100
        released_esp = 0x8100
        memory.write_u32(released_esp, 0xDEADBEEF)
        memory.write_u32(released_esp + 4, TITLE_XINPUT_HANDLE_BASE)
        memory.write_u32(released_esp + 8, released_address)
        released_cpu = CpuState.with_registers(esp=released_esp)
        fast_path.get_state_handler(
            released_cpu, memory, 0x0028D180, ExecutionTrace(enabled=False)
        )
        self.assertEqual(memory.read_u32(released_address), 2)
        self.assertEqual(memory.read(released_address + 4, 10), bytes(10))

    def test_device_io_control_uses_observed_ten_argument_guest_contract(self) -> None:
        self.assertEqual(GUEST_ARGUMENT_COUNT_OVERRIDES["NtDeviceIoControlFile"], 10)

    def test_render_watchpoint_retains_delayed_capture_window(self) -> None:
        watchpoint = RenderWriteWatchpoint(max_writes=4, stop_after=5, capture_after=2)

        watchpoint.observe(0x80000000, b"\x01\x00\x00\x00")
        watchpoint.observe(0x80000004, b"\x02\x00\x00\x00")
        watchpoint.observe(0x80000008, b"\x03\x00\x00\x00")

        stream = watchpoint.to_stream()
        self.assertEqual(stream["write_count"], 3)
        self.assertEqual(stream["skipped_write_count"], 2)
        self.assertEqual(stream["captured_write_count"], 1)
        self.assertEqual(stream["writes"][0]["value"], 3)
        self.assertEqual(len(watchpoint.history_stream()["writes"]), 3)

    def test_texture_snapshot_uses_lightweight_push_buffer_scan(self) -> None:
        watchpoint = RenderWriteWatchpoint()
        texture_address = 0x22001000
        texture_format = (2 << 24) | (2 << 20) | (1 << 16) | (0x0C << 8) | (2 << 4)
        packet = struct.pack("<III", (2 << 18) | 0x1B00, texture_address, texture_format)
        watchpoint.observe(0x80000000, packet)
        watchpoint.observe(
            0x80000100,
            struct.pack("<II", (1 << 18) | 0x17FC, 6),
        )
        bindings = _scan_render_texture_bindings(watchpoint.history_stream()["writes"])
        self.assertEqual(bindings, [(0, texture_address, texture_format)])

        payload = bytes.fromhex("00F8E00700000000")
        memory = SparseMemory()
        memory.write(texture_address, payload)
        stream = _snapshot_render_texture_resources(
            watchpoint.to_stream(), watchpoint.history_stream(), memory
        )
        self.assertEqual(stream["resource_snapshot_count"], 1)
        self.assertEqual(stream["resource_snapshots"][0]["format"], "DXT1")
        self.assertEqual(stream["resource_snapshots"][0]["bytes_hex"], payload.hex().upper())

    def test_texture_snapshot_cache_reuses_unchanged_pages_and_invalidates_writes(self) -> None:
        class CountingMemory(SparseMemory):
            def __init__(self) -> None:
                super().__init__()
                self.large_reads = 0

            def read(self, address: int, size: int) -> bytes:
                if size >= 8:
                    self.large_reads += 1
                return super().read(address, size)

        watchpoint = RenderWriteWatchpoint()
        texture_address = 0x22001000
        texture_format = (2 << 24) | (2 << 20) | (1 << 16) | (0x0C << 8) | (2 << 4)
        packet = struct.pack("<III", (2 << 18) | 0x1B00, texture_address, texture_format)
        watchpoint.observe(0x80000000, packet)
        watchpoint.observe(
            0x80000100,
            struct.pack("<II", (1 << 18) | 0x17FC, 6),
        )
        memory = CountingMemory()
        memory.write(texture_address, bytes.fromhex("00F8E00700000000"))
        cache = {}

        first = _snapshot_render_texture_resources(
            watchpoint.to_stream(),
            watchpoint.resource_binding_stream(),
            memory,
            cache=cache,
        )
        second = _snapshot_render_texture_resources(
            watchpoint.to_stream(),
            watchpoint.resource_binding_stream(),
            memory,
            cache=cache,
        )
        self.assertEqual(memory.large_reads, 1)
        self.assertEqual(first["resource_snapshots"], second["resource_snapshots"])

        memory.write(texture_address, bytes.fromhex("1FF8E00700000000"))
        third = _snapshot_render_texture_resources(
            watchpoint.to_stream(),
            watchpoint.resource_binding_stream(),
            memory,
            cache=cache,
        )
        self.assertEqual(memory.large_reads, 2)
        self.assertNotEqual(
            second["resource_snapshots"][0]["sha256"],
            third["resource_snapshots"][0]["sha256"],
        )

    def test_texture_snapshot_ignores_transient_offset_format_pairs(self) -> None:
        watchpoint = RenderWriteWatchpoint()
        background_address = 0x22001000
        logo_address = 0x22041000
        background_format = (9 << 24) | (9 << 20) | (1 << 16) | (0x0C << 8) | (2 << 4)
        logo_format = (7 << 24) | (8 << 20) | (1 << 16) | (0x0F << 8) | (2 << 4)
        words = (
            (2 << 18) | 0x1B00,
            background_address,
            background_format,
            (1 << 18) | 0x17FC,
            6,
            (1 << 18) | 0x17FC,
            0,
            (1 << 18) | 0x1B04,
            logo_format,
            (1 << 18) | 0x1B00,
            logo_address,
            (1 << 18) | 0x17FC,
            6,
        )
        watchpoint.observe(0x80000000, struct.pack(f"<{len(words)}I", *words))

        self.assertEqual(
            _scan_render_texture_bindings(watchpoint.history_stream()["writes"]),
            [
                (0, background_address, background_format),
                (0, logo_address, logo_format),
            ],
        )

    def test_texture_snapshot_retains_draw_binding_beyond_bounded_write_history(self) -> None:
        watchpoint = RenderWriteWatchpoint(max_writes=2)
        texture_address = 0x22001000
        texture_format = (2 << 24) | (2 << 20) | (1 << 16) | (0x0C << 8) | (2 << 4)
        words = (
            (2 << 18) | 0x1B00,
            texture_address,
            texture_format,
            (1 << 18) | 0x17FC,
            6,
        )
        watchpoint.observe(0x80000000, struct.pack(f"<{len(words)}I", *words))
        watchpoint.observe(0x80000100, struct.pack("<II", (1 << 18) | 0x0304, 1))
        watchpoint.observe(0x80000200, struct.pack("<II", (1 << 18) | 0x0304, 0))
        payload = bytes.fromhex("00F8E00700000000")
        memory = SparseMemory()
        memory.write(texture_address, payload)

        history = watchpoint.history_stream()
        stream = _snapshot_render_texture_resources(watchpoint.to_stream(), history, memory)

        self.assertTrue(history["truncated"])
        self.assertEqual(history["texture_bindings"], [[0, texture_address, texture_format]])
        self.assertEqual(stream["resource_snapshot_count"], 1)
        self.assertEqual(stream["resource_snapshots"][0]["format"], "DXT1")

    def test_xbe_memory_aliases_cpu_gpu_physical_surface_addresses(self) -> None:
        loaded = load_xbe_bytes(_synthetic_xbe()[0])
        memory = XbeBackedSparseMemory(loaded)
        physical_address = 0x22080080
        cpu_alias = physical_address | 0x80000000

        memory.write(cpu_alias, bytes.fromhex("8877665544332211"))
        self.assertEqual(
            memory.read(physical_address, 8),
            bytes.fromhex("8877665544332211"),
        )

        memory.write_u32(physical_address + 8, 0xAABBCCDD)
        self.assertEqual(memory.read_u32(cpu_alias + 8), 0xAABBCCDD)

    def test_native_page_writeback_commits_exact_range_without_host_invalidation(self) -> None:
        loaded = load_xbe_bytes(_synthetic_xbe()[0])
        observed_writes: list[tuple[int, bytes]] = []
        memory = XbeBackedSparseMemory(
            loaded,
            write_observer=lambda address, payload: observed_writes.append(
                (address, payload)
            ),
        )

        memory.write_native_page_range(0x22080084, b"\x11\x22\x33\x44")

        self.assertEqual(memory.read(0x22080084, 4), b"\x11\x22\x33\x44")
        self.assertEqual(observed_writes, [(0x22080084, b"\x11\x22\x33\x44")])
        self.assertEqual(memory.page_generation(0x22080084), 1)
        self.assertEqual(memory.consume_changed_pages(), set())

    def test_title_frontend_special_audio_fast_path_returns_opaque_handle(self) -> None:
        runtime = XboxRuntimeShims()
        fast_path = TitleFrontendSpecialAudioFastPath(
            runtime,
            TitleAssetStreamOpenFastPath(runtime),
        )
        base_address = 0x2680
        code = bytearray(_push_u32(0))
        code.extend(
            _call_relative_bytes(
                base_address + len(code),
                TITLE_FRONTEND_SPECIAL_AUDIO_CREATE_ADDRESS,
            )
        )
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="title_frontend_special_audio_call",
        )
        state = CpuState.with_registers(ecx=0x003D6870, esp=0x8000)
        memory = SparseMemory({0x8000: 0xFEEDFACE})

        result = execute_lifted_function(
            function,
            state=state,
            memory=memory,
            call_handlers=fast_path.call_handlers(),
            max_steps=5,
        )

        self.assertEqual(result.return_address, 0xFEEDFACE)
        self.assertEqual(
            result.state.get_register("eax"),
            TITLE_FRONTEND_SYNTHETIC_AUDIO_HANDLE_ADDRESS,
        )
        self.assertEqual(
            memory.read_u32(TITLE_FRONTEND_SYNTHETIC_AUDIO_HANDLE_ADDRESS + 0x10),
            TITLE_FRONTEND_SYNTHETIC_AUDIO_HANDLE_ADDRESS + 0x0C,
        )
        self.assertEqual(fast_path.summary()["invocation_count"], 1)
        self.assertEqual(fast_path.summary()["submitted_clip_count"], 0)
        self.assertFalse(fast_path.invocations[0]["playback_started"])

    def test_title_frontend_special_audio_fast_path_submits_requested_clip(self) -> None:
        class RecordingOutput:
            def __init__(self) -> None:
                self.submissions: list[tuple[bytes, dict[str, int]]] = []

            def submit_pcm(self, payload: bytes, **fields: int) -> bool:
                self.submissions.append((payload, fields))
                return True

        runtime = XboxRuntimeShims()
        assets = TitleAssetStreamOpenFastPath(runtime)
        assets._states[assets.object_address] = {
            "payload": b"special-bank",
            "position": 0,
            "title_path": "audio/special.rws",
        }
        output = RecordingOutput()
        fast_path = TitleFrontendSpecialAudioFastPath(
            runtime, assets, output  # type: ignore[arg-type]
        )
        cpu = CpuState.with_registers(ecx=0x003D6870, esp=0x8000)
        memory = SparseMemory({0x8004: 1, 0x8000: 0xFEEDFACE})
        clips = [
            PcmClip(44100, 1, 16, b"first"),
            PcmClip(22050, 1, 16, b"second"),
        ]

        with patch(
            "tools.playability.playability_probe.parse_rws_pcm",
            return_value=clips,
        ):
            fast_path.create_handler(
                cpu,
                memory,
                TITLE_FRONTEND_SPECIAL_AUDIO_CREATE_ADDRESS,
                ExecutionTrace(),
            )

        self.assertEqual(
            output.submissions,
            [(b"second", {"sample_rate": 22050, "channels": 1, "bits_per_sample": 16})],
        )
        self.assertEqual(fast_path.summary()["submitted_clip_count"], 1)
        self.assertTrue(fast_path.invocations[0]["playback_started"])

    def test_title_music_mode_fast_path_submits_menu_track_on_transition(self) -> None:
        class RecordingOutput:
            def __init__(self) -> None:
                self.submissions: list[tuple[bytes, dict[str, int | bool]]] = []

            def submit_pcm(self, payload: bytes, **fields: int) -> bool:
                self.submissions.append((payload, fields))
                return True

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            track = root / "music0" / "trk07menust.rws"
            track.parent.mkdir(parents=True)
            track.write_bytes(b"streamed-menu-track")
            runtime = XboxRuntimeShims(XboxRuntimeConfig(extracted_disc_root=root))
            output = RecordingOutput()
            fast_path = TitleMusicModeFastPath(runtime, output)  # type: ignore[arg-type]
            base_address = 0x26C0
            code = bytearray(_push_u32(2))
            code.extend(
                _call_relative_bytes(base_address + len(code), TITLE_MUSIC_MODE_SET_ADDRESS)
            )
            code.extend(b"\xC3")
            function = lift_x86_function(
                bytes(code),
                base_address=base_address,
                symbol="title_music_mode_call",
            )
            object_holder_address = 0x9400
            state = CpuState.with_registers(ecx=object_holder_address, esp=0x8000)
            memory = SparseMemory({0x8000: 0xFEEDFACE})
            clip = PcmClip(48000, 2, 16, b"\x01\x02\x03\x04")

            with patch(
                "tools.playability.playability_probe.parse_rws_xbox_adpcm",
                return_value=clip,
            ):
                result = execute_lifted_function(
                    function,
                    state=state,
                    memory=memory,
                    call_handlers=fast_path.call_handlers(),
                    max_steps=5,
                )

            self.assertEqual(result.return_address, 0xFEEDFACE)
            self.assertEqual(
                memory.read_u32(object_holder_address),
                TITLE_MUSIC_SYNTHETIC_MANAGER_ADDRESS,
            )
            self.assertEqual(
                memory.read_u32(TITLE_MUSIC_SYNTHETIC_MANAGER_ADDRESS + 0x38),
                2,
            )
            self.assertEqual(
                output.submissions,
                [
                    (
                        clip.payload,
                        {
                            "sample_rate": 48000,
                            "channels": 2,
                            "bits_per_sample": 16,
                            "loop": True,
                        },
                    )
                ],
            )
            self.assertEqual(fast_path.summary()["decoded_track_count"], 1)
            self.assertEqual(fast_path.summary()["submitted_track_count"], 1)
            self.assertTrue(fast_path.summary()["recent_invocations"][0]["playback_started"])

    def test_title_music_mode_fast_path_reuses_validated_pcm_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            track = root / "music0" / "trk07menust.rws"
            track.parent.mkdir(parents=True)
            track.write_bytes(b"stable-streamed-menu-track")
            cache_dir = root / "cache"
            runtime = XboxRuntimeShims(XboxRuntimeConfig(extracted_disc_root=root))
            clip = PcmClip(48000, 2, 16, b"\x01\x02\x03\x04")
            first = TitleMusicModeFastPath(runtime, None, cache_dir=cache_dir)

            with patch(
                "tools.playability.playability_probe.parse_rws_xbox_adpcm",
                return_value=clip,
            ):
                self.assertEqual(first._load_menu_clip(), clip)

            second = TitleMusicModeFastPath(runtime, None, cache_dir=cache_dir)
            with patch(
                "tools.playability.playability_probe.parse_rws_xbox_adpcm",
                side_effect=AssertionError("valid cache should bypass the decoder"),
            ):
                self.assertEqual(second._load_menu_clip(), clip)

            self.assertEqual(first.summary()["cache_miss_count"], 1)
            self.assertEqual(second.summary()["cache_hit_count"], 1)
            self.assertEqual(second.summary()["decoded_track_count"], 1)

    def test_title_asset_stream_open_fast_path_uses_configured_disc_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            asset = root / "audio" / "special.rws"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(b"recovered-asset")
            runtime = XboxRuntimeShims(XboxRuntimeConfig(extracted_disc_root=root))
            fast_path = TitleAssetStreamOpenFastPath(runtime)
            base_address = 0x2700
            path_address = 0x9200
            code = bytearray()
            code.extend(_push_u32(1))
            code.extend(_push_u32(path_address))
            code.extend(
                _call_relative_bytes(
                    base_address + len(code),
                    TITLE_ASSET_STREAM_OPEN_ADDRESS,
                )
            )
            code.extend(b"\xC3")
            function = lift_x86_function(
                bytes(code),
                base_address=base_address,
                symbol="title_asset_stream_open_call",
            )
            state = CpuState.with_registers(ecx=0x34AB48, esp=0x8000)
            memory = SparseMemory(
                {0x8000: 0xFEEDFACE, path_address: b"audio/special.rws\x00"}
            )

            result = execute_lifted_function(
                function,
                state=state,
                memory=memory,
                call_handlers=fast_path.call_handlers(),
                max_steps=6,
            )

        self.assertEqual(result.return_address, 0xFEEDFACE)
        self.assertEqual(
            result.state.get_register("eax"),
            TITLE_ASSET_STREAM_SYNTHETIC_OBJECT_ADDRESS,
        )
        self.assertEqual(
            memory.read_u32(TITLE_ASSET_STREAM_SYNTHETIC_OBJECT_ADDRESS + 0x10),
            len(b"recovered-asset"),
        )
        summary = fast_path.summary()
        self.assertEqual(summary["successful_open_count"], 1)
        self.assertEqual(summary["recent_invocations"][0]["guest_path"], "D:\\audio\\special.rws")
        read_stack = 0x8100
        read_destination = 0x9300
        memory.write_u32(read_stack + 4, read_destination)
        memory.write_u32(read_stack + 8, 9)
        read_state = CpuState.with_registers(
            ecx=TITLE_ASSET_STREAM_SYNTHETIC_OBJECT_ADDRESS,
            esp=read_stack,
        )
        fast_path.read_handler(
            read_state,
            memory,
            TITLE_ASSET_STREAM_SYNTHETIC_READ_TARGET_ADDRESS,
            ExecutionTrace(),
        )
        self.assertEqual(read_state.get_register("eax"), 9)
        self.assertEqual(memory.read(read_destination, 9), b"recovered")
        self.assertEqual(fast_path.summary()["read_count"], 1)

    def test_xbe_backed_memory_clears_observed_nv2a_status_poll_bit(self) -> None:
        class DummyArena:
            def read(self, _address: int, _size: int) -> bytes:
                raise AssertionError("fallback arena should not be read")

        class DummyLoaded:
            arena = DummyArena()

        observed_writes: list[tuple[int, bytes]] = []
        memory = XbeBackedSparseMemory(
            DummyLoaded(),  # type: ignore[arg-type]
            write_observer=lambda address, payload: observed_writes.append(
                (address, payload)
            ),
        )

        memory.write_u32(0xFD100410, 0x00010000)

        self.assertEqual(observed_writes, [(0xFD100410, b"\x00\x00\x01\x00")])
        self.assertEqual(memory.read_u32(0xFD100410), 0)
        self.assertEqual(
            memory.title_hardware_completion_summary()["nv2a_status_busy_clear_count"],
            1,
        )

    def test_xbe_backed_memory_completes_observed_gpu_submission_poll(self) -> None:
        class DummyArena:
            def read(self, _address: int, _size: int) -> bytes:
                raise AssertionError("fallback arena should not be read")

        class DummyLoaded:
            arena = DummyArena()

        memory = XbeBackedSparseMemory(
            DummyLoaded(),  # type: ignore[arg-type]
            {
                TITLE_GPU_SUBMISSION_BASE_ADDRESS: 0x21B8D000,
                TITLE_GPU_COMPLETION_REGISTER_ADDRESS: 0,
            },
        )

        self.assertEqual(
            memory.read_u32(TITLE_GPU_COMPLETION_REGISTER_ADDRESS),
            0x21B8D000 & TITLE_GPU_COMPLETION_MASK,
        )
        summary = memory.title_hardware_completion_summary()
        self.assertEqual(summary["gpu_completion_poll_count"], 1)
        self.assertEqual(summary["gpu_completion_last_value_hex"], "0x01B8D000")

    def test_xbe_backed_memory_signals_dynamic_gpu_completion_on_kick(self) -> None:
        class DummyArena:
            def read(self, _address: int, size: int) -> bytes:
                return bytes(size)

        class DummyLoaded:
            arena = DummyArena()

        dma_state = 0x2238D000
        completion_address = dma_state + TITLE_GPU_COMPLETION_DMA_STATUS_OFFSET
        memory = XbeBackedSparseMemory(DummyLoaded())  # type: ignore[arg-type]
        memory.write_u32(TITLE_GPU_SUBMISSION_BASE_ADDRESS, 0x21B8D000)
        memory.write_u32(
            TITLE_GPU_SUBMISSION_BASE_ADDRESS
            + TITLE_GPU_COMPLETION_DMA_POINTER_OFFSET,
            dma_state,
        )

        memory.write_u32(TITLE_GPU_COMMAND_KICK_ADDRESS, 0x00000023)

        self.assertEqual(
            memory.read_u32(completion_address),
            0x21B8D000 & TITLE_GPU_COMPLETION_MASK,
        )
        summary = memory.title_hardware_completion_summary()
        self.assertEqual(summary["gpu_completion_signal_count"], 1)
        self.assertEqual(
            summary["gpu_completion_last_register_address_hex"],
            "0x2238D044",
        )

    def test_xbe_backed_memory_seeds_observed_gpu_submission_window(self) -> None:
        class DummyArena:
            def read(self, _address: int, size: int) -> bytes:
                return bytes(size)

        class DummyLoaded:
            arena = DummyArena()

        memory = XbeBackedSparseMemory(DummyLoaded())  # type: ignore[arg-type]

        self.assertEqual(
            memory.read_u32(TITLE_GPU_SUBMISSION_BASE_ADDRESS),
            TITLE_D3D_PUSH_BUFFER_BASE_ADDRESS,
        )
        self.assertEqual(
            memory.read_u32(TITLE_GPU_SUBMISSION_LIMIT_ADDRESS),
            TITLE_D3D_PUSH_BUFFER_END_ADDRESS,
        )
        summary = memory.title_hardware_completion_summary()
        self.assertEqual(summary["gpu_submission_window_seed_count"], 1)
        self.assertEqual(
            summary["gpu_submission_current_base_hex"],
            f"0x{TITLE_D3D_PUSH_BUFFER_BASE_ADDRESS:08X}",
        )
        self.assertEqual(
            summary["gpu_submission_current_limit_hex"],
            f"0x{TITLE_D3D_PUSH_BUFFER_END_ADDRESS:08X}",
        )

    def test_xbe_backed_memory_seeds_observed_cleanup_list_sentinel(self) -> None:
        class DummyArena:
            def read(self, _address: int, size: int) -> bytes:
                return bytes(size)

        class DummyLoaded:
            arena = DummyArena()

        memory = XbeBackedSparseMemory(  # type: ignore[arg-type]
            DummyLoaded(), enable_title_sentinel_fallbacks=True
        )

        self.assertEqual(
            memory.read_u32(TITLE_CLEANUP_LIST_SENTINEL_ADDRESS),
            TITLE_CLEANUP_LIST_SENTINEL_ADDRESS,
        )
        self.assertEqual(
            memory.read_u32(TITLE_CLEANUP_LIST_SENTINEL_ADDRESS + 4),
            TITLE_CLEANUP_LIST_SENTINEL_ADDRESS,
        )
        summary = memory.title_hardware_completion_summary()
        self.assertEqual(summary["title_cleanup_list_seed_count"], 1)
        self.assertEqual(
            summary["title_cleanup_list_next_hex"],
            f"0x{TITLE_CLEANUP_LIST_SENTINEL_ADDRESS:08X}",
        )

    def test_xbe_backed_memory_repairs_observed_cleanup_list_null_link(self) -> None:
        class DummyArena:
            def read(self, _address: int, size: int) -> bytes:
                return bytes(size)

        class DummyLoaded:
            arena = DummyArena()

        memory = XbeBackedSparseMemory(  # type: ignore[arg-type]
            DummyLoaded(), enable_title_sentinel_fallbacks=True
        )
        memory.write_u32(TITLE_CLEANUP_LIST_SENTINEL_ADDRESS, 0)
        memory.write_u32(TITLE_CLEANUP_LIST_SENTINEL_ADDRESS + 4, 0x1000A8CC)

        self.assertEqual(
            memory.read_u32(TITLE_CLEANUP_LIST_SENTINEL_ADDRESS),
            TITLE_CLEANUP_LIST_SENTINEL_ADDRESS,
        )
        self.assertEqual(
            memory.read_u32(TITLE_CLEANUP_LIST_SENTINEL_ADDRESS + 4),
            TITLE_CLEANUP_LIST_SENTINEL_ADDRESS,
        )
        self.assertEqual(
            memory.title_hardware_completion_summary()[
                "title_cleanup_list_repair_count"
            ],
            1,
        )

    def test_xbe_backed_memory_seeds_frontend_resource_cache_sentinel(self) -> None:
        class DummyArena:
            def read(self, _address: int, size: int) -> bytes:
                return bytes(size)

        class DummyLoaded:
            arena = DummyArena()

        memory = XbeBackedSparseMemory(  # type: ignore[arg-type]
            DummyLoaded(), enable_title_sentinel_fallbacks=True
        )

        self.assertEqual(
            memory.read_u32(TITLE_FRONTEND_RESOURCE_CACHE_SENTINEL_ADDRESS),
            TITLE_FRONTEND_RESOURCE_CACHE_SENTINEL_ADDRESS,
        )
        summary = memory.title_hardware_completion_summary()
        self.assertEqual(summary["title_frontend_resource_cache_seed_count"], 1)
        self.assertEqual(
            summary["title_frontend_resource_cache_next_hex"],
            f"0x{TITLE_FRONTEND_RESOURCE_CACHE_SENTINEL_ADDRESS:08X}",
        )

    def test_xbe_backed_memory_repairs_frontend_resource_cache_null_link(
        self,
    ) -> None:
        class DummyArena:
            def read(self, _address: int, size: int) -> bytes:
                return bytes(size)

        class DummyLoaded:
            arena = DummyArena()

        memory = XbeBackedSparseMemory(  # type: ignore[arg-type]
            DummyLoaded(), enable_title_sentinel_fallbacks=True
        )
        memory.write_u32(TITLE_FRONTEND_RESOURCE_CACHE_SENTINEL_ADDRESS, 0)

        self.assertEqual(
            memory.read_u32(TITLE_FRONTEND_RESOURCE_CACHE_SENTINEL_ADDRESS),
            TITLE_FRONTEND_RESOURCE_CACHE_SENTINEL_ADDRESS,
        )
        self.assertEqual(
            memory.title_hardware_completion_summary()[
                "title_frontend_resource_cache_repair_count"
            ],
            1,
        )

    def test_xbe_backed_memory_seeds_frontend_registry_list_sentinel(self) -> None:
        class DummyArena:
            def read(self, _address: int, size: int) -> bytes:
                return bytes(size)

        class DummyLoaded:
            arena = DummyArena()

        memory = XbeBackedSparseMemory(  # type: ignore[arg-type]
            DummyLoaded(), enable_title_sentinel_fallbacks=True
        )

        self.assertEqual(
            memory.read_u32(TITLE_FRONTEND_REGISTRY_LIST_SENTINEL_ADDRESS),
            TITLE_FRONTEND_REGISTRY_LIST_SENTINEL_ADDRESS,
        )
        self.assertEqual(
            memory.read_u32(TITLE_FRONTEND_REGISTRY_LIST_SENTINEL_ADDRESS + 4),
            TITLE_FRONTEND_REGISTRY_LIST_SENTINEL_ADDRESS,
        )
        summary = memory.title_hardware_completion_summary()
        self.assertEqual(summary["title_frontend_registry_list_seed_count"], 1)
        self.assertEqual(
            summary["title_frontend_registry_list_next_hex"],
            f"0x{TITLE_FRONTEND_REGISTRY_LIST_SENTINEL_ADDRESS:08X}",
        )

    def test_xbe_backed_memory_repairs_frontend_registry_list_null_link(
        self,
    ) -> None:
        class DummyArena:
            def read(self, _address: int, size: int) -> bytes:
                return bytes(size)

        class DummyLoaded:
            arena = DummyArena()

        memory = XbeBackedSparseMemory(  # type: ignore[arg-type]
            DummyLoaded(), enable_title_sentinel_fallbacks=True
        )
        memory.write_u32(TITLE_FRONTEND_REGISTRY_LIST_SENTINEL_ADDRESS, 0)
        memory.write_u32(TITLE_FRONTEND_REGISTRY_LIST_SENTINEL_ADDRESS + 4, 0)

        self.assertEqual(
            memory.read_u32(TITLE_FRONTEND_REGISTRY_LIST_SENTINEL_ADDRESS),
            TITLE_FRONTEND_REGISTRY_LIST_SENTINEL_ADDRESS,
        )
        self.assertEqual(
            memory.read_u32(TITLE_FRONTEND_REGISTRY_LIST_SENTINEL_ADDRESS + 4),
            TITLE_FRONTEND_REGISTRY_LIST_SENTINEL_ADDRESS,
        )
        self.assertEqual(
            memory.title_hardware_completion_summary()[
                "title_frontend_registry_list_repair_count"
            ],
            1,
        )

    def test_xbe_backed_memory_repairs_frontend_initializer_scalar_link(
        self,
    ) -> None:
        class DummyArena:
            def read(self, _address: int, size: int) -> bytes:
                return bytes(size)

        class DummyLoaded:
            arena = DummyArena()

        memory = XbeBackedSparseMemory(  # type: ignore[arg-type]
            DummyLoaded(), enable_title_sentinel_fallbacks=True
        )
        memory.write_u32(
            TITLE_FRONTEND_INITIALIZER_LIST_SENTINEL_ADDRESS,
            0x3FD99989,
        )

        self.assertEqual(
            memory.read_u32(TITLE_FRONTEND_INITIALIZER_LIST_SENTINEL_ADDRESS),
            TITLE_FRONTEND_INITIALIZER_LIST_SENTINEL_ADDRESS,
        )
        summary = memory.title_hardware_completion_summary()
        self.assertEqual(summary["title_frontend_initializer_list_repair_count"], 1)
        self.assertEqual(
            summary["title_frontend_initializer_list_next_hex"],
            f"0x{TITLE_FRONTEND_INITIALIZER_LIST_SENTINEL_ADDRESS:08X}",
        )

    def test_xbe_backed_memory_leaves_title_sentinels_to_guest_by_default(self) -> None:
        class DummyArena:
            def read(self, _address: int, size: int) -> bytes:
                return bytes(size)

        class DummyLoaded:
            arena = DummyArena()

        memory = XbeBackedSparseMemory(DummyLoaded())  # type: ignore[arg-type]

        self.assertEqual(memory.read_u32(TITLE_CLEANUP_LIST_SENTINEL_ADDRESS), 0)
        self.assertEqual(
            memory.read_u32(TITLE_FRONTEND_RESOURCE_CACHE_SENTINEL_ADDRESS),
            0,
        )
        summary = memory.title_hardware_completion_summary()
        self.assertFalse(summary["title_sentinel_fallbacks_enabled"])
        self.assertEqual(summary["title_cleanup_list_seed_count"], 0)
        self.assertEqual(summary["title_frontend_resource_cache_seed_count"], 0)

    def test_frontend_record_table_repair_clamps_observed_negative_count(
        self,
    ) -> None:
        table_address = 0x10005C78
        memory = SparseMemory({table_address + 4: 0xFFFFFFFF})
        state = CpuState.with_registers(edi=table_address)
        state.eip = TITLE_FRONTEND_RECORD_TABLE_SCAN_ADDRESS
        repair = TitleFrontendRecordTableCountRepair()
        trace = ExecutionTrace()

        repair.observer(state, memory, trace, 123)

        self.assertEqual(memory.read_u32(table_address + 4), 0)
        self.assertEqual(repair.summary()["repair_count"], 1)
        self.assertIn(
            "title_frontend_record_table_count_repair",
            {event["operation"] for event in trace.to_list()},
        )

        memory.write_u32(table_address + 4, 0x1001E688)
        repair.observer(state, memory, trace, 124)
        self.assertEqual(memory.read_u32(table_address + 4), 0)
        self.assertEqual(repair.summary()["repair_count"], 2)

        memory.write_u32(table_address + 4, TITLE_FRONTEND_RECORD_TABLE_MAX_COUNT)
        repair.observer(state, memory, trace, 125)
        self.assertEqual(
            memory.read_u32(table_address + 4),
            TITLE_FRONTEND_RECORD_TABLE_MAX_COUNT,
        )
        self.assertEqual(repair.summary()["repair_count"], 2)

    def test_xbe_backed_memory_acks_observed_gpu_interrupt_statuses(self) -> None:
        class DummyArena:
            def read(self, _address: int, _size: int) -> bytes:
                raise AssertionError("fallback arena should not be read")

        class DummyLoaded:
            arena = DummyArena()

        observed_writes: list[tuple[int, bytes]] = []
        memory = XbeBackedSparseMemory(
            DummyLoaded(),  # type: ignore[arg-type]
            write_observer=lambda address, payload: observed_writes.append(
                (address, payload)
            ),
        )

        memory.write_u32(TITLE_GPU_INTERRUPT_STATUS_ADDRESS, 0x00001000)

        self.assertEqual(
            observed_writes,
            [(TITLE_GPU_INTERRUPT_STATUS_ADDRESS, b"\x00\x10\x00\x00")],
        )
        self.assertEqual(memory.read_u32(TITLE_GPU_INTERRUPT_STATUS_ADDRESS), 0)
        summary = memory.title_hardware_completion_summary()
        self.assertEqual(summary["gpu_interrupt_ack_count"], 1)
        self.assertEqual(summary["gpu_interrupt_last_ack_hex"], "0x00001000")
        self.assertEqual(summary["gpu_interrupt_status_current_hex"], "0x00000000")

        memory.write_u32(TITLE_GPU_PFIFO_INTERRUPT_STATUS_ADDRESS, 0x01000000)

        self.assertEqual(memory.read_u32(TITLE_GPU_PFIFO_INTERRUPT_STATUS_ADDRESS), 0)
        summary = memory.title_hardware_completion_summary()
        self.assertEqual(summary["pfifo_interrupt_ack_count"], 1)
        self.assertEqual(summary["pfifo_interrupt_last_ack_hex"], "0x01000000")
        self.assertEqual(summary["pfifo_interrupt_status_current_hex"], "0x00000000")

    def test_xbe_backed_memory_reports_observed_pfifo_idle_status(self) -> None:
        class DummyArena:
            def read(self, _address: int, _size: int) -> bytes:
                raise AssertionError("fallback arena should not be read")

        class DummyLoaded:
            arena = DummyArena()

        memory = XbeBackedSparseMemory(DummyLoaded())  # type: ignore[arg-type]

        self.assertEqual(
            memory.read_u32(TITLE_GPU_PFIFO_CACHE1_STATUS_ADDRESS),
            TITLE_GPU_PFIFO_IDLE_BIT,
        )
        self.assertEqual(
            memory.read_u32(TITLE_GPU_PFIFO_RUNOUT_STATUS_ADDRESS),
            TITLE_GPU_PFIFO_IDLE_BIT,
        )
        self.assertEqual(memory.read(TITLE_GPU_PFIFO_CACHE1_STATUS_ADDRESS, 1), b"\x10")
        self.assertEqual(memory.read(TITLE_GPU_PFIFO_RUNOUT_STATUS_ADDRESS, 1), b"\x10")
        summary = memory.title_hardware_completion_summary()
        self.assertEqual(summary["pfifo_cache1_status_read_count"], 2)
        self.assertEqual(summary["pfifo_cache1_status_current_hex"], "0x00000010")
        self.assertEqual(summary["pfifo_runout_status_read_count"], 2)
        self.assertEqual(summary["pfifo_runout_status_current_hex"], "0x00000010")

    def test_xbe_backed_memory_advances_observed_gpu_progress_counter(self) -> None:
        class DummyArena:
            def read(self, _address: int, _size: int) -> bytes:
                raise AssertionError("fallback arena should not be read")

        class DummyLoaded:
            arena = DummyArena()

        memory = XbeBackedSparseMemory(
            DummyLoaded(),  # type: ignore[arg-type]
            {TITLE_GPU_PROGRESS_COUNTER_ADDRESS: 3},
        )

        self.assertEqual(memory.read_u32(TITLE_GPU_PROGRESS_COUNTER_ADDRESS), 4)
        self.assertEqual(memory.read_u32(TITLE_GPU_PROGRESS_COUNTER_ADDRESS), 5)
        summary = memory.title_hardware_completion_summary()
        self.assertEqual(summary["gpu_progress_counter_poll_count"], 2)
        self.assertEqual(summary["gpu_progress_counter_last_value_hex"], "0x00000005")
        self.assertEqual(summary["gpu_progress_counter_current_hex"], "0x00000005")

    def test_xbe_backed_memory_clears_observed_gpu_software_completion_flag(
        self,
    ) -> None:
        class DummyArena:
            def read(self, _address: int, _size: int) -> bytes:
                raise AssertionError("fallback arena should not be read")

        class DummyLoaded:
            arena = DummyArena()

        pending = TITLE_GPU_SOFTWARE_COMPLETION_PENDING_BIT | 0x24448B70
        memory = XbeBackedSparseMemory(
            DummyLoaded(),  # type: ignore[arg-type]
            {TITLE_GPU_SOFTWARE_COMPLETION_FLAG_ADDRESS: pending},
        )

        self.assertEqual(
            memory.read_u32(TITLE_GPU_SOFTWARE_COMPLETION_FLAG_ADDRESS),
            0x24448B70,
        )
        summary = memory.title_hardware_completion_summary()
        self.assertEqual(summary["gpu_software_completion_clear_count"], 1)
        self.assertEqual(
            summary["gpu_software_completion_last_value_hex"],
            "0x24448B70",
        )

    def test_xbe_backed_memory_advances_mcpx_frame_counter(self) -> None:
        class DummyArena:
            def read(self, _address: int, _size: int) -> bytes:
                raise AssertionError("fallback arena should not be read")

        class DummyLoaded:
            arena = DummyArena()

        memory = XbeBackedSparseMemory(DummyLoaded())  # type: ignore[arg-type]

        self.assertEqual(
            memory.read_u32(TITLE_MCPX_FRAME_COUNTER_ADDRESS),
            TITLE_MCPX_FRAME_COUNTER_INCREMENT,
        )
        self.assertEqual(
            memory.read_u32(TITLE_MCPX_FRAME_COUNTER_ADDRESS),
            TITLE_MCPX_FRAME_COUNTER_INCREMENT * 2,
        )
        summary = memory.title_hardware_completion_summary()
        self.assertEqual(summary["mcpx_frame_counter_read_count"], 2)
        self.assertEqual(
            summary["mcpx_frame_counter_last_value_hex"],
            "0x00000008",
        )
        self.assertEqual(summary["mcpx_frame_counter_current_hex"], "0x00000008")

    def test_xbe_backed_memory_completes_audio_dsp_reset_handshake(self) -> None:
        class DummyArena:
            def read(self, _address: int, _size: int) -> bytes:
                raise AssertionError("fallback arena should not be read")

        class DummyLoaded:
            arena = DummyArena()

        memory = XbeBackedSparseMemory(DummyLoaded())  # type: ignore[arg-type]

        memory.write_u32(
            TITLE_AUDIO_DSP_CONTROL_ADDRESS,
            TITLE_AUDIO_DSP_RESET_REQUEST_BIT,
        )

        self.assertEqual(
            memory.read_u32(TITLE_AUDIO_DSP_STATUS_ADDRESS),
            TITLE_AUDIO_DSP_RESET_READY_BIT,
        )
        summary = memory.title_hardware_completion_summary()
        self.assertEqual(summary["audio_dsp_reset_count"], 1)
        self.assertEqual(summary["audio_dsp_status_read_count"], 1)
        self.assertEqual(
            summary["audio_dsp_status_last_value_hex"],
            "0x00000100",
        )

    def test_xbe_backed_memory_acknowledges_audio_voice_commands(self) -> None:
        class DummyArena:
            def read(self, _address: int, _size: int) -> bytes:
                raise AssertionError("fallback arena should not be read")

        class DummyLoaded:
            arena = DummyArena()

        memory = XbeBackedSparseMemory(DummyLoaded())  # type: ignore[arg-type]

        for address in TITLE_AUDIO_DSP_VOICE_COMMAND_ADDRESSES:
            memory.write(address, bytes([TITLE_AUDIO_DSP_VOICE_COMMAND_PENDING_BIT]))
            self.assertEqual(memory.read(address, 1), b"\x00")

        summary = memory.title_hardware_completion_summary()
        self.assertEqual(summary["audio_dsp_voice_command_count"], 2)
        self.assertEqual(
            summary["audio_dsp_voice_command_last_address_hex"],
            "0xFEC0017B",
        )

    def test_xbe_backed_memory_speculative_clone_isolates_writes_and_mmio(self) -> None:
        class DummyArena:
            def read(self, _address: int, _size: int) -> bytes:
                raise AssertionError("fallback arena should not be read")

        class DummyLoaded:
            arena = DummyArena()

        observed_writes: list[tuple[int, bytes]] = []
        memory = XbeBackedSparseMemory(  # type: ignore[arg-type]
            DummyLoaded(),
            write_observer=lambda address, payload: observed_writes.append(
                (address, payload)
            ),
        )
        address = 0x10020000
        memory.write_u32(address, 0x11223344)

        speculative = memory.clone_for_speculative_execution()
        speculative.write_u32(address, 0xAABBCCDD)
        speculative.write_u32(
            TITLE_AUDIO_DSP_CONTROL_ADDRESS,
            TITLE_AUDIO_DSP_RESET_REQUEST_BIT,
        )

        self.assertEqual(memory.read_u32(address), 0x11223344)
        self.assertEqual(speculative.read_u32(address), 0xAABBCCDD)
        self.assertEqual(len(observed_writes), 1)
        self.assertEqual(
            memory.title_hardware_completion_summary()["audio_dsp_reset_count"],
            0,
        )
        self.assertEqual(
            speculative.title_hardware_completion_summary()["audio_dsp_reset_count"],
            1,
        )
        self.assertIs(speculative._loaded, memory._loaded)

    def test_xbe_backed_memory_seeds_observed_d3d_context_pointer(self) -> None:
        class DummyArena:
            def read(self, _address: int, size: int) -> bytes:
                return bytes(size)

        class DummyLoaded:
            arena = DummyArena()

        memory = XbeBackedSparseMemory(DummyLoaded())  # type: ignore[arg-type]

        self.assertEqual(
            memory.read_u32(TITLE_D3D_CONTEXT_GLOBAL_ADDRESS),
            TITLE_D3D_CONTEXT_SYNTHETIC_ADDRESS,
        )
        self.assertEqual(
            memory.read_u32(TITLE_D3D_CONTEXT_SYNTHETIC_ADDRESS),
            TITLE_D3D_PUSH_BUFFER_BASE_ADDRESS,
        )
        self.assertEqual(
            memory.read_u32(TITLE_D3D_CONTEXT_SYNTHETIC_ADDRESS + 0x04),
            TITLE_D3D_PUSH_BUFFER_END_ADDRESS,
        )
        self.assertEqual(
            memory.read_u32(TITLE_D3D_CONTEXT_SYNTHETIC_ADDRESS + 0x24),
            TITLE_D3D_PUSH_BUFFER_BASE_ADDRESS,
        )
        self.assertEqual(
            memory.read_u32(TITLE_D3D_CONTEXT_SYNTHETIC_ADDRESS + 0x28),
            TITLE_D3D_PUSH_BUFFER_END_ADDRESS,
        )
        self.assertEqual(
            memory.read_u32(TITLE_D3D_CONTEXT_SYNTHETIC_ADDRESS + 0x30),
            TITLE_D3D_CONTEXT_GET_POINTER_ADDRESS,
        )
        self.assertEqual(
            memory.read_u32(TITLE_D3D_CONTEXT_SYNTHETIC_ADDRESS + 0x38),
            0x3F,
        )
        self.assertEqual(
            memory.read_u32(TITLE_D3D_CONTEXT_SYNTHETIC_ADDRESS + 0x40),
            TITLE_D3D_PUSH_BUFFER_SIZE,
        )
        self.assertEqual(
            memory.read_u32(TITLE_D3D_CONTEXT_SYNTHETIC_ADDRESS + 0x48),
            TITLE_D3D_CONTEXT_MARKER_QUEUE_ADDRESS,
        )
        self.assertEqual(
            memory.read_u32(TITLE_D3D_CONTEXT_SYNTHETIC_ADDRESS + 0x51C),
            TITLE_D3D_CONTEXT_SURFACE_STATE_ADDRESS,
        )
        self.assertEqual(
            memory.read_u32(TITLE_D3D_CONTEXT_SYNTHETIC_ADDRESS + 0x17F4),
            TITLE_D3D_CONTEXT_DMA_STATE_ADDRESS,
        )
        self.assertEqual(
            memory.read_u32(
                TITLE_D3D_CONTEXT_SYNTHETIC_ADDRESS
                + TITLE_D3D_CONTEXT_LIST_COUNT_OFFSET
            ),
            TITLE_D3D_CONTEXT_LIST_SEEDED_COUNT,
        )
        self.assertEqual(
            memory.read_u32(
                TITLE_D3D_CONTEXT_SYNTHETIC_ADDRESS
                + TITLE_D3D_CONTEXT_LIST_FIRST_OFFSET
            ),
            TITLE_D3D_CONTEXT_LIST_NODE0_ADDRESS,
        )
        self.assertEqual(
            memory.read_u32(
                TITLE_D3D_CONTEXT_SYNTHETIC_ADDRESS
                + TITLE_D3D_CONTEXT_LIST_SECOND_OFFSET
            ),
            TITLE_D3D_CONTEXT_LIST_NODE1_ADDRESS,
        )
        self.assertEqual(memory.read_u32(TITLE_D3D_CONTEXT_LIST_NODE0_ADDRESS + 0x04), 0)
        self.assertEqual(memory.read_u32(TITLE_D3D_CONTEXT_LIST_NODE1_ADDRESS + 0x08), 0)
        self.assertEqual(memory.read_u32(TITLE_D3D_CONTEXT_GET_POINTER_ADDRESS), 0)
        self.assertEqual(
            memory.read_u32(TITLE_D3D_CONTEXT_DMA_STATE_ADDRESS + 0x44),
            0,
        )
        self.assertEqual(
            memory.read_u32(TITLE_D3D_CONTEXT_SURFACE_STATE_ADDRESS + 0x324C),
            0,
        )
        descriptor_switch_address = (
            TITLE_D3D_STATE_DESCRIPTOR_BASE_ADDRESS
            + TITLE_D3D_STATE_DESCRIPTOR_OBSERVED_INDEX
            * TITLE_D3D_STATE_DESCRIPTOR_STRIDE
            + TITLE_D3D_STATE_DESCRIPTOR_SWITCH_OFFSET
        )
        self.assertEqual(
            memory.read_u32(descriptor_switch_address),
            TITLE_D3D_STATE_DESCRIPTOR_SWITCH_VALUE,
        )

        summary = memory.title_hardware_completion_summary()
        self.assertEqual(summary["d3d_context_seed_count"], 1)
        self.assertEqual(
            summary["d3d_context_last_address_hex"],
            f"0x{TITLE_D3D_CONTEXT_SYNTHETIC_ADDRESS:08X}",
        )
        self.assertEqual(summary["d3d_context_list_seed_count"], 1)
        self.assertEqual(
            summary["d3d_context_list_last_count"],
            TITLE_D3D_CONTEXT_LIST_SEEDED_COUNT,
        )
        self.assertEqual(summary["d3d_state_descriptor_seed_count"], 1)
        self.assertEqual(
            summary["d3d_state_descriptor_last_address_hex"],
            f"0x{descriptor_switch_address:08X}",
        )
        self.assertEqual(
            summary["d3d_state_descriptor_last_value_hex"],
            f"0x{TITLE_D3D_STATE_DESCRIPTOR_SWITCH_VALUE:08X}",
        )

        self.assertEqual(
            memory.read_u32(TITLE_D3D_CONTEXT_GLOBAL_ADDRESS),
            TITLE_D3D_CONTEXT_SYNTHETIC_ADDRESS,
        )
        self.assertEqual(
            memory.title_hardware_completion_summary()["d3d_context_seed_count"],
            1,
        )

    def test_xbe_backed_memory_syncs_synthetic_d3d_get_pointer(self) -> None:
        class DummyArena:
            def read(self, _address: int, size: int) -> bytes:
                return bytes(size)

        class DummyLoaded:
            arena = DummyArena()

        memory = XbeBackedSparseMemory(DummyLoaded())  # type: ignore[arg-type]

        self.assertEqual(
            memory.read_u32(TITLE_D3D_CONTEXT_GLOBAL_ADDRESS),
            TITLE_D3D_CONTEXT_SYNTHETIC_ADDRESS,
        )
        memory.write_u32(TITLE_D3D_CONTEXT_SYNTHETIC_ADDRESS + 0x2C, 0x0A60)

        self.assertEqual(memory.read_u32(TITLE_D3D_CONTEXT_GET_POINTER_ADDRESS), 0x0A60)
        summary = memory.title_hardware_completion_summary()
        self.assertEqual(summary["d3d_get_pointer_sync_count"], 1)
        self.assertEqual(summary["d3d_get_pointer_last_value_hex"], "0x00000A60")
        self.assertEqual(summary["d3d_get_pointer_current_hex"], "0x00000A60")

    def test_xbe_backed_memory_preserves_existing_d3d_context_pointer(self) -> None:
        class DummyArena:
            def read(self, _address: int, _size: int) -> bytes:
                raise AssertionError("fallback arena should not be read")

        class DummyLoaded:
            arena = DummyArena()

        existing_context = 0x12345000
        memory = XbeBackedSparseMemory(
            DummyLoaded(),  # type: ignore[arg-type]
            {TITLE_D3D_CONTEXT_GLOBAL_ADDRESS: existing_context},
        )

        self.assertEqual(memory.read_u32(TITLE_D3D_CONTEXT_GLOBAL_ADDRESS), existing_context)
        summary = memory.title_hardware_completion_summary()
        self.assertEqual(summary["d3d_context_seed_count"], 0)
        self.assertIsNone(summary["d3d_context_last_address_hex"])

    def test_title_d3d_flush_fast_path_updates_dma_progress(self) -> None:
        base_address = 0x2000
        function = lift_x86_function(
            _call_relative_bytes(base_address, TITLE_D3D_FLUSH_ADDRESS) + b"\xC3",
            base_address=base_address,
            symbol="title_d3d_flush_call",
        )
        context_address = 0x3000
        dma_state_address = 0x5000
        fast_path = TitleD3DFlushFastPath()
        state = CpuState.with_registers(
            ecx=context_address,
            eax=0xDEADC0DE,
            esp=0x8000,
        )
        memory = SparseMemory(
            {
                0x8000: 0xFEEDFACE,
                context_address: 0x80001234,
                context_address + 0x17F4: dma_state_address,
            }
        )

        result = execute_lifted_function(
            function,
            state=state,
            memory=memory,
            call_handlers=fast_path.call_handlers(),
            max_steps=4,
        )

        self.assertEqual(result.return_address, 0xFEEDFACE)
        self.assertEqual(result.state.get_register("ecx"), context_address)
        self.assertEqual(result.state.get_register("eax"), 0)
        self.assertEqual(memory.read_u32(dma_state_address + 0x40), 0x00001234)
        self.assertEqual(fast_path.summary()["invocation_count"], 1)
        self.assertEqual(fast_path.summary()["progress_update_count"], 1)

    def test_title_d3d_packet_alloc_fast_path_emits_packet_and_advances_put(
        self,
    ) -> None:
        base_address = 0x2100
        function = lift_x86_function(
            _push_u32(0)
            + _call_relative_bytes(base_address + 5, TITLE_D3D_PACKET_ALLOC_ADDRESS)
            + b"\xC3",
            base_address=base_address,
            symbol="title_d3d_packet_alloc_call",
        )
        context_address = 0x3000
        packet_address = 0x80000080
        fast_path = TitleD3DPacketAllocFastPath()
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory(
            {
                0x8000: 0xFEEDFACE,
                TITLE_D3D_CONTEXT_GLOBAL_ADDRESS: context_address,
                context_address: packet_address,
                context_address + 0x04: 0x80010000,
                context_address + 0x24: TITLE_D3D_PUSH_BUFFER_BASE_ADDRESS,
                context_address + 0x2C: 0x20,
            }
        )

        result = execute_lifted_function(
            function,
            state=state,
            memory=memory,
            call_handlers=fast_path.call_handlers(),
            max_steps=4,
        )

        self.assertEqual(result.return_address, 0xFEEDFACE)
        self.assertEqual(result.state.get_register("esp"), 0x8004)
        self.assertEqual(result.state.get_register("eax"), 0x20)
        self.assertEqual(memory.read_u32(packet_address), 0x00041D70)
        self.assertEqual(memory.read_u32(packet_address + 0x04), 0x20)
        self.assertEqual(memory.read_u32(packet_address + 0x08), 0x00041D90)
        self.assertEqual(
            memory.read_u32(context_address),
            packet_address + TITLE_D3D_PACKET_ALLOC_SIZE,
        )
        self.assertEqual(memory.read_u32(context_address + 0x2C), 0x22)
        self.assertEqual(fast_path.summary()["invocation_count"], 1)

    def test_title_d3d_reserve_fast_path_sets_limit_and_marker_packet(
        self,
    ) -> None:
        base_address = 0x2200
        half_reserve_bytes = 0x8000
        full_reserve_bytes = 0x10000
        function = lift_x86_function(
            _push_u32(full_reserve_bytes)
            + _push_u32(half_reserve_bytes)
            + _call_relative_bytes(base_address + 10, TITLE_D3D_RESERVE_ADDRESS)
            + b"\xC3",
            base_address=base_address,
            symbol="title_d3d_reserve_call",
        )
        context_address = 0x3000
        dma_state_address = 0x5000
        get_pointer_address = 0x6000
        packet_address = TITLE_D3D_PUSH_BUFFER_BASE_ADDRESS + 0x7ED8
        put_value = 0x6A52
        fast_path = TitleD3DReserveFastPath()
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory(
            {
                0x8000: 0xFEEDFACE,
                TITLE_D3D_CONTEXT_GLOBAL_ADDRESS: context_address,
                context_address: packet_address,
                context_address + 0x04: TITLE_D3D_PUSH_BUFFER_END_ADDRESS,
                context_address + 0x24: TITLE_D3D_PUSH_BUFFER_BASE_ADDRESS,
                context_address + 0x28: TITLE_D3D_PUSH_BUFFER_END_ADDRESS,
                context_address + 0x2C: put_value,
                context_address + 0x30: get_pointer_address,
                context_address + 0x17F4: dma_state_address,
            }
        )

        result = execute_lifted_function(
            function,
            state=state,
            memory=memory,
            call_handlers=fast_path.call_handlers(),
            max_steps=6,
        )

        expected_limit = (
            TITLE_D3D_PUSH_BUFFER_END_ADDRESS - TITLE_D3D_RESERVE_LIMIT_MARGIN
        )
        expected_return = packet_address + TITLE_D3D_PACKET_ALLOC_SIZE
        self.assertEqual(result.return_address, 0xFEEDFACE)
        self.assertEqual(result.state.get_register("esp"), 0x8004)
        self.assertEqual(result.state.get_register("eax"), expected_return)
        self.assertEqual(memory.read_u32(packet_address), 0x00041D70)
        self.assertEqual(memory.read_u32(packet_address + 0x04), put_value)
        self.assertEqual(memory.read_u32(packet_address + 0x08), 0x00041D90)
        self.assertEqual(memory.read_u32(context_address), expected_return)
        self.assertEqual(memory.read_u32(context_address + 0x04), expected_limit)
        self.assertEqual(memory.read_u32(context_address + 0x2C), put_value + 2)
        self.assertEqual(
            memory.read_u32(TITLE_GPU_SUBMISSION_BASE_ADDRESS),
            expected_return,
        )
        self.assertEqual(
            memory.read_u32(TITLE_GPU_SUBMISSION_LIMIT_ADDRESS),
            expected_limit,
        )
        self.assertEqual(memory.read_u32(get_pointer_address), put_value)
        self.assertEqual(
            memory.read_u32(dma_state_address + 0x40),
            expected_return & TITLE_GPU_COMPLETION_MASK,
        )
        summary = fast_path.summary()
        self.assertEqual(summary["invocation_count"], 1)
        self.assertEqual(summary["limit_update_count"], 1)
        self.assertEqual(summary["marker_packet_count"], 1)
        self.assertEqual(summary["wrap_count"], 0)
        self.assertEqual(summary["flush_progress_update_count"], 1)

    def test_title_vertex_append_fast_path_matches_observed_record_layout(
        self,
    ) -> None:
        def f32(value: float) -> int:
            return struct.unpack("<I", struct.pack("<f", value))[0]

        base_address = 0x2300
        object_address = 0x5000
        color_vector_address = 0x9000
        code = bytearray()
        code.extend(_push_u32(f32(0.5)))
        code.extend(_push_u32(f32(0.25)))
        code.extend(_push_u32(color_vector_address))
        code.extend(_push_u32(f32(20.0)))
        code.extend(_push_u32(f32(10.0)))
        code.extend(_call_relative_bytes(base_address + len(code), TITLE_VERTEX_APPEND_ADDRESS))
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="title_vertex_append_call",
        )
        fast_path = TitleVertexAppendFastPath()
        state = CpuState.with_registers(ecx=object_address, esp=0x8000)
        memory = SparseMemory(
            {
                0x8000: 0xFEEDFACE,
                object_address + TITLE_VERTEX_APPEND_COUNT_OFFSET: 3,
                object_address + TITLE_VERTEX_APPEND_DEFAULT_Z_OFFSET: f32(1.5),
                color_vector_address + 0x00: f32(64.0),
                color_vector_address + 0x04: f32(32.0),
                color_vector_address + 0x08: f32(16.0),
                color_vector_address + 0x0C: f32(255.0),
            }
        )

        result = execute_lifted_function(
            function,
            state=state,
            memory=memory,
            call_handlers=fast_path.call_handlers(),
            max_steps=7,
        )

        record_address = object_address + 3 * TITLE_VERTEX_APPEND_STRIDE
        self.assertEqual(result.return_address, 0xFEEDFACE)
        self.assertEqual(result.state.get_register("esp"), 0x8004)
        self.assertEqual(TITLE_VERTEX_APPEND_STACK_CLEANUP, 0x14)
        self.assertEqual(result.state.get_register("eax"), object_address)
        self.assertEqual(memory.read_u32(record_address), f32(10.0))
        self.assertEqual(memory.read_u32(record_address + 0x04), f32(20.0))
        self.assertEqual(memory.read_u32(record_address + 0x08), f32(1.5))
        self.assertEqual(memory.read_u32(record_address + 0x10), 0xFF402010)
        self.assertEqual(memory.read_u32(record_address + 0x14), f32(0.25))
        self.assertEqual(memory.read_u32(record_address + 0x18), f32(0.5))
        self.assertEqual(
            memory.read_u32(object_address + TITLE_VERTEX_APPEND_COUNT_OFFSET),
            4,
        )
        summary = fast_path.summary()
        self.assertEqual(summary["invocation_count"], 1)
        self.assertEqual(summary["stack_cleanup"], 0x14)
        self.assertEqual(summary["last_vertex_index"], 3)
        self.assertEqual(summary["anomaly_counts"], {})
        self.assertEqual(
            summary["producer_callers"][0]["caller_return_address_hex"],
            f"0x{base_address + len(code) - 1:08X}",
        )
        self.assertEqual(summary["producer_callers"][0]["min_x"], 10.0)
        self.assertEqual(summary["producer_callers"][0]["max_y"], 20.0)

    def test_title_vertex_append_fast_path_captures_malformed_vertex_provenance(
        self,
    ) -> None:
        def f32(value: float) -> int:
            return struct.unpack("<I", struct.pack("<f", value))[0]

        base_address = 0x2600
        object_address = 0x5000
        color_vector_address = 0x9000
        code = bytearray()
        code.extend(_push_u32(f32(0.0)))
        code.extend(_push_u32(f32(0.0)))
        code.extend(_push_u32(color_vector_address))
        code.extend(_push_u32(f32(0.0)))
        code.extend(_push_u32(f32(320.0)))
        code.extend(_call_relative_bytes(base_address + len(code), TITLE_VERTEX_APPEND_ADDRESS))
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="malformed_title_vertex_append_call",
        )
        watchpoint = RenderWriteWatchpoint()
        fast_path = TitleVertexAppendFastPath(render_watchpoint=watchpoint)
        state = CpuState.with_registers(ecx=object_address, esp=0x8000, ebp=0x8040)
        memory = SparseMemory(
            {
                0x8000: 0xFEEDFACE,
                0x8040: 0,
                0x8044: 0x000B8B44,
                object_address + TITLE_VERTEX_APPEND_COUNT_OFFSET: 0,
                object_address + TITLE_VERTEX_APPEND_DEFAULT_Z_OFFSET: f32(1.0),
                color_vector_address + 0x00: f32(255.0),
                color_vector_address + 0x04: f32(255.0),
                color_vector_address + 0x08: f32(255.0),
                color_vector_address + 0x0C: f32(255.0),
            }
        )

        execute_lifted_function(
            function,
            state=state,
            memory=memory,
            call_handlers=fast_path.call_handlers(),
            max_steps=7,
        )

        summary = fast_path.summary()
        self.assertEqual(summary["anomaly_counts"], {"exact_center_origin": 1})
        sample = summary["anomalous_samples"][0]
        self.assertEqual(sample["x"], 320.0)
        self.assertEqual(sample["y"], 0.0)
        self.assertEqual(sample["guest_flip_count"], 0)
        self.assertEqual(sample["render_write_count"], 0)
        self.assertEqual(sample["frame_chain"][0]["return_address_hex"], "0x000B8B44")
        self.assertTrue(sample["stack_code_candidates"])
        self.assertEqual(summary["geometry_by_guest_flip"][0]["guest_flip_count"], 0)
        self.assertEqual(summary["geometry_by_guest_flip"][0]["anomaly_count"], 1)

    def test_title_vertex_append_fast_path_aggregates_quad_submit_parent(self) -> None:
        def f32(value: float) -> int:
            return struct.unpack("<I", struct.pack("<f", value))[0]

        object_address = 0x5000
        color_vector_address = 0x9000
        esp = 0x8000
        parent_return_address = 0x000B8B52
        memory = SparseMemory(
            {
                esp: 0x000C233B,
                esp + 4: f32(316.0),
                esp + 8: f32(39.0),
                esp + 12: color_vector_address,
                esp + 16: f32(0.0),
                esp + 20: f32(0.0),
                esp + 0x54: parent_return_address,
                object_address + TITLE_VERTEX_APPEND_COUNT_OFFSET: 0,
                object_address + TITLE_VERTEX_APPEND_DEFAULT_Z_OFFSET: f32(1.0),
                color_vector_address + 0x00: f32(255.0),
                color_vector_address + 0x04: f32(255.0),
                color_vector_address + 0x08: f32(255.0),
                color_vector_address + 0x0C: f32(255.0),
            }
        )
        fast_path = TitleVertexAppendFastPath()

        fast_path.append_handler(
            CpuState.with_registers(ecx=object_address, esp=esp),
            memory,
            TITLE_VERTEX_APPEND_ADDRESS,
            ExecutionTrace(enabled=False),
        )

        upstream = fast_path.summary()["upstream_producers"]
        self.assertEqual(upstream[0]["upstream_return_address_hex"], "0x000B8B52")
        self.assertEqual(upstream[0]["min_x"], 316.0)
        self.assertEqual(upstream[0]["max_y"], 39.0)

    def test_title_text_draw_fast_path_preserves_observed_call_contract(self) -> None:
        base_address = 0x2400
        string_address = 0x9100
        descriptor_address = 0x9200
        code = bytearray()
        code.extend(_push_u32(descriptor_address))
        code.extend(_push_u32(0x3F800000))
        code.extend(_push_u32(0x43AA0000))
        code.extend(_push_u32(0x43A00000))
        code.extend(_push_u32(string_address))
        code.extend(_call_relative_bytes(base_address + len(code), TITLE_TEXT_DRAW_ADDRESS))
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="title_text_draw_call",
        )
        watchpoint = RenderWriteWatchpoint()
        watchpoint.observe(0xFED00000, bytes.fromhex("01000000"))
        watchpoint.flip_count = 7
        fast_path = TitleTextDrawFastPath(render_watchpoint=watchpoint)
        state = CpuState.with_registers(ecx=0x00318A9C, esp=0x8000)
        memory = SparseMemory({0x8000: 0xFEEDFACE, string_address: b"PLAY\x00"})

        result = execute_lifted_function(
            function,
            state=state,
            memory=memory,
            call_handlers=fast_path.call_handlers(),
            max_steps=8,
        )

        self.assertEqual(result.return_address, 0xFEEDFACE)
        self.assertEqual(result.state.get_register("esp"), 0x8004)
        self.assertEqual(TITLE_TEXT_DRAW_STACK_CLEANUP, 0x14)
        summary = fast_path.summary()
        self.assertEqual(summary["invocation_count"], 1)
        self.assertEqual(summary["stack_cleanup"], 0x14)
        self.assertEqual(summary["total_character_count"], 4)
        self.assertEqual(summary["max_character_count"], 4)
        self.assertEqual(summary["last_object_address_hex"], "0x00318A9C")
        self.assertEqual(summary["last_string_address_hex"], "0x00009100")
        self.assertEqual(summary["last_character_count"], 4)
        self.assertEqual(
            summary["last_arguments"],
            [
                "0x00009100",
                "0x43A00000",
                "0x43AA0000",
                "0x3F800000",
                "0x00009200",
            ],
        )
        self.assertEqual(summary["sampled_strings"][0]["bytes_hex"], "504C4159")
        self.assertEqual(summary["sampled_strings"][0]["render_write_count"], 1)
        self.assertEqual(summary["sampled_strings"][0]["guest_flip_count"], 7)
        self.assertEqual(
            summary["sampled_strings"][0]["caller_return_address_hex"],
            f"0x{base_address + len(code) - 1:08X}",
        )
        self.assertEqual(summary["caller_return_counts"][0]["invocation_count"], 1)
        self.assertIn(
            "title_text_draw_fast_path",
            {event["operation"] for event in result.trace.to_list()},
        )

    def test_title_d3d_primitive_draw_fast_path_preserves_call_contract(self) -> None:
        base_address = 0x2500
        context_address = 0x21B70000
        put_address = 0x80001234
        arguments = [
            0x00000000,
            0x00000000,
            0x002C9F60,
            0x00000008,
            0x3F800000,
            0x005ACDD0,
        ]
        code = bytearray()
        for argument in reversed(arguments):
            code.extend(_push_u32(argument))
        code.extend(
            _call_relative_bytes(
                base_address + len(code),
                TITLE_D3D_PRIMITIVE_DRAW_ADDRESS,
            )
        )
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="title_d3d_primitive_draw_call",
        )
        fast_path = TitleD3DPrimitiveDrawFastPath(execute_fast_path=True)
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory(
            {
                0x8000: 0xFEEDFACE,
                TITLE_D3D_CONTEXT_GLOBAL_ADDRESS: context_address,
                context_address: put_address,
            }
        )

        result = execute_lifted_function(
            function,
            state=state,
            memory=memory,
            call_handlers=fast_path.call_handlers(),
            max_steps=10,
        )

        self.assertEqual(result.return_address, 0xFEEDFACE)
        self.assertEqual(result.state.get_register("esp"), 0x8004)
        self.assertEqual(result.state.get_register("eax"), put_address)
        self.assertEqual(TITLE_D3D_PRIMITIVE_DRAW_STACK_CLEANUP, 0x18)
        summary = fast_path.summary()
        self.assertEqual(summary["invocation_count"], 1)
        self.assertEqual(summary["stack_cleanup"], 0x18)
        self.assertEqual(summary["last_context_address_hex"], "0x21B70000")
        self.assertEqual(summary["last_put_address_hex"], "0x80001234")
        self.assertEqual(
            summary["last_arguments"],
            [
                "0x00000000",
                "0x00000000",
                "0x002C9F60",
                "0x00000008",
                "0x3F800000",
                "0x005ACDD0",
            ],
        )
        self.assertIn(
            "title_d3d_primitive_draw_fast_path",
            {event["operation"] for event in result.trace.to_list()},
        )

    def test_title_d3d_primitive_draw_recovered_audit_is_read_only(self) -> None:
        context_address = 0x21B70000
        put_address = 0x80001234
        arguments = [1, 2, 3, 4, 5, 6]
        memory_values = {
            0x8000: 0x000F4300,
            TITLE_D3D_CONTEXT_GLOBAL_ADDRESS: context_address,
            context_address: put_address,
        }
        memory_values.update(
            {0x8004 + index * 4: value for index, value in enumerate(arguments)}
        )
        memory = SparseMemory(memory_values)
        state = CpuState.with_registers(
            eax=0xAABBCCDD,
            esp=0x8000,
        )
        state.eip = TITLE_D3D_PRIMITIVE_DRAW_ADDRESS
        before_state = state.to_dict()
        before_stack = memory.read(0x8000, 0x20)
        audit = TitleD3DPrimitiveDrawFastPath()

        self.assertEqual(audit.call_handlers(), {})
        self.assertEqual(audit.observer_addresses, {TITLE_D3D_PRIMITIVE_DRAW_ADDRESS})
        audit.observer(
            state,
            memory,
            ExecutionTrace(enabled=False),
            123,
        )

        self.assertEqual(state.to_dict(), before_state)
        self.assertEqual(memory.read(0x8000, 0x20), before_stack)
        summary = audit.summary()
        self.assertEqual(summary["execution_mode"], "recovered_guest_code")
        self.assertEqual(summary["invocation_count"], 1)
        self.assertEqual(
            summary["last_arguments"],
            [f"0x{value:08X}" for value in arguments],
        )
        self.assertEqual(summary["last_put_address_hex"], "0x80001234")

    def test_title_immediate_draw_audit_records_submitted_geometry_read_only(
        self,
    ) -> None:
        def f32(value: float) -> int:
            return struct.unpack("<I", struct.pack("<f", value))[0]

        vertex_data = 0x9000
        stride = 0x1C
        memory_values = {
            0x8000: 0x000C1BB0,
            0x8004: 6,
            0x8008: 4,
            0x800C: vertex_data,
            0x8010: stride,
        }
        for index, (x, y) in enumerate(
            ((0.0, 0.0), (640.0, 0.0), (640.0, 480.0), (0.0, 480.0))
        ):
            memory_values[vertex_data + index * stride] = f32(x)
            memory_values[vertex_data + index * stride + 4] = f32(y)
        memory = SparseMemory(memory_values)
        state = CpuState.with_registers(eax=0xAABBCCDD, esp=0x8000)
        watchpoint = RenderWriteWatchpoint()
        watchpoint.flip_count = 12
        audit = TitleImmediateDrawAudit(render_watchpoint=watchpoint)
        state.eip = audit.draw_address
        before_state = state.to_dict()
        before_stack = memory.read(0x8000, 0x14)

        audit.observer(state, memory, ExecutionTrace(enabled=False), 123)

        self.assertEqual(state.to_dict(), before_state)
        self.assertEqual(memory.read(0x8000, 0x14), before_stack)
        summary = audit.summary()
        self.assertEqual(summary["execution_mode"], "observer_only_recovered_guest_code")
        self.assertEqual(summary["invocation_count"], 1)
        self.assertEqual(summary["invalid_submission_count"], 0)
        geometry = summary["geometry_by_guest_flip"][0]
        self.assertEqual(geometry["guest_flip_count"], 12)
        self.assertEqual(geometry["draw_call_count"], 1)
        self.assertEqual(geometry["vertex_count"], 4)
        self.assertEqual(
            (geometry["min_x"], geometry["max_x"]), (0.0, 640.0)
        )
        self.assertEqual(
            (geometry["min_y"], geometry["max_y"]), (0.0, 480.0)
        )

    def test_legacy_title_frontend_asset_seed_is_not_registered_over_real_init(self) -> None:
        object_address = 0x00400000
        fast_path = TitleFrontendAssetInitFastPath()
        state = CpuState.with_registers(ecx=object_address, esp=0x8000)
        memory = SparseMemory({0x8000: 0xFEEDFACE})
        trace = ExecutionTrace()

        self.assertNotIn(TITLE_FRONTEND_ASSET_INIT_ADDRESS, fast_path.call_handlers())
        fast_path.init_handler(
            state,
            memory,
            TITLE_FRONTEND_ASSET_INIT_ADDRESS,
            trace,
        )

        list_base = TITLE_FRONTEND_SYNTHETIC_GLOBAL_DIC_LIST_ADDRESS
        sentinel = list_base + 8
        node = TITLE_FRONTEND_SYNTHETIC_GLOBAL_DIC_NODE_ADDRESS
        link = node + 8
        self.assertEqual(state.get_register("eax"), 1)
        self.assertEqual(
            memory.read_u32(TITLE_FRONTEND_GLOBAL_RESOURCE_LIST_ADDRESS),
            list_base,
        )
        self.assertEqual(
            memory.read_u32(object_address + 0x6B950),
            TITLE_FRONTEND_SYNTHETIC_ASSET_MANAGER_ADDRESS,
        )
        self.assertEqual(
            memory.read_u32(TITLE_FRONTEND_SYNTHETIC_ASSET_MANAGER_ADDRESS + 0x18),
            TITLE_FRONTEND_SYNTHETIC_ASSET_METHOD_TARGET_ADDRESS,
        )
        self.assertEqual(
            memory.read_u32(TITLE_FRONTEND_SYNTHETIC_ASSET_MANAGER_ADDRESS + 0x1C),
            TITLE_FRONTEND_SYNTHETIC_ASSET_METHOD_TARGET_ADDRESS,
        )
        self.assertEqual(
            memory.read_u32(object_address + 0x6B978),
            TITLE_FRONTEND_SYNTHETIC_BOOT_RESOURCE_HANDLE_ADDRESS,
        )
        self.assertEqual(
            memory.read_u32(object_address + 0x6B97C),
            TITLE_FRONTEND_SYNTHETIC_GLOBAL_DIC_HANDLE_ADDRESS,
        )
        self.assertEqual(
            memory.read_u32(object_address + 0x6B984),
            TITLE_FRONTEND_SYNTHETIC_GLASSB_HANDLE_ADDRESS,
        )
        self.assertEqual(
            memory.read_u32(object_address + 0x6B988),
            TITLE_FRONTEND_SYNTHETIC_GLASS_HANDLE_ADDRESS,
        )
        self.assertEqual(memory.read_u32(sentinel), link)
        self.assertEqual(memory.read_u32(sentinel + 4), link)
        self.assertEqual(memory.read_u32(link), sentinel)
        self.assertEqual(memory.read_u32(link + 4), sentinel)
        self.assertEqual(memory.read(node + 0x10, 8), b"impact2\x00")
        summary = fast_path.summary()
        self.assertEqual(summary["invocation_count"], 1)
        self.assertEqual(summary["last_object_address_hex"], "0x00400000")
        self.assertEqual(summary["resource_path"], "Frontend/global.dic")
        self.assertIn(
            "title_frontend_asset_init_fast_path",
            {event["operation"] for event in trace.to_list()},
        )

        method_stack = 0x9000
        method_state = CpuState.with_registers(esp=method_stack)
        memory.write_u32(method_stack + 4, TITLE_FRONTEND_SYNTHETIC_ASSET_MANAGER_ADDRESS)
        fast_path.method_handler(
            method_state,
            memory,
            TITLE_FRONTEND_ASSET_METHOD_TRAMPOLINE_ADDRESS,
            ExecutionTrace(),
        )
        self.assertEqual(method_state.get_register("eax"), 1)
        self.assertEqual(fast_path.summary()["method_invocation_count"], 1)
        fast_path.method_handler(
            method_state,
            memory,
            TITLE_FRONTEND_ASSET_SECOND_METHOD_TRAMPOLINE_ADDRESS,
            ExecutionTrace(),
        )
        self.assertEqual(fast_path.summary()["method_invocation_count"], 2)

    def test_title_frontend_object_constructor_fast_path_seeds_dispatch_child(
        self,
    ) -> None:
        base_address = 0x2A00
        owner_address = 0x00443FA0
        function = lift_x86_function(
            _call_relative_bytes(
                base_address,
                TITLE_FRONTEND_OBJECT_CONSTRUCTOR_ADDRESS,
            )
            + b"\xC3",
            base_address=base_address,
            symbol="title_frontend_object_constructor_call",
        )
        fast_path = TitleFrontendObjectConstructorFastPath()
        state = CpuState.with_registers(ecx=owner_address, esp=0x8000)
        memory = SparseMemory({0x8000: 0xFEEDFACE})

        result = execute_lifted_function(
            function,
            state=state,
            memory=memory,
            call_handlers=fast_path.call_handlers(include_constructor_bypass=True),
            max_steps=4,
        )

        child_object = TITLE_FRONTEND_SYNTHETIC_CHILD_OBJECT_BASE_ADDRESS
        child_state = TITLE_FRONTEND_SYNTHETIC_CHILD_STATE_BASE_ADDRESS
        child_descriptor = TITLE_FRONTEND_SYNTHETIC_CHILD_DESCRIPTOR_BASE_ADDRESS
        self.assertEqual(result.return_address, 0xFEEDFACE)
        self.assertEqual(result.state.get_register("esp"), 0x8004)
        self.assertEqual(result.state.get_register("eax"), 1)
        self.assertEqual(memory.read_u32(owner_address + 0x58), child_object)
        self.assertEqual(memory.read_u32(owner_address + 0x5C), child_state)
        self.assertEqual(memory.read_u32(child_object), owner_address)
        self.assertEqual(memory.read_u32(child_object + 4), child_descriptor)
        self.assertEqual(memory.read_u32(child_descriptor), child_object)
        self.assertEqual(
            memory.read_u32(child_descriptor + 4),
            TITLE_FRONTEND_SYNTHETIC_CHILD_METHOD_TABLE_ADDRESS,
        )
        self.assertEqual(
            memory.read_u32(child_descriptor + 8),
            TITLE_FRONTEND_SYNTHETIC_CHILD_METHOD_TABLE_ADDRESS,
        )
        for dispatch_index in TITLE_FRONTEND_SYNTHETIC_CHILD_DISPATCH_INDICES:
            method_entry = TITLE_FRONTEND_SYNTHETIC_CHILD_METHOD_TABLE_ADDRESS + (
                dispatch_index * 8
            )
            self.assertEqual(
                memory.read_u32(method_entry),
                TITLE_FRONTEND_SYNTHETIC_CHILD_METHOD_TARGET_ADDRESS,
            )
            self.assertEqual(memory.read(method_entry + 4, 2), b"\x00\x00")
        summary = fast_path.summary()
        self.assertEqual(summary["invocation_count"], 1)
        self.assertFalse(summary["constructor_bypass_enabled"])
        self.assertNotIn(
            TITLE_FRONTEND_OBJECT_CONSTRUCTOR_ADDRESS,
            fast_path.call_handlers(),
        )
        self.assertEqual(summary["method_invocation_count"], 0)
        self.assertEqual(summary["recent_invocations"][0]["owner_address_hex"], "0x00443FA0")
        self.assertEqual(
            summary["recent_invocations"][0]["dispatch_indices"],
            list(TITLE_FRONTEND_SYNTHETIC_CHILD_DISPATCH_INDICES),
        )
        self.assertIn(0, summary["recent_invocations"][0]["dispatch_indices"])
        self.assertIn(1, summary["recent_invocations"][0]["dispatch_indices"])
        self.assertIn(3, summary["recent_invocations"][0]["dispatch_indices"])
        self.assertIn(4, summary["recent_invocations"][0]["dispatch_indices"])
        self.assertIn(6, summary["recent_invocations"][0]["dispatch_indices"])
        self.assertIn(8, summary["recent_invocations"][0]["dispatch_indices"])
        self.assertIn(9, summary["recent_invocations"][0]["dispatch_indices"])
        self.assertIn(10, summary["recent_invocations"][0]["dispatch_indices"])
        self.assertIn(
            "title_frontend_object_constructor_fast_path",
            {event["operation"] for event in result.trace.to_list()},
        )

    def test_title_frontend_child_dispatch_method_returns_success(self) -> None:
        owner_address = 0x00443FA0
        payload_address = 0x004441B8
        dispatcher = bytes.fromhex(
            "56"
            "8B742408"
            "8B4604"
            "8B5004"
            "8B44240C"
            "8B0E"
            "57"
            "8B7C2418"
            "8D04C2"
            "0FB75004"
            "57"
            "52"
            "51"
            "FF10"
            "83C40C"
            "F7D8"
            "1BC0"
            "5F"
            "23C6"
            "5E"
            "C3"
        )
        function = lift_x86_function(
            dispatcher,
            base_address=0x0010C510,
            symbol="title_frontend_virtual_dispatcher",
        )
        fast_path = TitleFrontendObjectConstructorFastPath()
        memory = SparseMemory(
            {
                0x8000: 0xFEEDFACE,
                0x8004: TITLE_FRONTEND_SYNTHETIC_CHILD_OBJECT_BASE_ADDRESS,
                0x8008: TITLE_FRONTEND_SYNTHETIC_CHILD_DISPATCH_INDEX,
                0x800C: 1,
                0x8010: payload_address,
            }
        )
        fast_path._seed_child(
            memory,
            owner_address,
            TITLE_FRONTEND_SYNTHETIC_CHILD_OBJECT_BASE_ADDRESS,
            TITLE_FRONTEND_SYNTHETIC_CHILD_STATE_BASE_ADDRESS,
            TITLE_FRONTEND_SYNTHETIC_CHILD_DESCRIPTOR_BASE_ADDRESS,
        )
        state = CpuState.with_registers(esp=0x8000, edi=0x12345678)

        result = execute_lifted_function(
            function,
            state=state,
            memory=memory,
            call_handlers=fast_path.call_handlers(),
            max_steps=64,
        )

        self.assertEqual(result.return_address, 0xFEEDFACE)
        self.assertEqual(result.state.get_register("esp"), 0x8004)
        self.assertEqual(
            result.state.get_register("eax"),
            TITLE_FRONTEND_SYNTHETIC_CHILD_OBJECT_BASE_ADDRESS,
        )
        self.assertEqual(result.state.get_register("edi"), 0x12345678)
        summary = fast_path.summary()
        self.assertEqual(summary["method_invocation_count"], 1)
        self.assertEqual(
            summary["recent_method_invocations"][0]["context_hex"],
            "0x00443FA0",
        )
        self.assertEqual(summary["recent_method_invocations"][0]["selector"], 0)
        self.assertEqual(
            summary["recent_method_invocations"][0]["payload_hex"],
            "0x004441B8",
        )
        self.assertIn(
            "title_frontend_child_method_fast_path",
            {event["operation"] for event in result.trace.to_list()},
        )

    def test_title_frontend_child_slot_four_writes_selected_object(self) -> None:
        fast_path = TitleFrontendObjectConstructorFastPath()
        payload_address = 0x9000
        state = CpuState.with_registers(
            eax=TITLE_FRONTEND_SYNTHETIC_CHILD_METHOD_TABLE_ADDRESS + 4 * 8,
            esp=0x8000,
        )
        memory = SparseMemory(
            {
                0x8000: 0xFEEDFACE,
                0x8004: 0x00443FA0,
                0x8008: 0,
                0x800C: payload_address,
                payload_address: 4,
            }
        )

        fast_path.method_handler(
            state,
            memory,
            TITLE_FRONTEND_SYNTHETIC_CHILD_METHOD_TARGET_ADDRESS,
            ExecutionTrace(),
        )

        self.assertEqual(
            memory.read_u32(payload_address),
            TITLE_FRONTEND_SYNTHETIC_CHILD_OBJECT_BASE_ADDRESS,
        )
        invocation = fast_path.summary()["recent_method_invocations"][0]
        self.assertEqual(invocation["dispatch_index"], 4)
        self.assertEqual(invocation["output_object_hex"], "0x31F06000")

    def test_fixed_width_compare_fast_path_matches_observed_helper(self) -> None:
        fast_path = TitleFixedWidthCompareFastPath()
        trace = ExecutionTrace()
        memory = SparseMemory(
            {
                0x8000: 0xFEEDFACE,
                0x8004: 0x9000,
                0x8008: 0x9100,
                0x9000: b"ABCDEFGHIJKLMNOP",
                0x9100: b"ABCDEFGHIJKLMNOP",
            }
        )
        state = CpuState.with_registers(esp=0x8000)

        fast_path.compare_handler(
            state,
            memory,
            TITLE_FIXED_WIDTH_COMPARE_ADDRESS,
            trace,
        )

        self.assertEqual(state.get_register("eax"), 0)
        self.assertEqual(state.get_register("esp"), 0x8000)

        memory.write(0x9000, b"ABCD")
        memory.write(0x9100, b"ABCE")
        fast_path.compare_handler(
            state,
            memory,
            TITLE_FIXED_WIDTH_COMPARE_ADDRESS,
            trace,
        )
        self.assertEqual(state.get_register("eax"), 0xFFFFFFFF)

        memory.write(0x9000, b"ABCF")
        fast_path.compare_handler(
            state,
            memory,
            TITLE_FIXED_WIDTH_COMPARE_ADDRESS,
            trace,
        )
        self.assertEqual(state.get_register("eax"), 1)
        summary = fast_path.summary()
        self.assertEqual(summary["compare_size"], TITLE_FIXED_WIDTH_COMPARE_BYTES)
        self.assertFalse(summary["compare_bypass_enabled"])
        self.assertNotIn(
            TITLE_FIXED_WIDTH_COMPARE_ADDRESS,
            fast_path.call_handlers(),
        )
        self.assertIn(
            TITLE_FIXED_WIDTH_COMPARE_ADDRESS,
            fast_path.call_handlers(include_compare_bypass=True),
        )
        self.assertEqual(summary["invocation_count"], 3)
        self.assertEqual(summary["equal_count"], 1)
        self.assertEqual(summary["less_count"], 1)
        self.assertEqual(summary["greater_count"], 1)
        self.assertEqual(
            trace.to_list()[0]["operation"],
            "title_fixed_width_compare_fast_path",
        )

    def test_frontend_compare_search_repairs_missing_child_list_sentinel(self) -> None:
        repair = TitleFrontendCompareSearchSentinelRepair()
        trace = ExecutionTrace()
        stack = 0x8000
        root = 0x19000
        sentinel = root + TITLE_FRONTEND_COMPARE_SEARCH_CHILD_LIST_OFFSET
        memory = SparseMemory(
            {
                stack: 0xDEADC0DE,
                stack + 4: 0,
                stack + 8: 1,
                stack + 0x0C: 0x0010D260,
                stack + 0x10: 0x9100,
                stack + 0x14: 0x9200,
                TITLE_FRONTEND_COMPARE_SEARCH_GLOBAL_ROOT_ADDRESS: root,
            }
        )
        state = CpuState.with_registers(esp=stack)
        state.eip = TITLE_FRONTEND_COMPARE_SEARCH_ADDRESS

        repair.observer(state, memory, trace, 123)

        self.assertEqual(memory.read_u32(sentinel), sentinel)
        self.assertEqual(memory.read_u32(sentinel + 4), sentinel)
        summary = repair.summary()
        self.assertEqual(summary["invocation_count"], 1)
        self.assertEqual(summary["repair_count"], 1)
        self.assertEqual(summary["repaired_next_count"], 1)
        self.assertEqual(summary["repaired_previous_count"], 1)
        self.assertEqual(summary["samples"][0]["root_hex"], "0x00019000")
        self.assertEqual(summary["call_site_counts"], {"0xDEADC0DE": 1})
        self.assertEqual(
            trace.to_list()[0]["operation"],
            "title_frontend_compare_search_sentinel_repair",
        )

        repair.observer(state, memory, trace, 124)
        self.assertEqual(repair.summary()["invocation_count"], 2)
        self.assertEqual(repair.summary()["repair_count"], 1)

    def test_frontend_compare_search_does_not_repair_wrapped_root(self) -> None:
        repair = TitleFrontendCompareSearchSentinelRepair()
        trace = ExecutionTrace()
        stack = 0x8000
        memory = SparseMemory(
            {
                stack: 0x0010D1C3,
                stack + 4: 0xFFFFFFE8,
                stack + 8: 0,
                stack + 0x0C: 0x0010D260,
                stack + 0x10: 0x9100,
                stack + 0x14: 1,
            }
        )
        state = CpuState.with_registers(esp=stack)
        state.eip = TITLE_FRONTEND_COMPARE_SEARCH_ADDRESS

        repair.observer(state, memory, trace, 7)

        self.assertEqual(memory.read_u32(0xFFFFFFF8), 0)
        self.assertEqual(repair.summary()["repair_count"], 0)
        self.assertEqual(repair.summary()["null_root_count"], 1)
        self.assertFalse(repair.summary()["samples"][0]["valid_root"])

    def test_frontend_compare_search_seeds_missing_global_root(self) -> None:
        repair = TitleFrontendCompareSearchSentinelRepair()
        trace = ExecutionTrace()
        stack = 0x8000
        memory = SparseMemory(
            {
                stack: 0x0010D4CA,
                stack + 4: 0,
                stack + 8: 0x8100,
                stack + 0x0C: 0x0010D260,
                stack + 0x10: 0x8200,
                stack + 0x14: 1,
            }
        )
        state = CpuState.with_registers(esp=stack)
        state.eip = TITLE_FRONTEND_COMPARE_SEARCH_ADDRESS

        repair.observer(state, memory, trace, 9)

        root = TITLE_FRONTEND_COMPARE_SEARCH_SYNTHETIC_ROOT_ADDRESS
        sentinel = root + TITLE_FRONTEND_COMPARE_SEARCH_CHILD_LIST_OFFSET
        self.assertEqual(
            memory.read_u32(TITLE_FRONTEND_COMPARE_SEARCH_GLOBAL_ROOT_ADDRESS),
            root,
        )
        self.assertEqual(
            memory.read_u32(root), TITLE_FRONTEND_COMPARE_SEARCH_SYNTHETIC_KEY_ADDRESS
        )
        self.assertEqual(memory.read(sentinel, 8), struct.pack("<II", sentinel, sentinel))
        self.assertEqual(memory.read(TITLE_FRONTEND_COMPARE_SEARCH_SYNTHETIC_KEY_ADDRESS, 16), bytes(16))
        self.assertEqual(repair.summary()["synthetic_root_seed_count"], 1)
        self.assertIn(
            "title_frontend_compare_search_root_seed",
            {event["operation"] for event in trace.to_list()},
        )

    def test_directsound_buffer_sync_fast_path_returns_success_without_stack_cleanup(self) -> None:
        fast_path = TitleDirectSoundBufferSyncFastPath()
        trace = ExecutionTrace()
        object_address = 0x18001000
        memory = SparseMemory(
            {
                object_address + 8: 0x18002000,
                object_address + 0x20: 0x18003000,
            }
        )
        state = CpuState.with_registers(ecx=object_address, eax=0x80004005, esp=0x8000)

        fast_path.sync_handler(
            state,
            memory,
            TITLE_DIRECTSOUND_BUFFER_SYNC_ADDRESS,
            trace,
        )

        self.assertEqual(state.get_register("eax"), 0)
        self.assertEqual(state.get_register("esp"), 0x8000)
        summary = fast_path.summary()
        self.assertEqual(summary["invocation_count"], 1)
        self.assertEqual(summary["samples"][0]["sound_object_hex"], "0x18002000")
        self.assertEqual(
            trace.to_list()[0]["operation"],
            "title_directsound_buffer_sync_fast_path",
        )

    def test_directsound_effect_image_fast_path_returns_workspace_and_cleans_arguments(self) -> None:
        fast_path = TitleDirectSoundBufferSyncFastPath()
        trace = ExecutionTrace()
        output_address = 0x18004000
        memory = SparseMemory(
            {
                0x8000: 0xFEEDFACE,
                0x8004: 0x18002000,
                0x8008: 0x818,
                0x800C: output_address,
            }
        )
        state = CpuState.with_registers(ecx=0, eax=0x80004005, esp=0x8000)

        fast_path.effect_image_handler(
            state,
            memory,
            fast_path.effect_image_address,
            trace,
        )

        self.assertEqual(state.get_register("eax"), 0)
        self.assertEqual(state.get_register("esp"), 0x800C)
        self.assertEqual(
            memory.read_u32(output_address),
            fast_path.workspace_address,
        )
        self.assertEqual(memory.read_u32(fast_path.workspace_address), 0x818)
        self.assertEqual(
            trace.to_list()[0]["operation"],
            "title_directsound_effect_image_fast_path",
        )

    def test_frontend_post_audio_list_repair_closes_null_next_link(self) -> None:
        repair = TitleFrontendPostAudioListRepair()
        trace = ExecutionTrace()
        link = 0x18004000
        memory = SparseMemory({link: 0})
        state = CpuState.with_registers(esi=link)
        state.eip = TITLE_FRONTEND_POST_AUDIO_LIST_ADVANCE_ADDRESS

        repair.observer(state, memory, trace, 77)

        self.assertEqual(
            memory.read_u32(link), TITLE_FRONTEND_POST_AUDIO_LIST_SENTINEL_ADDRESS
        )
        self.assertEqual(repair.summary()["repair_count"], 1)
        self.assertEqual(
            trace.to_list()[0]["operation"],
            "title_frontend_post_audio_list_repair",
        )

    def test_frontend_static_singleton_repair_matches_constructor_contract(self) -> None:
        repair = TitleFrontendStaticSingletonRepair()
        trace = ExecutionTrace()
        memory = SparseMemory()
        state = CpuState.with_registers()
        state.eip = TITLE_FRONTEND_STATIC_SINGLETON_USE_ADDRESS

        repair.observer(state, memory, trace, 88)

        object_address = TITLE_FRONTEND_STATIC_SINGLETON_OBJECT_ADDRESS
        self.assertEqual(
            memory.read_u32(object_address),
            TITLE_FRONTEND_STATIC_SINGLETON_VTABLE_ADDRESS,
        )
        self.assertEqual(memory.read(object_address + 0x10C, 16), bytes(16))
        for index in range(TITLE_FRONTEND_STATIC_SINGLETON_RECORD_COUNT):
            record = (
                object_address
                + TITLE_FRONTEND_STATIC_SINGLETON_RECORDS_OFFSET
                + index * TITLE_FRONTEND_STATIC_SINGLETON_RECORD_SIZE
            )
            self.assertEqual(memory.read_u32(record), 1)
            self.assertEqual(
                memory.read(record + 4, TITLE_FRONTEND_STATIC_SINGLETON_RECORD_SIZE - 4),
                bytes(TITLE_FRONTEND_STATIC_SINGLETON_RECORD_SIZE - 4),
            )
        self.assertEqual(
            memory.read_u32(TITLE_FRONTEND_STATIC_SINGLETON_AUDIO_OBJECT_ADDRESS),
            TITLE_FRONTEND_STATIC_SINGLETON_AUDIO_VTABLE_ADDRESS,
        )
        self.assertEqual(
            memory.read_u32(TITLE_FRONTEND_STATIC_SINGLETON_EFFECT_OBJECT_ADDRESS),
            TITLE_FRONTEND_STATIC_SINGLETON_EFFECT_VTABLE_ADDRESS,
        )
        self.assertEqual(repair.summary()["repair_count"], 3)
        self.assertEqual(
            trace.to_list()[0]["operation"],
            "title_frontend_static_singleton_repair",
        )

        repair.observer(state, memory, trace, 89)
        self.assertEqual(repair.summary()["observation_count"], 2)
        self.assertEqual(repair.summary()["repair_count"], 3)

    def test_frontend_crt_initializer_audit_reports_without_mutating_guest_state(
        self,
    ) -> None:
        audit = TitleFrontendCrtInitializerAudit()
        trace = ExecutionTrace()
        object_address = 0x004BABE0
        vtable_address = 0x00295E08
        method0_target = 0x000558D0
        owner_pointer = (
            TITLE_FRONTEND_DYNAMIC_OBJECT_OWNER_ADDRESS
            + TITLE_FRONTEND_DYNAMIC_OBJECT_POINTER_OFFSET
        )
        memory = SparseMemory(
            {
                owner_pointer: object_address,
                object_address: vtable_address,
                vtable_address: method0_target,
                TITLE_FRONTEND_CRT_VTABLE_INITIALIZER_TABLE_ENTRY_ADDRESS: (
                    TITLE_FRONTEND_CRT_VTABLE_INITIALIZER_ADDRESS
                ),
            }
        )
        state = CpuState.with_registers(
            esi=TITLE_FRONTEND_DYNAMIC_OBJECT_OWNER_ADDRESS,
        )
        state.eip = TITLE_FRONTEND_CRT_VTABLE_INITIALIZER_ADDRESS
        audit.observer(state, memory, trace, 89)
        state.eip = TITLE_FRONTEND_DYNAMIC_OBJECT_RESET_USE_ADDRESS

        audit.observer(state, memory, trace, 90)
        summary = audit.summary()
        self.assertEqual(
            summary["initializer_address_hex"],
            f"0x{TITLE_FRONTEND_CRT_VTABLE_INITIALIZER_ADDRESS:08X}",
        )
        self.assertEqual(summary["initializer_execution_count"], 1)
        self.assertEqual(summary["observation_count"], 1)
        self.assertEqual(summary["null_vtable_count"], 0)
        self.assertEqual(memory.read_u32(object_address), vtable_address)
        self.assertEqual(audit.call_handlers(), {})

        memory.write_u32(object_address, 0)
        audit.observer(state, memory, trace, 91)
        self.assertEqual(audit.summary()["observation_count"], 2)
        self.assertEqual(audit.summary()["null_vtable_count"], 1)
        self.assertEqual(memory.read_u32(object_address), 0)

    def test_runtime_object_table_repair_restores_all_constructor_vtables(self) -> None:
        repair = TitleRuntimeObjectTableConstructorRepair()
        trace = ExecutionTrace()
        memory = SparseMemory()
        state = CpuState.with_registers()
        state.eip = TITLE_RUNTIME_OBJECT_TABLE_USE_ADDRESS

        repair.observer(state, memory, trace, 91)

        for object_address, vtable_address, records_offset, record_count in (
            TITLE_RUNTIME_OBJECT_CONSTRUCTOR_SPECS
        ):
            self.assertEqual(memory.read_u32(object_address), vtable_address)
            for index in range(record_count):
                self.assertEqual(
                    memory.read_u32(object_address + records_offset + index * 0x18),
                    1,
                )
        self.assertEqual(
            repair.summary()["repair_count"],
            len(TITLE_RUNTIME_OBJECT_CONSTRUCTOR_SPECS),
        )

    def test_runtime_callback_list_repair_closes_parsed_owner_list(self) -> None:
        repair = TitleRuntimeCallbackListRepair()
        trace = ExecutionTrace()
        object_address = 0x18000000
        sentinel = object_address + TITLE_RUNTIME_CALLBACK_LIST_OFFSET
        stack = 0x8000
        memory = SparseMemory(
            {
                TITLE_RUNTIME_CALLBACK_GLOBAL_OBJECT_ADDRESS: object_address,
                stack + 4: object_address,
                sentinel: 0x3F4CCCBC,
                sentinel + 4: 0x3F4CCCCD,
            }
        )
        state = CpuState.with_registers(esp=stack)
        state.eip = TITLE_RUNTIME_CALLBACK_DISPATCH_ADDRESSES[0]

        repair.observer(state, memory, trace, 92)

        self.assertEqual(memory.read_u32(sentinel), sentinel)
        self.assertEqual(memory.read_u32(sentinel + 4), sentinel)
        self.assertEqual(repair.summary()["repair_count"], 1)

    def test_title_spin_delay_fast_path_preserves_call_contract(self) -> None:
        base_address = 0x1000
        function = lift_x86_function(
            _call_relative_bytes(base_address, TITLE_SPIN_DELAY_ADDRESS) + b"\xC3",
            base_address=base_address,
            symbol="title_spin_delay_call",
        )
        fast_path = TitleSpinDelayFastPath()
        state = CpuState.with_registers(
            eax=0x12345678,
            ecx=0xCAFEBABE,
            esp=0x8000,
            timestamp_counter=10,
        )
        state.flags.cf = True
        memory = SparseMemory({0x8000: 0xDEADC0DE})

        result = execute_lifted_function(
            function,
            state=state,
            memory=memory,
            call_handlers=fast_path.call_handlers(),
            max_steps=4,
        )
        operations = [event["operation"] for event in result.trace.to_list()]

        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertEqual(result.state.get_register("esp"), 0x8004)
        self.assertEqual(result.state.get_register("ecx"), 0xCAFEBABE)
        self.assertEqual(result.state.get_register("eax"), 0)
        self.assertTrue(result.state.flags.cf)
        self.assertTrue(result.state.flags.zf)
        self.assertFalse(result.state.flags.sf)
        self.assertEqual(result.state.timestamp_counter, 10 + TITLE_SPIN_DELAY_ITERATIONS)
        self.assertEqual(fast_path.summary()["invocation_count"], 1)
        self.assertIn("title_spin_delay_fast_path", operations)

    def test_title_heap_fast_path_returns_unique_aligned_zero_descriptor_allocations(
        self,
    ) -> None:
        base_address = 0x1800
        code = bytearray()
        code.extend(_push_u32(0x2000))
        code.extend(
            _call_relative_bytes(
                base_address + len(code),
                TITLE_HEAP_ALLOC_TRAMPOLINE_ADDRESS,
            )
        )
        code.extend(bytes.fromhex("8BD8"))  # mov ebx, eax
        code.extend(_push_u32(0))
        code.extend(
            _call_relative_bytes(
                base_address + len(code),
                TITLE_HEAP_ALLOC_TRAMPOLINE_ADDRESS,
            )
        )
        code.extend(bytes.fromhex("83C408C3"))  # add esp, 8; ret
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="title_heap_fast_path_allocations",
        )
        fast_path = TitleGuestHeapFastPath()
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x2000: 0, 0x9000: 0xDEADC0DE})

        result = execute_lifted_function(
            function,
            state=state,
            memory=memory,
            call_handlers=fast_path.call_handlers(),
        )
        first = result.state.get_register("ebx")
        second = result.state.get_register("eax")
        operations = [event["operation"] for event in result.trace.to_list()]

        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertNotEqual(first, second)
        self.assertEqual(first % TITLE_HEAP_FAST_PATH_ALIGNMENT, 0)
        self.assertEqual(second % TITLE_HEAP_FAST_PATH_ALIGNMENT, 0)
        self.assertEqual(second - first, TITLE_HEAP_FAST_PATH_MIN_OBJECT_SIZE)
        summary = fast_path.summary()
        self.assertEqual(summary["allocation_count"], 2)
        self.assertEqual(summary["min_object_size"], TITLE_HEAP_FAST_PATH_MIN_OBJECT_SIZE)
        self.assertEqual(summary["recent_allocations"][0]["allocated_size"], 0x60)
        self.assertEqual(summary["recent_allocations"][0]["return_address"], base_address + 10)
        self.assertIn("title_heap_fast_path_allocate", operations)

        free_state = CpuState.with_registers(esp=0x7000)
        free_memory = SparseMemory({0x7008: first})
        free_trace = ExecutionTrace()
        fast_path.free_handler(
            free_state,
            free_memory,
            TITLE_HEAP_FREE_TRAMPOLINE_ADDRESS,
            free_trace,
        )

        self.assertEqual(free_state.get_register("eax"), first)
        self.assertEqual(fast_path.summary()["free_count"], 1)
        self.assertEqual(
            free_trace.to_list()[0]["operation"],
            "title_heap_fast_path_free",
        )

    def test_title_allocation_list_count_fast_path_counts_owner_matches(
        self,
    ) -> None:
        owner_address = 0x005A7218
        sentinel = TITLE_ALLOCATION_LIST_SENTINEL_ADDRESS
        first_node = 0x10001004
        second_node = 0x10001024
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory(
            {
                0x8000: 0xDEADC0DE,
                0x8004: owner_address,
                sentinel: first_node,
                first_node - 4: owner_address,
                first_node: second_node,
                second_node - 4: 0x11111111,
                second_node: sentinel,
            }
        )
        trace = ExecutionTrace()
        fast_path = TitleAllocationListCountFastPath()

        fast_path.call_handler(
            state,
            memory,
            TITLE_ALLOCATION_LIST_COUNT_ADDRESS,
            trace,
        )

        self.assertEqual(state.get_register("eax"), 1)
        summary = fast_path.summary()
        self.assertEqual(summary["invocation_count"], 1)
        self.assertEqual(summary["total_nodes_scanned"], 2)
        self.assertEqual(summary["last_summary"]["terminated_at"], "sentinel")
        self.assertEqual(
            trace.to_list()[0]["operation"],
            "title_allocation_list_count_fast_path",
        )

        cycle_state = CpuState.with_registers(esp=0x9000)
        cycle_memory = SparseMemory(
            {
                0x9000: 0xDEADC0DE,
                0x9004: owner_address,
                sentinel: first_node,
                first_node - 4: owner_address,
                first_node: second_node,
                second_node - 4: owner_address,
                second_node: first_node,
            }
        )
        fast_path.call_handler(
            cycle_state,
            cycle_memory,
            TITLE_ALLOCATION_LIST_COUNT_ADDRESS,
            ExecutionTrace(),
        )

        self.assertEqual(cycle_state.get_register("eax"), 2)
        summary = fast_path.summary()
        self.assertEqual(summary["cycle_count"], 1)
        self.assertEqual(summary["last_summary"]["terminated_at"], "cycle")

    def test_title_global_list_fast_path_seeds_empty_list_before_insert(
        self,
    ) -> None:
        object_address = 0x18000E60
        node_address = object_address + 0x08
        return_address = 0xDEADC0DE
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory(
            {
                0x8000: return_address,
                0x8004: object_address,
                object_address + 0xA0: object_address,
            }
        )
        trace = ExecutionTrace()
        fast_path = TitleGlobalListRegistrationFastPath()

        fast_path.register_handler(
            state,
            memory,
            TITLE_GLOBAL_LIST_REGISTER_ADDRESS,
            trace,
        )

        self.assertEqual(memory.read_u32(TITLE_GLOBAL_LIST_HEAD_ADDRESS), node_address)
        self.assertEqual(memory.read_u32(TITLE_GLOBAL_LIST_TAIL_ADDRESS), node_address)
        self.assertEqual(memory.read_u32(node_address), TITLE_GLOBAL_LIST_HEAD_ADDRESS)
        self.assertEqual(
            memory.read_u32(node_address + 4),
            TITLE_GLOBAL_LIST_HEAD_ADDRESS,
        )
        self.assertEqual(
            memory.read(object_address + 3, 1)[0],
            TITLE_GLOBAL_LIST_ACTIVE_FLAG | TITLE_GLOBAL_LIST_OWNER_FLAG,
        )
        self.assertEqual(state.get_register("eax"), object_address)
        summary = fast_path.summary()
        self.assertEqual(summary["invocation_count"], 1)
        self.assertEqual(summary["lazy_seed_count"], 1)
        self.assertEqual(summary["insert_count"], 1)
        self.assertEqual(
            trace.to_list()[0]["operation"],
            "title_global_list_register",
        )

    def test_title_global_list_fast_path_cleanup_resets_sentinel(self) -> None:
        node_address = 0x18000E68
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory(
            {
                0x8000: 0xDEADC0DE,
                TITLE_GLOBAL_LIST_HEAD_ADDRESS: node_address,
                TITLE_GLOBAL_LIST_TAIL_ADDRESS: node_address,
                node_address: TITLE_GLOBAL_LIST_HEAD_ADDRESS,
                node_address + 4: TITLE_GLOBAL_LIST_HEAD_ADDRESS,
            }
        )
        trace = ExecutionTrace()
        fast_path = TitleGlobalListRegistrationFastPath()

        fast_path.cleanup_handler(
            state,
            memory,
            TITLE_GLOBAL_LIST_CLEANUP_ADDRESS,
            trace,
        )

        self.assertEqual(
            memory.read_u32(TITLE_GLOBAL_LIST_HEAD_ADDRESS),
            TITLE_GLOBAL_LIST_HEAD_ADDRESS,
        )
        self.assertEqual(
            memory.read_u32(TITLE_GLOBAL_LIST_TAIL_ADDRESS),
            TITLE_GLOBAL_LIST_HEAD_ADDRESS,
        )
        self.assertEqual(state.get_register("eax"), 1)
        summary = fast_path.summary()
        self.assertEqual(summary["cleanup_count"], 1)
        self.assertEqual(summary["cleanup_node_count"], 1)
        self.assertEqual(
            trace.to_list()[0]["operation"],
            "title_global_list_cleanup",
        )

    def test_title_static_drive_array_fast_path_seeds_element_descriptors(self) -> None:
        object_address = 0x3000
        element_base = 0x5000
        descriptor_address = 0x7000
        descriptor_tag = 0x3A647664
        path_address = 0x7100
        return_address = 0xDEADC0DE
        state = CpuState.with_registers(esp=0x8000, ecx=object_address)
        memory = SparseMemory(
            {
                0x8000: return_address,
                0x8004: 3,
                0x8008: element_base,
                0x800C: path_address,
                0x8010: descriptor_address,
                descriptor_address: descriptor_tag,
                TITLE_STATIC_DRIVE_ARRAY_REGISTRY_COUNT_ADDRESS: 0,
            }
        )
        trace = ExecutionTrace()
        fast_path = TitleStaticDriveArrayFastPath()

        fast_path.setup_handler(
            state,
            memory,
            TITLE_STATIC_DRIVE_ARRAY_SETUP_ADDRESS,
            trace,
        )

        self.assertEqual(state.get_register("eax"), 1)
        self.assertEqual(state.get_register("esp"), 0x8010)
        self.assertEqual(memory.read_u32(0x8010), return_address)
        self.assertEqual(memory.read_u32(object_address + 0x04), 1)
        self.assertEqual(memory.read_u32(object_address + 0x08), 0)
        self.assertEqual(memory.read_u32(object_address + 0x0C), 3)
        self.assertEqual(memory.read_u32(object_address + 0x10), element_base)
        self.assertEqual(
            memory.read_u32(TITLE_STATIC_DRIVE_ARRAY_REGISTRY_OBJECTS_ADDRESS),
            object_address,
        )
        self.assertEqual(
            memory.read_u32(TITLE_STATIC_DRIVE_ARRAY_REGISTRY_DESCRIPTORS_ADDRESS),
            descriptor_tag,
        )
        self.assertEqual(
            memory.read_u32(TITLE_STATIC_DRIVE_ARRAY_REGISTRY_COUNT_ADDRESS),
            1,
        )
        for index in range(3):
            element_address = element_base + index * TITLE_STATIC_DRIVE_ARRAY_ELEMENT_SIZE
            self.assertEqual(memory.read_u32(element_address), descriptor_address)
            self.assertEqual(memory.read_u32(element_address + 0x20), 0)
        summary = fast_path.summary()
        self.assertEqual(summary["invocation_count"], 1)
        self.assertEqual(summary["seeded_element_count"], 3)
        self.assertEqual(
            trace.to_list()[0]["operation"],
            "title_static_drive_array_fast_path",
        )

    def test_seed_guest_thread_fs_block_points_thread_local_self(self) -> None:
        memory = SparseMemory()
        fs_base = 0x72010000

        _seed_guest_thread_fs_block(memory, fs_base)

        self.assertEqual(memory.read_u32(fs_base + 0x20), fs_base)
        self.assertEqual(memory.read_u32(fs_base + 0x250), 0)

    def test_gpu_idle_pump_step_limit_classifies_observed_loop(self) -> None:
        trace_events = [
            {
                "operation": "memory_read",
                "address": 0x0021D83A,
                "address_hex": "0x0021D83A",
                "details": {
                    "memory_address": TITLE_GPU_PFIFO_INTERRUPT_STATUS_ADDRESS,
                    "memory_address_hex": "0xFD002100",
                    "value": 0,
                    "value_hex": "0x00000000",
                },
            },
            {
                "operation": "memory_read",
                "address": 0x0021DDA2,
                "address_hex": "0x0021DDA2",
                "details": {
                    "memory_address": TITLE_GPU_INTERRUPT_STATUS_ADDRESS,
                    "memory_address_hex": "0xFD400100",
                    "value": 0,
                    "value_hex": "0x00000000",
                },
            },
            {
                "operation": "branch",
                "address": TITLE_GPU_IDLE_PUMP_LOOP_BRANCH,
                "address_hex": "0x0021DDBC",
                "details": {
                    "condition": "e",
                    "taken": True,
                    "target": TITLE_GPU_IDLE_PUMP_LOOP_ENTRY,
                    "target_hex": "0x0021DD80",
                },
            },
        ]
        state = CpuState.with_registers(
            esi=0xFD000000,
            edi=0x00226EB8,
            ecx=0x00226EB8,
        )

        boundary = _gpu_idle_pump_boundary_from_step_limit(
            trace_events, state, 327680
        )

        self.assertIsNotNone(boundary)
        assert boundary is not None
        self.assertEqual(boundary["status"], "hardware_boundary")
        self.assertEqual(boundary["boundary_kind"], "title_gpu_idle_pump")
        self.assertEqual(boundary["loop_entry"], TITLE_GPU_IDLE_PUMP_LOOP_ENTRY)
        self.assertEqual(
            boundary["gpu_idle_pump"]["latest_status_reads"][0]["value_hex"],
            "0x00000000",
        )

    def test_frontend_resource_boundary_classifies_null_dictionary_list(self) -> None:
        state = CpuState.with_registers(
            ebx=0x00000008,
            esi=0x00000000,
            edi=0xFFFFFFF8,
            ebp=TITLE_FRONTEND_GLOBAL_DIC_IMPACT2_KEY_ADDRESS,
            edx=0x00000008,
        )
        trace_events = [
            {
                "sequence": 1,
                "address": 0x000B904C,
                "address_hex": "0x000B904C",
                "operation": "memory_write",
                "details": {
                    "memory_address": TITLE_FRONTEND_GLOBAL_RESOURCE_LIST_ADDRESS,
                    "value": 0,
                    "size": 4,
                },
            },
            {
                "sequence": 2,
                "address": 0x00028B11,
                "address_hex": "0x00028B11",
                "operation": "memory_read",
                "details": {
                    "memory_address": TITLE_FRONTEND_GLOBAL_RESOURCE_LIST_ADDRESS,
                    "value": 0,
                },
            },
            {
                "sequence": 3,
                "address": 0x000EB4FA,
                "address_hex": "0x000EB4FA",
                "operation": "memory_read",
                "details": {
                    "memory_address": 0,
                    "value": 0,
                },
            },
            {
                "sequence": 4,
                "address": TITLE_FRONTEND_RESOURCE_LIST_FIND_LOOP_BRANCH,
                "address_hex": "0x000EB4FE",
                "operation": "branch",
                "details": {
                    "condition": "ne",
                    "taken": True,
                    "target": TITLE_FRONTEND_RESOURCE_LIST_FIND_LOOP_ENTRY,
                    "target_hex": "0x000EB4E5",
                },
            },
        ]

        boundary = _frontend_resource_boundary_from_step_limit(
            trace_events,
            state,
            327680,
        )

        self.assertIsNotNone(boundary)
        assert boundary is not None
        self.assertEqual(boundary["status"], "asset_boundary")
        self.assertEqual(
            boundary["boundary_kind"],
            "frontend_global_dictionary_resource_list",
        )
        self.assertEqual(boundary["loop_entry_hex"], "0x000EB4E5")
        scan = boundary["frontend_resource_scan"]
        self.assertEqual(scan["resource_path"], "Frontend/global.dic")
        self.assertEqual(scan["lookup_key_hint"], "impact2")
        self.assertEqual(scan["list_argument_hex"], "0x00000000")
        self.assertEqual(scan["sentinel_hex"], "0x00000008")
        self.assertTrue(scan["current_link_is_null"])
        self.assertTrue(scan["candidate_base_underflow"])
        self.assertEqual(scan["diagnosis"], "frontend_resource_list_pointer_is_null")
        self.assertEqual(scan["global_write_count"], 1)
        self.assertEqual(
            scan["latest_global_writes"][0]["memory_address_hex"],
            "0x00443EE8",
        )

    def test_runtime_abi_bridge_invokes_registered_kernel_import_target(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[127])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        function = lift_x86_function(
            _call_indirect_bytes(0x3000),
            base_address=0x1000,
            symbol="call_frequency",
        )
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory({0x3000: target, 0x8000: 0xDEADC0DE})

        result = execute_lifted_function(
            function,
            state=state,
            memory=memory,
            call_handlers=bridge.call_handlers(),
        )
        trace_operations = [event["operation"] for event in result.trace.to_list()]

        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertEqual(result.state.get_register("eax"), 10_000_000)
        self.assertEqual(len(bridge.invocations), 1)
        self.assertEqual(bridge.invocations[0].shim_name, "KeQueryPerformanceFrequency")
        self.assertIn("runtime_abi_call", trace_operations)

    def test_runtime_abi_bridge_caches_immutable_call_metadata(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[127, 156])
        bridge = RuntimeAbiBridge(runtime)
        frequency_shim = next(
            shim
            for shim in runtime.registered_shims
            if shim.name == "KeQueryPerformanceFrequency"
        )
        tick_shim = next(
            shim for shim in runtime.registered_shims if shim.name == "KeTickCount"
        )
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory({0x8000: 0xDEADC0DE})

        with patch(
            "tools.playability.playability_probe.inspect.signature",
            side_effect=AssertionError("handler signature was recomputed"),
        ):
            bridge.invoke(
                state,
                memory,
                frequency_shim.target_address,
                ExecutionTrace(),
            )
            bridge.invoke(
                state,
                memory,
                frequency_shim.target_address,
                ExecutionTrace(),
            )

        self.assertEqual(bridge.invocation_count, 2)
        self.assertEqual(
            [shim.name for _, shim in bridge._volatile_data_exports],
            ["KeTickCount"],
        )
        self.assertEqual(memory.read_u32(tick_shim.target_address), 0)

    def test_runtime_abi_bridge_cleans_both_shutdown_notification_arguments(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[47])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        return_address = 0x00237887
        registration_address = 0x1088411C
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory(
            {
                0x8000: return_address,
                0x8004: registration_address,
                0x8008: 1,
            }
        )

        bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "HalRegisterShutdownNotification")
        self.assertEqual(invocation.arguments, (registration_address, 1))
        self.assertEqual(invocation.handler_arguments, (registration_address, 1))
        self.assertEqual(invocation.stack_cleanup_bytes, 8)
        self.assertEqual(state.get_register("esp"), 0x8008)
        self.assertEqual(memory.read_u32(0x8008), return_address)

    def test_runtime_abi_bridge_cleans_all_interrupt_initializer_arguments(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[109])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        return_address = 0x00237836
        arguments = (
            0x0024AC50,
            0x002376CA,
            0x10884110,
            0x00000026,
            0x00000006,
            0x00000000,
            0x00000001,
        )
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory(
            {
                0x8000: return_address,
                **{
                    0x8004 + index * 4: argument
                    for index, argument in enumerate(arguments)
                },
            }
        )

        bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "KeInitializeInterrupt")
        self.assertEqual(invocation.arguments, arguments)
        self.assertEqual(invocation.handler_arguments, arguments)
        self.assertEqual(invocation.stack_cleanup_bytes, 28)
        self.assertEqual(state.get_register("esp"), 0x801C)
        self.assertEqual(memory.read_u32(0x801C), return_address)

    def test_runtime_abi_bridge_marshals_ke_query_system_time_output(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[128])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        output_address = 0x6000
        return_address = 0xDEADC0DE
        state = CpuState.with_registers(eax=0xA5A5A5A5, esp=0x8000)
        memory = SparseMemory(
            {
                0x8000: return_address,
                0x8004: output_address,
                output_address: b"\xCC" * 8,
            }
        )
        expected_system_time = runtime.ke_query_system_time()

        bridge.invoke(state, memory, target, ExecutionTrace())

        actual_system_time = int.from_bytes(memory.read(output_address, 8), "little")
        invocation = bridge.invocations[0]
        self.assertEqual(actual_system_time, expected_system_time)
        self.assertEqual(state.get_register("eax"), 0xA5A5A5A5)
        self.assertEqual(state.get_register("esp"), 0x8004)
        self.assertEqual(memory.read_u32(0x8004), return_address)
        self.assertEqual(invocation.arguments, (output_address,))
        self.assertEqual(invocation.handler_arguments, ())
        self.assertEqual(invocation.return_kind, "dict")
        self.assertEqual(invocation.stack_cleanup_bytes, 4)
        self.assertEqual(
            [write["label"] for write in invocation.memory_writes],
            ["system_time"],
        )

    def test_ke_query_system_time_preserves_crt_constructor_iterator(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[128])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        output_address = 0x6000
        saved_crt_iterator = 0x002CD674
        call_stack_pointer = 0x7FF4
        call_return_address = 0x00126744
        state = CpuState.with_registers(
            esi=saved_crt_iterator,
            esp=call_stack_pointer,
        )
        memory = SparseMemory(
            {
                call_stack_pointer: call_return_address,
                call_stack_pointer + 4: output_address,
                call_stack_pointer + 8: saved_crt_iterator,
                output_address: b"\x00" * 8,
            }
        )
        expected_system_time = runtime.ke_query_system_time()

        bridge.invoke(state, memory, target, ExecutionTrace())
        returned_to = memory.read_u32(state.get_register("esp"))
        state.set_register("esp", state.get_register("esp") + 4)
        restored_crt_iterator = memory.read_u32(state.get_register("esp"))

        self.assertEqual(returned_to, call_return_address)
        self.assertEqual(restored_crt_iterator, saved_crt_iterator)
        self.assertEqual(state.get_register("esi"), saved_crt_iterator)
        self.assertEqual(
            int.from_bytes(memory.read(output_address, 8), "little"),
            expected_system_time,
        )
        self.assertEqual(bridge.invocations[0].stack_cleanup_bytes, 4)

    def test_runtime_abi_bridge_materializes_volatile_tick_count_data(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[156])
        bridge = RuntimeAbiBridge(runtime)
        tick_shim = runtime.registered_shims[0]
        thunk_address = 0x3000
        function = lift_x86_function(
            b"\xA1"
            + struct.pack("<I", thunk_address)
            + b"\x8B\x00\xC3",
            base_address=0x1000,
            symbol="read_ke_tick_count_data",
        )
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory(
            {thunk_address: tick_shim.target_address, 0x8000: 0xDEADC0DE}
        )
        runtime.clock.advance_100ns(1_234_567)

        bridge.synchronize_data_exports(memory)
        result = execute_lifted_function(function, state=state, memory=memory)

        self.assertEqual(tick_shim.behavior, "data")
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertEqual(result.state.get_register("eax"), 12)
        self.assertEqual(memory.read_u32(tick_shim.target_address), 12)

    def test_runtime_abi_bridge_cleans_observed_tv_encoder_guest_arguments(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[2])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        function = lift_x86_function(
            _push_u32(0x00225208)
            + _push_u32(0)
            + _push_u32(6)
            + _push_u32(0)
            + _call_indirect_bytes(0x3000),
            base_address=0x1000,
            symbol="send_tv_encoder_option_callsite",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x3000: target, 0x9000: 0xDEADC0DE})

        result = execute_lifted_function(
            function,
            state=state,
            memory=memory,
            call_handlers=bridge.call_handlers(),
        )

        invocation = bridge.invocations[0]
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertEqual(result.state.get_register("esp"), 0x9004)
        self.assertEqual(invocation.shim_name, "AvSendTVEncoderOption")
        self.assertEqual(invocation.arguments, (0, 6, 0, 0x00225208))
        self.assertEqual(invocation.handler_arguments, (0, 6))
        self.assertEqual(invocation.stack_cleanup_bytes, 16)

    def test_runtime_abi_bridge_marshals_pci_space_read_buffer(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[46])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory(
            {
                0x7000: b"\xFF\xFF\xFF\xFF",
                0x8000: 0xDEADC0DE,
            }
        )
        arguments = (3, 0, 0x4C, 0x7000, 4, 0)
        for index, argument in enumerate(arguments):
            memory.write_u32(0x8004 + index * 4, argument)

        bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "HalReadWritePCISpace")
        self.assertEqual(invocation.arguments, arguments)
        self.assertEqual(invocation.handler_arguments, arguments)
        self.assertEqual(invocation.stack_cleanup_bytes, 24)
        self.assertEqual(invocation.eax, XboxStatus.SUCCESS)
        self.assertEqual(memory.read(0x7000, 4), b"\x00\x00\x00\x00")
        self.assertEqual(memory.read_u32(0x8000 + 24), 0xDEADC0DE)
        self.assertEqual(
            [write["label"] for write in invocation.memory_writes],
            ["pci_read_buffer"],
        )

    def test_runtime_abi_bridge_marshals_pci_space_write_payload(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[46])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory(
            {
                0x7000: b"\x12\x34\x56\x78",
                0x8000: 0xDEADC0DE,
            }
        )
        arguments = (3, 0, 0x4C, 0x7000, 4, 1)
        for index, argument in enumerate(arguments):
            memory.write_u32(0x8004 + index * 4, argument)

        bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "HalReadWritePCISpace")
        self.assertEqual(invocation.stack_cleanup_bytes, 24)
        self.assertEqual(invocation.result["bytes_read"], 4)
        self.assertTrue(invocation.result["data_elided"])
        self.assertEqual(memory.read(0x7000, 4), b"\x12\x34\x56\x78")
        self.assertEqual(invocation.memory_writes, ())

    def test_runtime_abi_bridge_maps_ps_create_system_thread_ex_arguments(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[255])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        handle_pointer = 0x6000
        thread_id_pointer = 0x6004
        start_context1 = 0x11112222
        start_context2 = 0x33334444
        start_routine = 0x000E682C
        arguments = (
            handle_pointer,
            0x20,
            0x3000,
            0x40,
            thread_id_pointer,
            start_context1,
            start_context2,
            0,
            1,
            start_routine,
        )
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory({0x8000: 0xDEADC0DE})
        for index, argument in enumerate(arguments):
            memory.write_u32(0x8004 + index * 4, argument)

        bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "PsCreateSystemThreadEx")
        self.assertEqual(invocation.arguments, arguments)
        self.assertEqual(invocation.handler_arguments, arguments)
        self.assertEqual(invocation.return_kind, "status_dict")
        self.assertEqual(state.get_register("eax"), 0)
        self.assertEqual(state.get_register("esp"), 0x8000 + 40)
        self.assertEqual(memory.read_u32(0x8000 + 40), 0xDEADC0DE)
        self.assertEqual(memory.read_u32(handle_pointer), 0x104)
        self.assertEqual(memory.read_u32(thread_id_pointer), 0x104)
        self.assertEqual(
            [write["label"] for write in invocation.memory_writes],
            ["thread_handle", "thread_id"],
        )

    def test_runtime_abi_bridge_writes_created_semaphore_handle(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[193])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        handle_pointer = 0x6000
        arguments = (handle_pointer, 0, 1, 4)
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory({0x8000: 0xDEADC0DE})
        for index, argument in enumerate(arguments):
            memory.write_u32(0x8004 + index * 4, argument)

        bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "NtCreateSemaphore")
        self.assertEqual(invocation.guest_stack_pointer, 0x8000)
        self.assertEqual(invocation.guest_return_address, 0xDEADC0DE)
        self.assertEqual(invocation.arguments, arguments)
        self.assertEqual(invocation.handler_arguments, arguments)
        self.assertEqual(invocation.return_kind, "status_dict")
        self.assertEqual(invocation.eax, XboxStatus.SUCCESS)
        self.assertEqual(invocation.stack_cleanup_bytes, 16)
        self.assertEqual(memory.read_u32(0x8010), 0xDEADC0DE)
        self.assertEqual(memory.read_u32(handle_pointer), invocation.result["handle"])
        self.assertEqual(
            [write["label"] for write in invocation.memory_writes],
            ["semaphore_handle"],
        )

    def test_runtime_abi_bridge_cleans_release_semaphore_stack_and_writes_previous_count(
        self,
    ) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        semaphore = runtime.nt_create_semaphore(initial_count=1, limit=2)
        runtime.register_kernel_imports(resolver, imported_ordinals=[222])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        previous_count_pointer = 0x6000
        arguments = (semaphore, 1, previous_count_pointer)
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory({0x8000: 0xDEADC0DE})
        for index, argument in enumerate(arguments):
            memory.write_u32(0x8004 + index * 4, argument)

        bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "NtReleaseSemaphore")
        self.assertEqual(invocation.arguments, arguments)
        self.assertEqual(invocation.handler_arguments, arguments[:2])
        self.assertEqual(invocation.return_kind, "status_dict")
        self.assertEqual(invocation.eax, XboxStatus.SUCCESS)
        self.assertEqual(invocation.stack_cleanup_bytes, 12)
        self.assertEqual(state.get_register("esp"), 0x8000 + 12)
        self.assertEqual(memory.read_u32(0x8000 + 12), 0xDEADC0DE)
        self.assertEqual(memory.read_u32(previous_count_pointer), 1)
        self.assertEqual(
            [write["label"] for write in invocation.memory_writes],
            ["semaphore_previous_count"],
        )

    def test_runtime_abi_bridge_ranks_failed_guest_callers(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[222])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        return_address = 0x000E307F
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory({0x8000: return_address})
        for index, argument in enumerate((0, 1, 0)):
            memory.write_u32(0x8004 + index * 4, argument)

        bridge.invoke(state, memory, target, ExecutionTrace())

        failed = bridge.summary()["failed_caller_counts"]
        self.assertEqual(failed[0]["shim_name"], "NtReleaseSemaphore")
        self.assertEqual(failed[0]["guest_return_address"], return_address)
        self.assertEqual(failed[0]["invocation_count"], 1)

    def test_runtime_abi_bridge_cleans_nonvolatile_setting_query(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[24])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        arguments = (0x01, 0x6000, 0x6004, 4, 0x6008)
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory({0x8000: 0xDEADC0DE})
        for index, argument in enumerate(arguments):
            memory.write_u32(0x8004 + index * 4, argument)

        bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "ExQueryNonVolatileSetting")
        self.assertEqual(invocation.handler_arguments, arguments)
        self.assertEqual(invocation.stack_cleanup_bytes, 20)
        self.assertEqual(state.get_register("eax"), 0)
        self.assertEqual(memory.read_u32(0x8000 + 20), 0xDEADC0DE)
        self.assertEqual(memory.read_u32(0x6000), 4)
        self.assertEqual(memory.read_u32(0x6004), 0)
        self.assertEqual(memory.read_u32(0x6008), 4)

    def test_runtime_abi_bridge_cleans_kernel_timer_pointer_call(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.ke_initialize_timer_ex(0x5000, 0)
        runtime.register_kernel_imports(resolver, imported_ordinals=[149])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        arguments = (0x5000, 0xB5659000, 0xFFFFFFCD, 0x5010)
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory({0x8000: 0xDEADC0DE})
        for index, argument in enumerate(arguments):
            memory.write_u32(0x8004 + index * 4, argument)

        bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "KeSetTimer")
        self.assertEqual(invocation.handler_arguments, arguments)
        self.assertEqual(invocation.stack_cleanup_bytes, 16)
        self.assertEqual(invocation.return_kind, "bool")
        self.assertEqual(state.get_register("eax"), 0)
        self.assertEqual(memory.read_u32(0x8000 + 16), 0xDEADC0DE)

    def test_runtime_abi_bridge_cleans_contiguous_memory_ex_call(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[166])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        arguments = (0x40000, 0, 0x03FFFFFF, 0, 0x404)
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory({0x8000: 0xDEADC0DE})
        for index, argument in enumerate(arguments):
            memory.write_u32(0x8004 + index * 4, argument)

        bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "MmAllocateContiguousMemoryEx")
        self.assertEqual(invocation.arguments, arguments)
        self.assertEqual(invocation.handler_arguments, arguments)
        self.assertEqual(invocation.stack_cleanup_bytes, 20)
        self.assertEqual(invocation.return_kind, "int")
        self.assertEqual(state.get_register("eax"), 0x20000000)
        self.assertEqual(memory.read_u32(0x8000 + 20), 0xDEADC0DE)

    def test_runtime_abi_bridge_returns_null_for_zero_contiguous_memory_ex(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[166])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        arguments = (0, 0, 0x07FFFFFF, 0x4000, 0x404)
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory({0x8000: 0xDEADC0DE})
        for index, argument in enumerate(arguments):
            memory.write_u32(0x8004 + index * 4, argument)

        bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "MmAllocateContiguousMemoryEx")
        self.assertEqual(invocation.arguments, arguments)
        self.assertEqual(invocation.return_kind, "int")
        self.assertEqual(state.get_register("eax"), 0)
        self.assertEqual(memory.read_u32(0x8000 + 20), 0xDEADC0DE)

    def test_runtime_abi_bridge_maps_virtual_memory_out_parameters(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[184])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        base_pointer = 0x6000
        size_pointer = 0x6004
        arguments = (base_pointer, 0, size_pointer, 0x2000, 0x04)
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory({0x8000: 0xDEADC0DE})
        memory.write_u32(base_pointer, 0)
        memory.write_u32(size_pointer, 0x3000)
        for index, argument in enumerate(arguments):
            memory.write_u32(0x8004 + index * 4, argument)

        bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "NtAllocateVirtualMemory")
        self.assertEqual(invocation.arguments, arguments)
        self.assertEqual(invocation.handler_arguments, arguments)
        self.assertEqual(invocation.stack_cleanup_bytes, 20)
        self.assertEqual(invocation.return_kind, "status_dict")
        self.assertEqual(state.get_register("eax"), XboxStatus.SUCCESS)
        self.assertEqual(state.get_register("esp"), 0x8000 + 20)
        self.assertEqual(memory.read_u32(0x8000 + 20), 0xDEADC0DE)
        self.assertEqual(memory.read_u32(base_pointer), 0x10000000)
        self.assertEqual(memory.read_u32(size_pointer), 0x3000)
        self.assertEqual(
            [write["label"] for write in invocation.memory_writes],
            ["base_address", "region_size"],
        )

    def test_runtime_abi_bridge_preserves_virtual_memory_commit_base(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[184])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        reserve_base_pointer = 0x6000
        reserve_size_pointer = 0x6004
        commit_base_pointer = 0x6010
        commit_size_pointer = 0x6014
        memory = SparseMemory()
        memory.write_u32(reserve_base_pointer, 0)
        memory.write_u32(reserve_size_pointer, 0x4000)
        reserve_arguments = (
            reserve_base_pointer,
            0,
            reserve_size_pointer,
            0x2000,
            0x04,
        )
        reserve_state = CpuState.with_registers(esp=0x8000)
        memory.write_u32(0x8000, 0xDEADC0DE)
        for index, argument in enumerate(reserve_arguments):
            memory.write_u32(0x8004 + index * 4, argument)

        bridge.invoke(reserve_state, memory, target, ExecutionTrace())

        reserved_base = memory.read_u32(reserve_base_pointer)
        requested_commit_base = reserved_base + 0x1000
        memory.write_u32(commit_base_pointer, requested_commit_base)
        memory.write_u32(commit_size_pointer, 0x1000)
        commit_arguments = (
            commit_base_pointer,
            0,
            commit_size_pointer,
            0x1000,
            0x04,
        )
        commit_state = CpuState.with_registers(esp=0x9000)
        memory.write_u32(0x9000, 0xFEEDC0DE)
        for index, argument in enumerate(commit_arguments):
            memory.write_u32(0x9004 + index * 4, argument)

        bridge.invoke(commit_state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[-1]
        self.assertEqual(invocation.shim_name, "NtAllocateVirtualMemory")
        self.assertEqual(invocation.arguments, commit_arguments)
        self.assertEqual(invocation.result["allocation_policy"], "commit_existing_reservation")
        self.assertEqual(invocation.result["allocated_address"], requested_commit_base)
        self.assertEqual(memory.read_u32(commit_base_pointer), requested_commit_base)
        self.assertEqual(memory.read_u32(commit_size_pointer), 0x1000)
        self.assertEqual(len(runtime.memory.allocations), 1)
        self.assertEqual(commit_state.get_register("eax"), XboxStatus.SUCCESS)
        self.assertEqual(commit_state.get_register("esp"), 0x9000 + 20)
        self.assertEqual(memory.read_u32(0x9000 + 20), 0xFEEDC0DE)

    def test_runtime_abi_bridge_reads_compare_memory_ulong_payload(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[269])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        arguments = (0x6000, 12, 0xA5A5A5A5)
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory({0x8000: 0xDEADC0DE})
        memory.write(0x6000, b"\xA5" * 8 + b"\x00" * 4)
        for index, argument in enumerate(arguments):
            memory.write_u32(0x8004 + index * 4, argument)

        bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "RtlCompareMemoryUlong")
        self.assertEqual(invocation.arguments, arguments)
        self.assertEqual(invocation.handler_arguments, arguments)
        self.assertEqual(invocation.stack_cleanup_bytes, 12)
        self.assertEqual(invocation.return_kind, "int")
        self.assertEqual(state.get_register("eax"), 8)
        self.assertEqual(memory.read_u32(0x8000 + 12), 0xDEADC0DE)

    def test_runtime_abi_bridge_preserves_stack_for_kf_lower_irql(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[161])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory({0x8000: 0xDEADC0DE})

        bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "KfLowerIrql")
        self.assertEqual(invocation.arguments, ())
        self.assertEqual(invocation.handler_arguments, ())
        self.assertEqual(invocation.stack_cleanup_bytes, 0)
        self.assertEqual(state.get_register("eax"), 0)
        self.assertEqual(state.get_register("esp"), 0x8000)
        self.assertEqual(memory.read_u32(0x8000), 0xDEADC0DE)

    def test_runtime_abi_bridge_cleans_ob_reference_object_by_handle_stack(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        thread_handle = runtime.ps_create_system_thread(start_address=0x1234)
        runtime.register_kernel_imports(resolver, imported_ordinals=[246])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        arguments = (thread_handle, 0xE0000660, 0x7000)
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory({0x8000: 0xDEADC0DE})
        for index, argument in enumerate(arguments):
            memory.write_u32(0x8004 + index * 4, argument)

        bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "ObReferenceObjectByHandle")
        self.assertEqual(invocation.arguments, arguments)
        self.assertEqual(invocation.handler_arguments, (thread_handle,))
        self.assertEqual(invocation.stack_cleanup_bytes, 12)
        self.assertEqual(invocation.return_kind, "status_dict")
        self.assertEqual(state.get_register("eax"), XboxStatus.SUCCESS)
        self.assertEqual(state.get_register("esp"), 0x8000 + 12)
        self.assertEqual(memory.read_u32(0x8000 + 12), 0xDEADC0DE)

    def test_runtime_abi_bridge_reads_obf_dereference_object_from_ecx(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[250])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        state = CpuState.with_registers(esp=0x8000, ecx=0x12345678)
        memory = SparseMemory({0x8000: 0xDEADC0DE})

        bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "ObfDereferenceObject")
        self.assertEqual(invocation.arguments, ())
        self.assertEqual(invocation.handler_arguments, (0x12345678,))
        self.assertEqual(invocation.stack_cleanup_bytes, 0)
        self.assertEqual(invocation.return_kind, "int")
        self.assertEqual(state.get_register("eax"), XboxStatus.SUCCESS)
        self.assertEqual(state.get_register("esp"), 0x8000)
        self.assertEqual(memory.read_u32(0x8000), 0xDEADC0DE)

    def test_scheduler_boundary_classifies_step_limit_loop(self) -> None:
        state = CpuState.with_registers(eax=1, esi=8)
        trace_events = [
            {
                "sequence": 0,
                "address": 0xF29F0,
                "address_hex": "0x000F29F0",
                "operation": "memory_write",
                "details": {
                    "memory_address": 0x71000024,
                    "value": 0x40D,
                    "size": 4,
                },
            },
            {
                "address": 0xF2A10,
                "operation": "memory_read",
                "details": {
                    "memory_address": 0x71000024,
                    "value": 0x40D,
                },
            },
            {
                "address": 0xF2A14,
                "operation": "memory_read",
                "details": {
                    "memory_address": 0x00000010,
                    "value": 0x410,
                },
            },
            {
                "address": 0xF2A1D,
                "operation": "memory_read",
                "details": {
                    "memory_address": 0x00000038,
                    "value": 0x00000008,
                },
            },
            {
                "address": 0xF2A22,
                "operation": "branch",
                "details": {
                    "taken": True,
                    "target": 0xF2A10,
                    "target_hex": "0x000F2A10",
                },
            }
        ]

        boundary = _scheduler_boundary_from_step_limit(
            trace_events,
            state,
            65536,
        )

        self.assertIsNotNone(boundary)
        assert boundary is not None
        self.assertEqual(boundary["status"], "scheduler_boundary")
        self.assertEqual(boundary["owner"], "guest_thread_scheduler")
        self.assertEqual(boundary["loop_entry_hex"], "0x000F2A10")
        self.assertEqual(boundary["steps"], 65536)
        self.assertEqual(
            boundary["scheduler_queue_scan"]["producer_consumer_hint"],
            "awaiting_work_item_matching_scan_key",
        )
        self.assertEqual(boundary["scheduler_queue_scan"]["scan_key_hex"], "0x0000040D")
        self.assertEqual(
            boundary["scheduler_queue_scan"]["scan_key_source_address_hex"],
            "0x71000024",
        )
        self.assertEqual(boundary["scheduler_queue_scan"]["next_node_hex"], "0x00000008")
        self.assertEqual(
            boundary["scheduler_queue_scan"]["current_node_base_hex"],
            "0x00000008",
        )
        self.assertTrue(
            boundary["scheduler_queue_scan"]["next_pointer_matches_current_node"]
        )
        self.assertFalse(
            boundary["scheduler_queue_scan"]["scan_key_matched_current_node"]
        )
        awaited = boundary["scheduler_queue_scan"]["awaited_work_item"]
        self.assertEqual(awaited["key_hex"], "0x0000040D")
        self.assertEqual(awaited["status"], "not_present_in_observed_scan")
        self.assertEqual(awaited["observed_node_self_loop"], True)
        self.assertEqual(awaited["loop_esi_hex"], "0x00000008")
        self.assertEqual(
            [read["label"] for read in boundary["scheduler_queue_scan"]["reads"]],
            ["scan_key", "current_node_key", "next_node"],
        )
        self.assertEqual(boundary["scheduler_queue_scan"]["producer_candidate_write_count"], 1)
        self.assertEqual(boundary["scheduler_queue_scan"]["observed_node_key_write_values"], [])
        self.assertEqual(
            boundary["scheduler_queue_scan"]["producer_candidate_writes"][0]["reasons"],
            ["watched_scheduler_address", "wrote_scan_key_value"],
        )
        producer_trace = boundary["scheduler_queue_scan"]["producer_trace"]
        self.assertEqual(
            producer_trace["status"],
            "awaited_key_seen_in_candidate_writes",
        )
        self.assertEqual(producer_trace["scan_key_value_write_count"], 1)
        self.assertEqual(producer_trace["candidate_instruction_group_count"], 1)
        self.assertEqual(
            producer_trace["next_recovery_anchor"]["instruction_address_hex"],
            "0x000F29F0",
        )
        self.assertEqual(
            producer_trace["next_recovery_anchor"]["roles"],
            ["scan_key_source_writer", "awaited_key_value_writer"],
        )
        self.assertEqual(
            producer_trace["producer_semantic_counts"],
            {"unknown_scheduler_writer": 1},
        )

    def test_scheduler_loop_convergence_detector_stops_before_step_limit(self) -> None:
        code = bytes.fromhex(
            "8B442424"  # mov eax, [esp + 0x24]
            "8B51EC"    # mov edx, [ecx - 0x14]
            "3BD7"      # cmp edx, edi
            "8D41EC"    # lea eax, [ecx - 0x14]
            "9090"      # observed helper branch slot is not needed here
            "8B09"      # mov ecx, [ecx]
            "3BCB"      # cmp ecx, ebx
            "75EC"      # jne 0x0010C100
        )
        function = lift_x86_function(
            code,
            base_address=TITLE_STARTUP_WORK_QUEUE_LOOP_ENTRY,
            symbol="scheduler_convergence_loop",
        )
        state = CpuState.with_registers(
            esp=0x8000,
            ecx=0x9014,
            ebx=0x9999,
            edi=0x10001730,
        )
        memory = SparseMemory(
            {
                0x8024: 0x10001730,
                0x9000: 0,
                0x9014: 0x9014,
            }
        )
        detector = SchedulerLoopConvergenceDetector(repetitions=4)

        with self.assertRaises(X86ExecutionError) as caught:
            execute_lifted_function(
                function,
                state=state,
                memory=memory,
                step_observer=detector.observer,
                max_steps=1000,
            )

        self.assertEqual(str(caught.exception), SCHEDULER_LOOP_CONVERGENCE_ERROR)
        self.assertLess(caught.exception.steps, 1000)
        trace_events = caught.exception.trace.to_list()
        self.assertEqual(trace_events[-1]["operation"], "scheduler_loop_converged")
        boundary = _scheduler_boundary_from_step_limit(
            trace_events,
            caught.exception.state,
            caught.exception.steps,
        )
        self.assertIsNotNone(boundary)
        assert boundary is not None
        self.assertEqual(boundary["status"], "scheduler_boundary")
        self.assertEqual(boundary["early_convergence"]["repeat_count"], 4)
        self.assertEqual(
            detector.summary()["last_detection"]["scan_key_hex"],
            "0x10001730",
        )

    def test_scheduler_boundary_names_next_anchor_when_awaited_key_is_absent(self) -> None:
        state = CpuState.with_registers(edx=0x40D, esi=8, edi=0x3445C0)
        trace_events = [
            {
                "sequence": 10,
                "address": 0xF2A63,
                "address_hex": "0x000F2A63",
                "operation": "memory_write",
                "details": {
                    "memory_address": 0x00000010,
                    "value": 0x401,
                    "size": 4,
                },
            },
            {
                "sequence": 11,
                "address": 0xF2AF9,
                "address_hex": "0x000F2AF9",
                "operation": "memory_write",
                "details": {
                    "memory_address": 0x00000038,
                    "value": 0x00000008,
                    "size": 4,
                },
            },
            {
                "sequence": 12,
                "address": 0xF29F6,
                "address_hex": "0x000F29F6",
                "operation": "memory_write",
                "details": {
                    "memory_address": 0x00000010,
                    "value": 0x003445C0,
                    "size": 4,
                },
            },
            {
                "address": 0xF2A10,
                "operation": "memory_read",
                "details": {
                    "memory_address": 0x70FFFECC,
                    "value": 0x40D,
                },
            },
            {
                "address": 0xF2A14,
                "operation": "memory_read",
                "details": {
                    "memory_address": 0x00000010,
                    "value": 0x003445C0,
                },
            },
            {
                "address": 0xF2A1D,
                "operation": "memory_read",
                "details": {
                    "memory_address": 0x00000038,
                    "value": 0x00000008,
                },
            },
            {
                "address": 0xF2A22,
                "operation": "branch",
                "details": {
                    "taken": True,
                    "target": 0xF2A10,
                    "target_hex": "0x000F2A10",
                },
            },
        ]

        boundary = _scheduler_boundary_from_step_limit(trace_events, state, 70000)

        self.assertIsNotNone(boundary)
        assert boundary is not None
        scan = boundary["scheduler_queue_scan"]
        self.assertEqual(scan["awaited_work_item"]["status"], "not_present_in_observed_scan")
        self.assertEqual(scan["observed_node_key_write_values"], ["0x00000401", "0x003445C0"])
        self.assertEqual(scan["observed_next_node_write_values"], ["0x00000008"])
        producer_trace = scan["producer_trace"]
        self.assertEqual(
            producer_trace["status"],
            "awaited_key_not_seen_in_candidate_writes",
        )
        self.assertEqual(producer_trace["scan_key_value_write_count"], 0)
        self.assertFalse(producer_trace["awaited_key_enqueue_observed"])
        self.assertEqual(producer_trace["observed_work_item_key_values"], ["0x00000401"])
        self.assertEqual(
            producer_trace["key_flow"]["consumer_requested_key"]["stack_offset_hex"],
            "0x00000024",
        )
        self.assertEqual(
            producer_trace["key_flow"]["producer_enqueued_key"]["write_instruction_address_hex"],
            "0x000F2A63",
        )
        self.assertEqual(
            producer_trace["key_flow"]["producer_enqueued_key"]["observed_values_hex"],
            ["0x00000401"],
        )
        self.assertFalse(
            producer_trace["key_flow"]["awaited_key_enqueued_in_observed_trace"]
        )
        self.assertTrue(
            producer_trace["key_flow"]["producer_key_source_matches_consumer_offset"]
        )
        self.assertEqual(producer_trace["candidate_instruction_group_count"], 3)
        self.assertEqual(
            producer_trace["producer_semantic_counts"],
            {
                "queue_slot_replacement_writer": 1,
                "work_item_key_writer": 1,
                "work_item_next_link": 1,
            },
        )
        self.assertEqual(
            producer_trace["queue_node_layout"]["key_offset_hex"],
            "0x00000008",
        )
        self.assertEqual(
            producer_trace["latest_current_node_key_writer"]["instruction_address_hex"],
            "0x000F29F6",
        )
        self.assertEqual(
            producer_trace["latest_current_node_key_writer"]["semantic"],
            "queue_slot_replacement_writer",
        )
        self.assertEqual(
            producer_trace["next_recovery_anchor"]["instruction_address_hex"],
            "0x000F29F6",
        )
        self.assertEqual(
            producer_trace["next_recovery_anchor"]["roles"],
            ["current_node_key_writer"],
        )
        self.assertEqual(
            producer_trace["watched_address_transitions"][0]["values_hex"],
            ["0x00000401", "0x003445C0"],
        )
        self.assertEqual(
            producer_trace["instruction_groups"][0]["semantic"],
            "work_item_key_writer",
        )
        self.assertEqual(
            producer_trace["instruction_groups"][1]["semantic"],
            "work_item_next_link",
        )

    def test_heap_free_list_boundary_classifies_null_head_walk(self) -> None:
        state = CpuState.with_registers(
            ebx=0x10000000,
            ecx=0,
            edx=0xFFFFFFF8,
            esi=0x10000180,
            edi=0x4B9A,
        )
        trace_events = [
            {
                "sequence": 0,
                "address": 0x000E46B1,
                "address_hex": "0x000E46B1",
                "operation": "memory_write",
                "details": {
                    "memory_address": 0x10000180,
                    "value": 0x10000180,
                    "size": 4,
                },
            },
            {
                "sequence": 1,
                "address": 0x000E5080,
                "address_hex": "0x000E5080",
                "operation": "memory_read",
                "details": {
                    "memory_address": 0xFFFFFFF8,
                    "value": 0,
                },
            },
            {
                "sequence": 2,
                "address": 0x000E5085,
                "address_hex": "0x000E5085",
                "operation": "memory_read",
                "details": {
                    "memory_address": 0,
                    "value": 0,
                },
            },
            {
                "sequence": 3,
                "address": 0x000E5087,
                "address_hex": "0x000E5087",
                "operation": "jump",
                "details": {
                    "target": 0x000E506F,
                    "target_hex": "0x000E506F",
                },
            },
        ]

        boundary = _heap_free_list_boundary_from_step_limit(
            trace_events,
            state,
            65536,
        )

        self.assertIsNotNone(boundary)
        assert boundary is not None
        self.assertEqual(boundary["status"], "heap_free_list_boundary")
        self.assertEqual(boundary["boundary_kind"], "title_heap_free_list_scan")
        self.assertEqual(boundary["loop_entry_hex"], "0x000E506F")
        scan = boundary["heap_free_list_scan"]
        self.assertEqual(scan["diagnosis"], "free_list_head_resolved_to_null")
        self.assertEqual(scan["heap_base_hex"], "0x10000000")
        self.assertEqual(scan["list_head_address_hex"], "0x10000180")
        self.assertEqual(scan["current_link_hex"], "0x00000000")
        self.assertEqual(scan["candidate_header_hex"], "0xFFFFFFF8")
        self.assertEqual(scan["free_chunk_units_hex"], "0x00004B9A")
        self.assertTrue(scan["current_link_is_null"])
        self.assertTrue(scan["candidate_header_underflow"])
        self.assertEqual(scan["head_write_count"], 1)
        self.assertEqual(scan["latest_head_writes"][0]["value_hex"], "0x10000180")
        self.assertEqual(
            [read["label"] for read in scan["reads"]],
            ["candidate_header_size", "next_free_link"],
        )

    def test_dynamic_block_cache_round_trips_decoded_metadata(self) -> None:
        function = LiftedFunction(
            symbol="dynamic_block_00001000",
            base_address=0x1000,
            code_size=2,
            instructions=(
                X86Instruction(
                    address=0x1000,
                    size=2,
                    mnemonic="mov",
                    operands=(Operand.register("eax"), Operand.immediate_u32(7)),
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "dynamic-block-cache.json"
            cache = DynamicBlockCache(path)
            key = cache.key(
                image_sha256="ABCDEF",
                target=0x1000,
                entry_bytes=0x200,
                max_block_instructions=224,
            )
            self.assertIsNone(cache.get(key))
            cache.put(key, function)
            cache.save()

            loaded_cache = DynamicBlockCache(path)
            cached = loaded_cache.get(key)

        self.assertIsNotNone(cached)
        assert cached is not None
        self.assertEqual(cached.symbol, "dynamic_block_00001000")
        self.assertEqual(cached.instructions[0].mnemonic, "mov")
        self.assertEqual(cached.instructions[0].operands[1].immediate, 7)

    def test_recovered_render_command_stream_captures_d3d_writes(self) -> None:
        trace_events = [
            {
                "sequence": 7,
                "address_hex": "0x0028CEB2",
                "operation": "memory_write",
                "details": {
                    "memory_address": 0xFED00040,
                    "value": 0x00002A29,
                },
            },
            {
                "sequence": 8,
                "address_hex": "0x0028CEBB",
                "operation": "memory_write",
                "details": {
                    "memory_address": 0x8000070C,
                    "value": 0x0000236F,
                    "size": 2,
                },
            },
            {
                "sequence": 9,
                "address_hex": "0x0028CEC0",
                "operation": "memory_write",
                "details": {
                    "memory_address": 0x1000,
                    "value": 0x11111111,
                },
            },
        ]

        stream = _recovered_render_command_stream(trace_events)

        self.assertEqual(stream["write_count"], 2)
        self.assertEqual(stream["mmio_write_count"], 1)
        self.assertEqual(stream["push_buffer_write_count"], 1)
        self.assertEqual(stream["writes"][0]["kind"], "d3d_mmio")
        self.assertEqual(stream["writes"][0]["offset_hex"], "0x00000040")
        self.assertEqual(stream["writes"][1]["kind"], "d3d_push_buffer")
        self.assertEqual(stream["writes"][1]["value_hex"], "0x0000236F")
        self.assertEqual(stream["writes"][1]["size"], 2)

    def test_asset_io_summary_identifies_next_edge_after_dash_and_d_link(self) -> None:
        invocations = [
            {
                "shim_name": "NtCreateFile",
                "result": {
                    "status": XboxStatus.SUCCESS,
                    "handle": 0x108,
                    "guest_path": "d:\\dashupdate.xbe",
                    "root_kind": "disc",
                    "mode": "rb",
                    "desired_access": 0x80100080,
                },
            },
            {
                "shim_name": "NtReadFile",
                "result": {
                    "status": XboxStatus.SUCCESS,
                    "handle": 0x108,
                    "requested_length": 376,
                    "bytes_read": 376,
                    "data_elided": True,
                },
            },
            {
                "shim_name": "NtQueryInformationFile",
                "result": {
                    "status": XboxStatus.SUCCESS,
                    "handle": 0x108,
                    "guest_path": "d:\\dashupdate.xbe",
                    "file_information_class": 14,
                    "position": 376,
                    "size": 1024,
                },
            },
            {
                "shim_name": "NtSetInformationFile",
                "result": {
                    "status": XboxStatus.SUCCESS,
                    "handle": 0x108,
                    "guest_path": "d:\\dashupdate.xbe",
                    "file_information_class": 14,
                    "position": 760,
                    "bytes_consumed": 8,
                },
            },
            {
                "shim_name": "NtReadFile",
                "result": {
                    "status": XboxStatus.SUCCESS,
                    "handle": 0x108,
                    "requested_length": 464,
                    "bytes_read": 464,
                    "data_elided": True,
                },
            },
            {"shim_name": "NtClose", "arguments": ["0x00000108"], "result": 0},
            {
                "shim_name": "NtCreateFile",
                "result": {
                    "status": XboxStatus.NO_SUCH_FILE,
                    "handle": None,
                    "guest_path": "y:\\xboxdash.xbe",
                    "root_kind": "cache",
                    "mode": "rb",
                },
            },
            {
                "shim_name": "NtOpenSymbolicLinkObject",
                "result": {
                    "status": XboxStatus.SUCCESS,
                    "handle": 0x10C,
                    "guest_path": "\\??\\D:",
                    "target": "\\Device\\Cdrom0",
                },
            },
            {
                "shim_name": "NtQuerySymbolicLinkObject",
                "result": {
                    "status": XboxStatus.SUCCESS,
                    "handle": 0x10C,
                    "name": "\\??\\D:",
                    "target": "\\Device\\Cdrom0",
                },
            },
            {"shim_name": "NtClose", "arguments": ["0x0000010C"], "result": 0},
            {
                "shim_name": "NtOpenFile",
                "result": {
                    "status": XboxStatus.NO_SUCH_FILE,
                    "handle": None,
                    "guest_path": "\\Device\\Harddisk0\\partition2\\XODash\\xonlinedash.xbe",
                    "root_kind": "dashboard",
                    "mode": "rb",
                    "desired_access": 0x80100000,
                },
            },
            {
                "shim_name": "NtOpenFile",
                "result": {
                    "status": XboxStatus.NO_SUCH_FILE,
                    "handle": None,
                    "guest_path": "\\Device\\Cdrom0\\XODash\\xonlinedash.xbe",
                    "root_kind": "disc",
                    "mode": "rb",
                    "desired_access": 0x80100000,
                },
            },
        ]

        summary = _asset_io_summary_from_invocations(invocations)

        self.assertTrue(summary["dashupdate_sequence"]["complete"])
        self.assertEqual(summary["dashupdate_sequence"]["read_count"], 2)
        self.assertEqual(summary["dashupdate_sequence"]["bytes_read_total"], 840)
        self.assertTrue(summary["symbolic_link_sequence"]["complete"])
        self.assertEqual(summary["symbolic_link_sequence"]["target"], "\\Device\\Cdrom0")
        self.assertEqual(
            summary["next_edge_after_dashupdate_and_d_link"]["guest_path"],
            "\\Device\\Harddisk0\\partition2\\XODash\\xonlinedash.xbe",
        )
        self.assertEqual(
            summary["next_edge_after_dashupdate_and_d_link"]["root_kind"],
            "dashboard",
        )
        self.assertEqual(summary["root_kind_counts"]["cache"], 1)
        self.assertGreaterEqual(summary["status_counts"]["not_found"], 3)
        assessment = summary["dashboard_cache_probe_assessment"]
        self.assertEqual(
            assessment["status"],
            "deterministic_not_found_preserved_for_observed_probes",
        )
        self.assertEqual(assessment["dashboard_probe_count"], 1)
        self.assertEqual(assessment["cache_probe_count"], 1)
        self.assertEqual(assessment["clean_not_found_probe_count"], 2)
        self.assertFalse(assessment["requires_configured_roots_for_observed_edges"])
        self.assertEqual(
            assessment["later_gameplay_requirement"],
            "unproven_until_execution_advances_past_current_scheduler_boundary",
        )

    def test_deterministic_service_validation_summarizes_runtime_models(self) -> None:
        runtime_summary = {
            "trace": [
                {
                    "subsystem": "filesystem",
                    "operation": "read_file",
                    "details": {
                        "bytes_read": 128,
                        "deterministic_latency_100ns": 750,
                    },
                },
                {
                    "subsystem": "filesystem",
                    "operation": "write_file",
                    "details": {"bytes_written": 32},
                },
            ],
            "open_files": [
                {"streaming": True, "save_data": False, "cache_data": False},
                {"streaming": False, "save_data": True, "cache_data": False},
            ],
            "audio_initialized": True,
            "audio_streams": [
                {
                    "submitted_buffer_count": 2,
                    "queued_bytes": 64,
                    "played_bytes": 128,
                }
            ],
            "input": {
                "sequence": 4,
                "ports": {
                    "0": {"poll_count": 2, "last_latency_samples": 1},
                    "1": {"poll_count": 0, "last_latency_samples": 0},
                },
            },
            "clock": {"performance_counter": 10},
            "determinism": {"input": "sequenced_snapshots"},
        }

        summary = _deterministic_service_validation_summary(runtime_summary)

        self.assertEqual(summary["streaming"]["read_event_count"], 1)
        self.assertEqual(summary["streaming"]["bytes_read_total"], 128)
        self.assertEqual(
            summary["streaming"]["deterministic_latency_100ns_total"],
            750,
        )
        self.assertEqual(summary["save_data"]["write_event_count"], 1)
        self.assertEqual(summary["save_data"]["open_save_file_count"], 1)
        self.assertEqual(summary["audio"]["submitted_buffer_count"], 2)
        self.assertEqual(summary["input_latency"]["poll_count"], 2)
        self.assertEqual(summary["input_latency"]["max_last_latency_samples"], 1)

    def test_runtime_abi_bridge_initializes_guest_ansi_string(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[289])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory(
            {
                0x7000: b"media\\boot.ini\x00",
                0x8000: 0xDEADC0DE,
                0x8004: 0x6000,
                0x8008: 0x7000,
            }
        )

        bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "RtlInitAnsiString")
        self.assertEqual(invocation.stack_cleanup_bytes, 8)
        self.assertEqual(memory.read(0x6000, 2), (14).to_bytes(2, "little"))
        self.assertEqual(memory.read(0x6002, 2), (15).to_bytes(2, "little"))
        self.assertEqual(memory.read_u32(0x6004), 0x7000)
        self.assertEqual(
            [write["label"] for write in invocation.memory_writes],
            ["ansi_string"],
        )

    def test_runtime_abi_bridge_decodes_nt_open_file_object_attributes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            media = root / "media"
            media.mkdir()
            (media / "intro.bin").write_bytes(b"burnout")
            resolver = ImportResolver()
            runtime = XboxRuntimeShims(
                XboxRuntimeConfig(extracted_disc_root=root)
            )
            runtime.register_kernel_imports(resolver, imported_ordinals=[202])
            bridge = RuntimeAbiBridge(runtime)
            target = runtime.registered_shims[0].target_address
            path = b"D:\\media\\intro.bin"
            ansi_string = (
                len(path).to_bytes(2, "little")
                + (len(path) + 1).to_bytes(2, "little")
                + (0x7000).to_bytes(4, "little")
            )
            object_attributes = bytearray(24)
            struct.pack_into("<I", object_attributes, 8, 0x6000)
            state = CpuState.with_registers(esp=0x8000)
            memory = SparseMemory(
                {
                    0x6000: ansi_string,
                    0x6100: bytes(object_attributes),
                    0x7000: path + b"\x00",
                    0x8000: 0xDEADC0DE,
                    0x8004: 0x6200,
                    0x8008: 0x80100000,
                    0x800C: 0x6100,
                    0x8010: 0x6210,
                    0x8014: 0x00000001,
                    0x8018: 0x00000000,
                }
            )

            bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "NtOpenFile")
        self.assertEqual(invocation.stack_cleanup_bytes, 24)
        self.assertEqual(invocation.eax, 0)
        self.assertEqual(invocation.result["guest_path"], "D:\\media\\intro.bin")
        self.assertEqual(memory.read_u32(0x6200), invocation.result["handle"])
        self.assertEqual(memory.read_u32(0x6210), 0)
        self.assertEqual(memory.read_u32(0x8000 + 24), 0xDEADC0DE)

    def test_runtime_abi_bridge_decodes_nt_open_symbolic_link_object(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[203])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        path = b"\\??\\D:"
        ansi_string = (
            len(path).to_bytes(2, "little")
            + (len(path) + 1).to_bytes(2, "little")
            + (0x7000).to_bytes(4, "little")
        )
        object_attributes = bytearray(24)
        struct.pack_into("<I", object_attributes, 8, 0x6000)
        state = CpuState.with_registers(esp=0x8000)
        arguments = (0x6200, 0x6100)
        memory = SparseMemory(
            {
                0x6000: ansi_string,
                0x6100: bytes(object_attributes),
                0x7000: path + b"\x00",
                0x8000: 0xDEADC0DE,
            }
        )
        for index, argument in enumerate(arguments):
            memory.write_u32(0x8004 + index * 4, argument)

        bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "NtOpenSymbolicLinkObject")
        self.assertEqual(invocation.arguments, arguments)
        self.assertEqual(invocation.handler_arguments, arguments)
        self.assertEqual(invocation.stack_cleanup_bytes, 8)
        self.assertEqual(invocation.eax, XboxStatus.SUCCESS)
        self.assertEqual(invocation.result["guest_path"], "\\??\\D:")
        self.assertEqual(invocation.result["target"], "\\Device\\Cdrom0")
        self.assertEqual(memory.read_u32(0x6200), invocation.result["handle"])
        self.assertEqual(memory.read_u32(0x8000 + 8), 0xDEADC0DE)
        self.assertEqual(
            [write["label"] for write in invocation.memory_writes],
            ["symbolic_link_handle"],
        )

    def test_runtime_abi_bridge_materializes_nt_query_symbolic_link_object(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        opened = runtime.nt_open_symbolic_link_object("\\??\\D:")
        runtime.register_kernel_imports(resolver, imported_ordinals=[215])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        state = CpuState.with_registers(esp=0x8000)
        ansi_string = (
            (0).to_bytes(2, "little")
            + (32).to_bytes(2, "little")
            + (0x6400).to_bytes(4, "little")
        )
        arguments = (opened["handle"], 0x6300, 0x6500)
        memory = SparseMemory(
            {
                0x6300: ansi_string,
                0x8000: 0xDEADC0DE,
            }
        )
        for index, argument in enumerate(arguments):
            memory.write_u32(0x8004 + index * 4, argument)

        bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        target_bytes = b"\\Device\\Cdrom0"
        self.assertEqual(invocation.shim_name, "NtQuerySymbolicLinkObject")
        self.assertEqual(invocation.arguments, arguments)
        self.assertEqual(invocation.handler_arguments, arguments)
        self.assertEqual(invocation.stack_cleanup_bytes, 12)
        self.assertEqual(invocation.eax, XboxStatus.SUCCESS)
        self.assertEqual(memory.read(0x6400, len(target_bytes)), target_bytes)
        self.assertEqual(memory.read(0x6300, 2), len(target_bytes).to_bytes(2, "little"))
        self.assertEqual(memory.read_u32(0x6500), len(target_bytes))
        self.assertEqual(memory.read_u32(0x8000 + 12), 0xDEADC0DE)
        self.assertEqual(
            [write["label"] for write in invocation.memory_writes],
            ["symbolic_link_target", "returned_length"],
        )

    def test_runtime_abi_bridge_materializes_nt_read_file_buffer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            media = root / "media"
            media.mkdir()
            (media / "intro.bin").write_bytes(b"burnout")
            resolver = ImportResolver()
            runtime = XboxRuntimeShims(
                XboxRuntimeConfig(extracted_disc_root=root)
            )
            opened = runtime.filesystem.open_file("D:\\media\\intro.bin", "rb")
            runtime.register_kernel_imports(resolver, imported_ordinals=[219])
            bridge = RuntimeAbiBridge(runtime)
            target = runtime.registered_shims[0].target_address
            state = CpuState.with_registers(esp=0x8000)
            arguments = (
                opened["handle"],
                0,
                0,
                0,
                0x6200,
                0x6300,
                4,
                0x6400,
            )
            memory = SparseMemory(
                {
                    0x6400: 2,
                    0x6404: 0,
                    0x8000: 0xDEADC0DE,
                }
            )
            for index, argument in enumerate(arguments):
                memory.write_u32(0x8004 + index * 4, argument)

            bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "NtReadFile")
        self.assertEqual(invocation.arguments, arguments)
        self.assertEqual(invocation.handler_arguments, arguments)
        self.assertEqual(invocation.eax, XboxStatus.SUCCESS)
        self.assertEqual(memory.read(0x6300, 4), b"rnou")
        self.assertEqual(memory.read_u32(0x6200), XboxStatus.SUCCESS)
        self.assertEqual(memory.read_u32(0x6204), 4)
        self.assertNotIn("data", invocation.result)
        self.assertTrue(invocation.result["data_elided"])
        self.assertEqual(invocation.result["bytes_read"], 4)
        self.assertEqual(
            [write["label"] for write in invocation.memory_writes],
            ["read_buffer", "io_status", "io_information"],
        )

    def test_runtime_abi_bridge_materializes_nt_query_information_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            media = root / "media"
            media.mkdir()
            (media / "intro.bin").write_bytes(b"burnout")
            resolver = ImportResolver()
            runtime = XboxRuntimeShims(
                XboxRuntimeConfig(extracted_disc_root=root)
            )
            opened = runtime.filesystem.open_file("D:\\media\\intro.bin", "rb")
            runtime.register_kernel_imports(resolver, imported_ordinals=[211])
            bridge = RuntimeAbiBridge(runtime)
            target = runtime.registered_shims[0].target_address
            state = CpuState.with_registers(esp=0x8000)
            arguments = (
                opened["handle"],
                0x6200,
                0x6300,
                24,
                5,
            )
            memory = SparseMemory({0x8000: 0xDEADC0DE})
            for index, argument in enumerate(arguments):
                memory.write_u32(0x8004 + index * 4, argument)

            bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "NtQueryInformationFile")
        self.assertEqual(invocation.arguments, arguments)
        self.assertEqual(invocation.handler_arguments, arguments)
        self.assertEqual(invocation.eax, XboxStatus.SUCCESS)
        self.assertEqual(memory.read_u32(0x6200), XboxStatus.SUCCESS)
        self.assertEqual(memory.read_u32(0x6204), 24)
        self.assertEqual(memory.read_u32(0x6300), 2048)
        self.assertEqual(memory.read_u32(0x6308), 7)
        self.assertEqual(memory.read_u32(0x6310), 1)
        self.assertEqual(
            [write["label"] for write in invocation.memory_writes],
            ["file_information", "io_status", "io_information"],
        )

    def test_runtime_abi_bridge_materializes_xbox_directory_information(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            save_root = root / "save"
            profile = save_root / "UDATA" / "41430019" / "Profile1"
            profile.mkdir(parents=True)
            resolver = ImportResolver()
            runtime = XboxRuntimeShims(
                XboxRuntimeConfig(
                    extracted_disc_root=root,
                    save_data_root=save_root,
                    title_id=0x41430019,
                )
            )
            opened = runtime.filesystem.open_file("U:\\", "rb")
            runtime.register_kernel_imports(resolver, imported_ordinals=[207])
            bridge = RuntimeAbiBridge(runtime)
            target = runtime.registered_shims[0].target_address
            state = CpuState.with_registers(esp=0x8000)
            arguments = (
                opened["handle"],
                0,
                0,
                0,
                0x6200,
                0x6300,
                0x148,
                1,
                0x6500,
                0,
            )
            mask = (
                (3).to_bytes(2, "little")
                + (4).to_bytes(2, "little")
                + (0x6600).to_bytes(4, "little")
            )
            memory = SparseMemory(
                {
                    0x6500: mask,
                    0x6600: b"*.*\x00",
                    0x8000: 0xDEADC0DE,
                }
            )
            for index, argument in enumerate(arguments):
                memory.write_u32(0x8004 + index * 4, argument)

            bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "NtQueryDirectoryFile")
        self.assertEqual(invocation.stack_cleanup_bytes, 40)
        self.assertEqual(invocation.eax, XboxStatus.SUCCESS)
        self.assertEqual(memory.read_u32(0x6200), XboxStatus.SUCCESS)
        self.assertEqual(memory.read_u32(0x6204), 72)
        self.assertEqual(memory.read_u32(0x6300), 0)
        self.assertEqual(memory.read_u32(0x6338), 0x10)
        self.assertEqual(memory.read_u32(0x633C), 8)
        self.assertEqual(memory.read(0x6340, 8), b"Profile1")
        self.assertEqual(invocation.result["file_mask"], "*.*")
        self.assertEqual(invocation.result["directory_record_bytes"], 72)
        self.assertEqual(
            [write["label"] for write in invocation.memory_writes],
            ["directory_information", "io_status", "io_information"],
        )

    def test_runtime_abi_bridge_materializes_volume_size_and_cleans_five_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            save_root = root / "save"
            resolver = ImportResolver()
            runtime = XboxRuntimeShims(
                XboxRuntimeConfig(
                    extracted_disc_root=root,
                    save_data_root=save_root,
                    title_id=0x41430019,
                )
            )
            opened = runtime.filesystem.open_file("U:\\", "rb")
            runtime.register_kernel_imports(resolver, imported_ordinals=[218])
            bridge = RuntimeAbiBridge(runtime)
            target = runtime.registered_shims[0].target_address
            state = CpuState.with_registers(esp=0x8000)
            arguments = (opened["handle"], 0x6200, 0x6300, 24, 3)
            memory = SparseMemory({0x8000: 0xDEADC0DE})
            for index, argument in enumerate(arguments):
                memory.write_u32(0x8004 + index * 4, argument)

            bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "NtQueryVolumeInformationFile")
        self.assertEqual(invocation.arguments, arguments)
        self.assertEqual(invocation.stack_cleanup_bytes, 20)
        self.assertEqual(invocation.eax, XboxStatus.SUCCESS)
        self.assertEqual(memory.read_u32(0x6200), XboxStatus.SUCCESS)
        self.assertEqual(memory.read_u32(0x6204), 24)
        self.assertEqual(int.from_bytes(memory.read(0x6300, 8), "little"), 0x9896B0 // 32)
        self.assertEqual(memory.read_u32(0x6310), 32)
        self.assertEqual(memory.read_u32(0x6314), 512)
        self.assertEqual(memory.read_u32(0x8000 + 20), 0xDEADC0DE)
        self.assertEqual(invocation.result["volume_information_bytes"], 24)
        self.assertEqual(
            [write["label"] for write in invocation.memory_writes],
            ["volume_information", "io_status", "io_information"],
        )

    def test_runtime_abi_bridge_materializes_nt_set_information_file_position(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            media = root / "media"
            media.mkdir()
            (media / "intro.bin").write_bytes(b"burnout")
            resolver = ImportResolver()
            runtime = XboxRuntimeShims(
                XboxRuntimeConfig(extracted_disc_root=root)
            )
            opened = runtime.filesystem.open_file("D:\\media\\intro.bin", "rb")
            runtime.register_kernel_imports(resolver, imported_ordinals=[226])
            bridge = RuntimeAbiBridge(runtime)
            target = runtime.registered_shims[0].target_address
            state = CpuState.with_registers(esp=0x8000)
            arguments = (
                opened["handle"],
                0x6200,
                0x6300,
                8,
                14,
            )
            memory = SparseMemory(
                {
                    0x6300: (2).to_bytes(8, "little"),
                    0x8000: 0xDEADC0DE,
                }
            )
            for index, argument in enumerate(arguments):
                memory.write_u32(0x8004 + index * 4, argument)

            bridge.invoke(state, memory, target, ExecutionTrace())

            handle_info = runtime.filesystem.query_file_handle_information(
                opened["handle"]
            )
            positioned_read = runtime.filesystem.read_file(opened["handle"], 4)

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "NtSetInformationFile")
        self.assertEqual(invocation.arguments, arguments)
        self.assertEqual(invocation.handler_arguments, arguments)
        self.assertEqual(invocation.stack_cleanup_bytes, 20)
        self.assertEqual(invocation.eax, XboxStatus.SUCCESS)
        self.assertEqual(handle_info["position"], 2)
        self.assertEqual(positioned_read["data"], b"rnou")
        self.assertEqual(memory.read_u32(0x6200), XboxStatus.SUCCESS)
        self.assertEqual(memory.read_u32(0x6204), 8)
        self.assertEqual(memory.read_u32(0x8000 + 20), 0xDEADC0DE)
        self.assertEqual(
            [write["label"] for write in invocation.memory_writes],
            ["io_status", "io_information"],
        )

    def test_runtime_abi_bridge_materializes_nt_write_file_save_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            save_root = root / "save"
            resolver = ImportResolver()
            runtime = XboxRuntimeShims(
                XboxRuntimeConfig(extracted_disc_root=root, save_data_root=save_root)
            )
            opened = runtime.filesystem.open_file(
                "\\Device\\Harddisk0\\Partition1\\UDATA\\41430019\\save.dat",
                "wb",
            )
            runtime.register_kernel_imports(resolver, imported_ordinals=[236])
            bridge = RuntimeAbiBridge(runtime)
            target = runtime.registered_shims[0].target_address
            state = CpuState.with_registers(esp=0x8000)
            arguments = (
                opened["handle"],
                0,
                0,
                0,
                0x6200,
                0x6300,
                8,
                0,
            )
            memory = SparseMemory(
                {
                    0x6300: b"progress",
                    0x8000: 0xDEADC0DE,
                }
            )
            for index, argument in enumerate(arguments):
                memory.write_u32(0x8004 + index * 4, argument)

            bridge.invoke(state, memory, target, ExecutionTrace())

            written = save_root / "UDATA" / "41430019" / "save.dat"
            self.assertEqual(written.read_bytes(), b"progress")

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "NtWriteFile")
        self.assertEqual(invocation.arguments, arguments)
        self.assertEqual(invocation.handler_arguments, arguments)
        self.assertEqual(invocation.eax, XboxStatus.SUCCESS)
        self.assertEqual(memory.read_u32(0x6200), XboxStatus.SUCCESS)
        self.assertEqual(memory.read_u32(0x6204), 8)
        self.assertEqual(invocation.result["bytes_written"], 8)
        self.assertEqual(
            [write["label"] for write in invocation.memory_writes],
            ["io_status", "io_information"],
        )

    def test_runtime_abi_bridge_creates_save_file_from_guest_attributes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            save_root = root / "save"
            resolver = ImportResolver()
            runtime = XboxRuntimeShims(
                XboxRuntimeConfig(extracted_disc_root=root, save_data_root=save_root)
            )
            runtime.register_kernel_imports(resolver, imported_ordinals=[190])
            bridge = RuntimeAbiBridge(runtime)
            target = runtime.registered_shims[0].target_address
            path = b"\\Device\\Harddisk0\\Partition1\\UDATA\\41430019\\settings.dat"
            ansi_string = (
                len(path).to_bytes(2, "little")
                + (len(path) + 1).to_bytes(2, "little")
                + (0x7000).to_bytes(4, "little")
            )
            object_attributes = bytearray(24)
            struct.pack_into("<I", object_attributes, 8, 0x6000)
            state = CpuState.with_registers(esp=0x8000)
            memory = SparseMemory(
                {
                    0x6000: ansi_string,
                    0x6100: bytes(object_attributes),
                    0x7000: path + b"\x00",
                    0x8000: 0xDEADC0DE,
                    0x8004: 0x6200,
                    0x8008: 0x40000002,
                    0x800C: 0x6100,
                    0x8010: 0x6210,
                    0x8014: 0,
                    0x8018: 0,
                    0x801C: 0,
                    0x8020: 3,
                    0x8024: 0,
                    0x8028: 0,
                    0x802C: 0,
                }
            )

            bridge.invoke(state, memory, target, ExecutionTrace())

            created = save_root / "UDATA" / "41430019" / "settings.dat"
            self.assertTrue(created.is_file())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "NtCreateFile")
        self.assertEqual(invocation.eax, XboxStatus.SUCCESS)
        self.assertEqual(invocation.result["mode"], "ab")
        self.assertEqual(memory.read_u32(0x6200), invocation.result["handle"])
        self.assertEqual(memory.read_u32(0x6210), XboxStatus.SUCCESS)

    def test_playability_probe_executes_synthetic_entry_import_call(self) -> None:
        blob, layout = _synthetic_xbe()
        data = bytearray(blob)
        text_raw = 0x1000
        entry_raw = text_raw + 0x20
        data[entry_raw : entry_raw + 7] = _call_indirect_bytes(
            layout["kernel_thunk_addr"]
        )
        struct.pack_into("<I", data, text_raw + 0x100, 0x8000007F)
        struct.pack_into("<I", data, text_raw + 0x104, 0)

        with tempfile.TemporaryDirectory() as temp_dir:
            xbe_path = Path(temp_dir) / "default.xbe"
            xbe_path.write_bytes(bytes(data))
            summary = build_playability_probe_summary(
                xbe_path,
                entry_bytes=16,
                max_instructions=8,
            )

        self.assertEqual(summary["format"], "b2-recomp-playability-probe")
        self.assertEqual(summary["runtime"]["registered_kernel_shim_count"], 1)
        self.assertEqual(summary["entry_recovery"]["status"], "decoded")
        self.assertEqual(summary["entry_recovery"]["instruction_count"], 2)
        self.assertEqual(summary["entry_recovery"]["execution"]["status"], "returned")
        self.assertEqual(summary["runtime_abi_bridge"]["invocation_count"], 1)
        self.assertEqual(
            summary["runtime_abi_bridge"]["invocations"][0]["shim_name"],
            "KeQueryPerformanceFrequency",
        )

    def test_playability_probe_dispatches_runtime_tail_jump(self) -> None:
        blob, layout = _synthetic_xbe()
        data = bytearray(blob)
        text_raw = 0x1000
        entry_raw = text_raw + 0x20
        entry = _jmp_indirect_bytes(layout["kernel_thunk_addr"]) + b"\xC3"
        data[entry_raw : entry_raw + len(entry)] = entry
        struct.pack_into("<I", data, text_raw + 0x100, 0x8000007F)
        struct.pack_into("<I", data, text_raw + 0x104, 0)

        with tempfile.TemporaryDirectory() as temp_dir:
            xbe_path = Path(temp_dir) / "default.xbe"
            xbe_path.write_bytes(bytes(data))
            summary = build_playability_probe_summary(
                xbe_path,
                entry_bytes=16,
                max_instructions=8,
            )

        self.assertEqual(summary["entry_recovery"]["execution"]["status"], "returned")
        self.assertEqual(summary["runtime_abi_bridge"]["invocation_count"], 1)
        self.assertEqual(
            summary["runtime_abi_bridge"]["invocations"][0]["shim_name"],
            "KeQueryPerformanceFrequency",
        )

    def test_playability_probe_recovers_first_internal_handoff_import_call(self) -> None:
        blob, layout = _synthetic_xbe()
        data = bytearray(blob)
        text_raw = 0x1000
        entry_va = layout["text_va"] + 0x20
        handoff_va = layout["text_va"] + 0x40
        entry_raw = text_raw + 0x20
        handoff_raw = text_raw + 0x40
        rel = handoff_va - (entry_va + 5)
        data[entry_raw : entry_raw + 6] = b"\xE8" + struct.pack("<i", rel) + b"\xC3"
        data[handoff_raw : handoff_raw + 7] = _call_indirect_bytes(
            layout["kernel_thunk_addr"]
        )
        struct.pack_into("<I", data, text_raw + 0x100, 0x8000007F)
        struct.pack_into("<I", data, text_raw + 0x104, 0)

        with tempfile.TemporaryDirectory() as temp_dir:
            xbe_path = Path(temp_dir) / "default.xbe"
            xbe_path.write_bytes(bytes(data))
            summary = build_playability_probe_summary(
                xbe_path,
                entry_bytes=0x80,
                max_instructions=16,
            )

        handoffs = summary["entry_recovery"]["internal_handoffs"]
        self.assertEqual(summary["entry_recovery"]["execution"]["status"], "returned")
        self.assertEqual(
            summary["entry_recovery"]["execution"]["executed_internal_targets"],
            [f"0x{handoff_va:08X}"],
        )
        self.assertEqual(len(handoffs), 1)
        self.assertEqual(handoffs[0]["status"], "decoded")
        self.assertEqual(summary["runtime_abi_bridge"]["invocation_count"], 1)
        self.assertEqual(
            summary["runtime_abi_bridge"]["invocations"][0]["shim_name"],
            "KeQueryPerformanceFrequency",
        )

    def test_playability_probe_batches_branch_target_and_fallthrough_blocks(self) -> None:
        blob, layout = _synthetic_xbe()
        data = bytearray(blob)
        text_raw = 0x1000
        entry_va = layout["text_va"] + 0x20
        handoff_va = layout["text_va"] + 0x40
        fallthrough_va = handoff_va + 4
        branch_target_va = handoff_va + 10
        entry_raw = text_raw + 0x20
        handoff_raw = text_raw + 0x40
        rel = handoff_va - (entry_va + 5)
        data[entry_raw : entry_raw + 6] = b"\xE8" + struct.pack("<i", rel) + b"\xC3"
        handoff = (
            b"\x33\xC0"
            b"\x74\x06"
            b"\xB8\x11\x11\x00\x00"
            b"\xC3"
            b"\xB8\x22\x22\x00\x00"
            b"\xC3"
        )
        data[handoff_raw : handoff_raw + len(handoff)] = handoff
        struct.pack_into("<I", data, text_raw + 0x100, 0)

        with tempfile.TemporaryDirectory() as temp_dir:
            xbe_path = Path(temp_dir) / "default.xbe"
            xbe_path.write_bytes(bytes(data))
            summary = build_playability_probe_summary(
                xbe_path,
                entry_bytes=0x80,
                max_instructions=16,
                max_block_instructions=4,
            )

        recovery = summary["entry_recovery"]["control_flow_recovery"]
        decoded_targets = set(recovery["decoded_block_targets"])
        self.assertEqual(summary["entry_recovery"]["execution"]["status"], "returned")
        self.assertEqual(
            summary["entry_recovery"]["execution"]["state"]["registers"]["eax"],
            "0x00002222",
        )
        self.assertEqual(recovery["frontier_count"], 0)
        self.assertIn(f"0x{handoff_va:08X}", decoded_targets)
        self.assertIn(f"0x{fallthrough_va:08X}", decoded_targets)
        self.assertIn(f"0x{branch_target_va:08X}", decoded_targets)

    def test_playability_probe_recovers_executed_indirect_call_block(self) -> None:
        blob, layout = _synthetic_xbe()
        data = bytearray(blob)
        text_raw = 0x1000
        entry_va = layout["text_va"] + 0x20
        pointer_va = layout["text_va"] + 0x140
        dynamic_va = layout["text_va"] + 0x180
        entry_raw = text_raw + 0x20
        pointer_raw = text_raw + 0x140
        dynamic_raw = text_raw + 0x180
        data[entry_raw : entry_raw + 7] = _call_indirect_bytes(pointer_va)
        struct.pack_into("<I", data, pointer_raw, dynamic_va)
        data[dynamic_raw : dynamic_raw + 6] = b"\xB8\x55\x44\x00\x00\xC3"

        with tempfile.TemporaryDirectory() as temp_dir:
            xbe_path = Path(temp_dir) / "default.xbe"
            xbe_path.write_bytes(bytes(data))
            summary = build_playability_probe_summary(
                xbe_path,
                entry_bytes=0x200,
                max_instructions=8,
                max_block_instructions=8,
            )

        execution = summary["entry_recovery"]["execution"]
        self.assertEqual(execution["status"], "returned")
        self.assertEqual(execution["dynamic_block_count"], 1)
        self.assertEqual(execution["dynamic_block_targets"], [f"0x{dynamic_va:08X}"])
        self.assertEqual(
            execution["state"]["registers"]["eax"],
            "0x00004455",
        )
        non_import_gaps = [
            gap for gap in summary["playability_gaps"] if gap["area"] != "imports"
        ]
        self.assertEqual(non_import_gaps, [])

    def test_playability_probe_reads_larger_dynamic_block_window(self) -> None:
        blob, layout = _synthetic_xbe()
        data = bytearray(blob)
        text_raw = 0x1000
        entry_va = layout["text_va"] + 0x20
        pointer_va = layout["text_va"] + 0x140
        dynamic_va = layout["text_va"] + 0x180
        entry_raw = text_raw + 0x20
        pointer_raw = text_raw + 0x140
        dynamic_raw = text_raw + 0x180
        data[entry_raw : entry_raw + 7] = _call_indirect_bytes(pointer_va)
        struct.pack_into("<I", data, pointer_raw, dynamic_va)
        long_dynamic_block = (b"\x90" * 0x40) + b"\xB8\x77\x66\x00\x00\xC3"
        data[dynamic_raw : dynamic_raw + len(long_dynamic_block)] = long_dynamic_block

        with tempfile.TemporaryDirectory() as temp_dir:
            xbe_path = Path(temp_dir) / "default.xbe"
            xbe_path.write_bytes(bytes(data))
            summary = build_playability_probe_summary(
                xbe_path,
                entry_bytes=0x20,
                max_instructions=8,
                max_block_instructions=80,
            )

        execution = summary["entry_recovery"]["execution"]
        self.assertEqual(execution["status"], "returned")
        self.assertEqual(execution["dynamic_block_count"], 1)
        self.assertEqual(execution["dynamic_block_targets"], [f"0x{dynamic_va:08X}"])
        self.assertEqual(
            execution["state"]["registers"]["eax"],
            "0x00006677",
        )

    def test_playability_probe_default_covers_title_sized_dynamic_block(self) -> None:
        block = (b"\x90" * 720) + b"\xC3"

        function = lift_x86_block(
            block,
            base_address=0x000E73B0,
            symbol="title_sized_dynamic_block",
            max_instructions=DEFAULT_MAX_BLOCK_INSTRUCTIONS,
        )

        self.assertEqual(function.instruction_count, 721)
        self.assertEqual(function.instructions[-1].mnemonic, "ret")

    def test_playability_probe_executes_created_guest_thread_start(self) -> None:
        blob, layout = _synthetic_xbe()
        data = bytearray(blob)
        text_raw = 0x1000
        entry_raw = text_raw + 0x20
        thread_va = layout["text_va"] + 0x180
        thread_raw = text_raw + 0x180
        handle_pointer = layout["data_va"] + 0x100
        thread_id_pointer = layout["data_va"] + 0x104
        entry = b"".join(
            [
                _push_u32(thread_va),
                _push_u32(0),
                _push_u32(0),
                _push_u32(0x33334444),
                _push_u32(0x11112222),
                _push_u32(thread_id_pointer),
                _push_u32(0x40),
                _push_u32(0x3000),
                _push_u32(0x20),
                _push_u32(handle_pointer),
                _call_indirect_bytes(layout["kernel_thunk_addr"]),
            ]
        )
        data[entry_raw : entry_raw + len(entry)] = entry
        data[thread_raw : thread_raw + 7] = _call_indirect_bytes(
            layout["kernel_thunk_addr"] + 4
        )
        struct.pack_into("<I", data, text_raw + 0x100, 0x800000FF)
        struct.pack_into("<I", data, text_raw + 0x104, 0x8000007F)
        struct.pack_into("<I", data, text_raw + 0x108, 0)

        with tempfile.TemporaryDirectory() as temp_dir:
            xbe_path = Path(temp_dir) / "default.xbe"
            xbe_path.write_bytes(bytes(data))
            summary = build_playability_probe_summary(
                xbe_path,
                entry_bytes=0x200,
                max_instructions=24,
                max_block_instructions=8,
            )

        execution = summary["entry_recovery"]["execution"]
        invocations = summary["runtime_abi_bridge"]["invocations"]
        self.assertEqual(execution["status"], "returned")
        self.assertEqual(execution["guest_thread_execution_count"], 1)
        self.assertEqual(execution["guest_thread_count"], 1)
        self.assertEqual(execution["guest_threads"][0]["start_address_hex"], f"0x{thread_va:08X}")
        self.assertEqual(execution["guest_thread_executions"][0]["status"], "returned")
        self.assertEqual(
            execution["guest_thread_executions"][0]["state"]["registers"]["eax"],
            "0x00989680",
        )
        self.assertEqual(
            [invocation["shim_name"] for invocation in invocations],
            ["PsCreateSystemThreadEx", "KeQueryPerformanceFrequency"],
        )
        self.assertEqual(invocations[0]["return_kind"], "status_dict")
        self.assertEqual(invocations[0]["eax_hex"], "0x00000000")
        self.assertEqual(
            [write["label"] for write in invocations[0]["memory_writes"]],
            ["thread_handle", "thread_id"],
        )
        non_import_gaps = [
            gap for gap in summary["playability_gaps"] if gap["area"] != "imports"
        ]
        self.assertEqual(non_import_gaps, [])

    def test_playability_probe_classifies_zero_guest_thread_return_as_completed(
        self,
    ) -> None:
        blob, layout = _synthetic_xbe()
        data = bytearray(blob)
        text_raw = 0x1000
        entry_raw = text_raw + 0x20
        thread_va = layout["text_va"] + 0x180
        thread_raw = text_raw + 0x180
        handle_pointer = layout["data_va"] + 0x100
        thread_id_pointer = layout["data_va"] + 0x104
        entry = b"".join(
            [
                _push_u32(thread_va),
                _push_u32(0),
                _push_u32(0),
                _push_u32(0),
                _push_u32(0),
                _push_u32(thread_id_pointer),
                _push_u32(0x40),
                _push_u32(0x3000),
                _push_u32(0x20),
                _push_u32(handle_pointer),
                _call_indirect_bytes(layout["kernel_thunk_addr"]),
            ]
        )
        data[entry_raw : entry_raw + len(entry)] = entry
        data[thread_raw : thread_raw + 4] = b"\x83\xC4\x04\xC3"
        struct.pack_into("<I", data, text_raw + 0x100, 0x800000FF)
        struct.pack_into("<I", data, text_raw + 0x104, 0)

        with tempfile.TemporaryDirectory() as temp_dir:
            xbe_path = Path(temp_dir) / "default.xbe"
            xbe_path.write_bytes(bytes(data))
            summary = build_playability_probe_summary(
                xbe_path,
                entry_bytes=0x200,
                max_instructions=24,
                max_block_instructions=8,
            )

        execution = summary["entry_recovery"]["execution"]
        thread_execution = execution["guest_thread_executions"][0]
        self.assertEqual(execution["guest_thread_execution_count"], 1)
        self.assertEqual(thread_execution["status"], "completed")
        self.assertEqual(thread_execution["return_address_hex"], "0x00000000")
        non_import_gaps = [
            gap for gap in summary["playability_gaps"] if gap["area"] != "imports"
        ]
        self.assertEqual(non_import_gaps, [])

    def test_playability_gaps_ignore_completed_guest_thread(self) -> None:
        gaps = _playability_gaps(
            {
                "status": "decoded",
                "execution": {
                    "status": "returned",
                    "guest_thread_executions": [
                        {
                            "status": "completed",
                            "thread_index": 0,
                            "start_address_hex": "0x000E682C",
                            "return_address_hex": "0x00000000",
                        }
                    ],
                    "dynamic_frontiers": [],
                },
                "frontier_batch": [],
            },
            [],
        )

        self.assertEqual(gaps, [])

    def test_playability_gaps_ignore_bounded_guest_thread(self) -> None:
        gaps = _playability_gaps(
            {
                "status": "decoded",
                "execution": {
                    "status": "returned",
                    "guest_thread_executions": [
                        {
                            "status": "step_budget",
                            "thread_index": 0,
                            "start_address_hex": "0x000E682C",
                            "return_address_hex": "0x00109B95",
                            "native_run": {"reason": "step_budget"},
                        }
                    ],
                    "dynamic_frontiers": [],
                },
                "frontier_batch": [],
            },
            [],
        )

        self.assertEqual(gaps, [])

    def test_live_guest_stop_is_terminal_and_not_a_playability_gap(self) -> None:
        thread_execution = {
            "status": "live_stop",
            "thread_index": 0,
            "start_address_hex": "0x000E682C",
            "return_address_hex": "0x00109B95",
            "native_run": {"reason": "yield_handler_stop"},
        }

        self.assertTrue(_guest_thread_requested_live_stop(thread_execution))
        self.assertFalse(
            _guest_thread_requested_live_stop(
                {"native_run": {"reason": "step_budget"}}
            )
        )
        self.assertEqual(
            _playability_gaps(
                {
                    "status": "decoded",
                    "execution": {
                        "status": "returned",
                        "guest_thread_executions": [thread_execution],
                        "dynamic_frontiers": [],
                    },
                    "frontier_batch": [],
                },
                [],
            ),
            [],
        )

    def test_playability_gaps_report_guest_thread_return_to_guest(self) -> None:
        gaps = _playability_gaps(
            {
                "status": "decoded",
                "execution": {
                    "status": "returned",
                    "guest_thread_executions": [
                        {
                            "status": "returned_to_guest",
                            "thread_index": 0,
                            "start_address_hex": "0x000E682C",
                            "return_address_hex": "0x1000A9A4",
                        }
                    ],
                    "dynamic_frontiers": [],
                },
                "frontier_batch": [],
            },
            [],
        )

        self.assertEqual(
            gaps,
            [
                {
                    "area": "control_flow",
                    "status": "thread_returned_to_guest",
                    "thread_index": 0,
                    "start_address_hex": "0x000E682C",
                    "return_address_hex": "0x1000A9A4",
                }
            ],
        )

    def test_title_render_loop_boundary_classifies_quad_submit_step_limit(self) -> None:
        state = CpuState.with_registers()
        state.eip = TITLE_QUAD_SUBMIT_ADDRESS + 0x10
        trace_events = [
            {
                "sequence": 0,
                "address": TITLE_QUAD_SUBMIT_ADDRESS,
                "address_hex": f"0x{TITLE_QUAD_SUBMIT_ADDRESS:08X}",
                "operation": "instruction",
                "details": {"text": "mov eax, [esp + 0x4]"},
            },
            *[
                {
                    "sequence": index + 1,
                    "address": TITLE_VERTEX_APPEND_ADDRESS,
                    "address_hex": f"0x{TITLE_VERTEX_APPEND_ADDRESS:08X}",
                    "operation": "title_vertex_append_fast_path",
                    "details": {
                        "object_address_hex": f"0x{TITLE_VERTEX_APPEND_OBJECT_ADDRESS:08X}",
                        "record_address_hex": f"0x{TITLE_VERTEX_APPEND_OBJECT_ADDRESS + index * TITLE_VERTEX_APPEND_STRIDE:08X}",
                        "vertex_index": index % 256,
                        "packed_color_hex": "0xFF0000FF",
                    },
                }
                for index in range(256)
            ],
            {
                "sequence": 300,
                "address": TITLE_QUAD_SUBMIT_END_ADDRESS,
                "address_hex": f"0x{TITLE_QUAD_SUBMIT_END_ADDRESS:08X}",
                "operation": "instruction",
                "details": {"text": "ret"},
            },
        ]

        boundary = _title_render_loop_boundary_from_step_limit(
            trace_events,
            state,
            1000000,
        )

        self.assertIsNotNone(boundary)
        assert boundary is not None
        self.assertEqual(boundary["status"], "render_boundary")
        self.assertEqual(boundary["boundary_kind"], "title_quad_submit_loop")
        summary = boundary["title_render_loop"]
        self.assertEqual(summary["vertex_append_invocation_count"], 256)
        self.assertEqual(summary["quad_submit_invocation_count"], 1)
        self.assertEqual(summary["max_vertex_index"], 255)

    def test_playability_gaps_report_guest_thread_render_boundary(self) -> None:
        gaps = _playability_gaps(
            {
                "status": "decoded",
                "execution": {
                    "status": "returned",
                    "guest_thread_executions": [
                        {
                            "status": "render_boundary",
                            "thread_index": 0,
                            "start_address_hex": "0x000E682C",
                            "boundary_kind": "title_quad_submit_loop",
                            "loop_entry_hex": f"0x{TITLE_QUAD_SUBMIT_ADDRESS:08X}",
                        }
                    ],
                    "dynamic_frontiers": [],
                },
                "frontier_batch": [],
            },
            [],
        )

        self.assertEqual(
            gaps,
            [
                {
                    "area": "control_flow",
                    "status": "thread_render_boundary",
                    "thread_index": 0,
                    "start_address_hex": "0x000E682C",
                    "boundary_kind": "title_quad_submit_loop",
                    "loop_entry_hex": f"0x{TITLE_QUAD_SUBMIT_ADDRESS:08X}",
                }
            ],
        )

    def test_runtime_abi_bridge_cleans_stdcall_guest_arguments(self) -> None:
        blob, layout = _synthetic_xbe()
        data = bytearray(blob)
        text_raw = 0x1000
        entry_va = layout["text_va"] + 0x20
        handoff_va = layout["text_va"] + 0x40
        entry_raw = text_raw + 0x20
        handoff_raw = text_raw + 0x40
        rel = handoff_va - (entry_va + 5)
        data[entry_raw : entry_raw + 6] = b"\xE8" + struct.pack("<i", rel) + b"\xC3"
        handoff = (b"\x6A\x00" * 10) + _call_indirect_bytes(
            layout["kernel_thunk_addr"]
        )
        data[handoff_raw : handoff_raw + len(handoff)] = handoff
        struct.pack_into("<I", data, text_raw + 0x100, 0x800000FF)
        struct.pack_into("<I", data, text_raw + 0x104, 0)

        with tempfile.TemporaryDirectory() as temp_dir:
            xbe_path = Path(temp_dir) / "default.xbe"
            xbe_path.write_bytes(bytes(data))
            summary = build_playability_probe_summary(
                xbe_path,
                entry_bytes=0x80,
                max_instructions=24,
            )

        invocation = summary["runtime_abi_bridge"]["invocations"][0]
        entry_execution = summary["entry_recovery"]["execution"]
        self.assertEqual(invocation["shim_name"], "PsCreateSystemThreadEx")
        self.assertEqual(invocation["stack_cleanup_bytes"], 40)
        self.assertEqual(entry_execution["status"], "returned")
        self.assertEqual(
            entry_execution["executed_internal_targets"],
            [f"0x{handoff_va:08X}"],
        )

    def test_native_branch_recovery_closes_bounded_direct_branch_gap(self) -> None:
        entry = lift_x86_block(
            b"\x76\x02",
            base_address=0x1000,
            symbol="entry",
        )
        requested: list[int] = []

        def load_block(target: int) -> LiftedFunction | None:
            requested.append(target)
            if target != 0x1004:
                return None
            return lift_x86_block(
                b"\x31\xC0\xC3",
                base_address=target,
                symbol="recovered",
            )

        recovered = _recover_missing_branch_targets(
            [entry],
            load_block,
            start_address=0x1000,
            end_address=0x1010,
        )

        self.assertEqual(requested, [0x1004])
        self.assertEqual([function.base_address for function in recovered], [0x1004])


    def test_live_host_bridge_publishes_render_and_forwards_controller_state(self) -> None:
        runtime = XboxRuntimeShims()
        watchpoint = RenderWriteWatchpoint()
        memory = SparseMemory()
        watchpoint.observe(0xFED00000, bytes.fromhex("01000000"))
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            render_path = root / "render.json"
            controller_path = root / "controller.json"
            controller_path.write_text(
                '{"ports":{"0":{"connected":true,"buttons":4096,"left_trigger":7}}}',
                encoding="utf-8",
            )
            bridge = LiveHostBridge(
                runtime,
                watchpoint,
                render_stream_path=render_path,
                controller_state_path=controller_path,
                render_publish_interval_seconds=0.0,
                render_publish_min_writes=2,
            )

            bridge.on_slice(CpuState(), memory, 100)
            self.assertFalse(render_path.exists())
            self.assertTrue(bridge.publish_render(memory, force=True))
            watchpoint.observe(0xFED00004, bytes.fromhex("02000000"))
            self.assertFalse(bridge.publish_render(memory))
            watchpoint.observe(0xFED00008, bytes.fromhex("03000000"))
            self.assertTrue(bridge.publish_render(memory, guest_steps=150))

            published = json.loads(render_path.read_text(encoding="utf-8"))
            self.assertEqual(published["write_count"], 3)
            command_path = Path(published["command_snapshot_path"])
            command_payload = command_path.read_bytes()
            self.assertEqual(command_payload[:8], b"B2APPND1")
            self.assertEqual((len(command_payload) - 8) // 16, 3)
            self.assertEqual(published["published_command_record_count"], 3)
            self.assertEqual(published["presentable_command_record_count"], 0)
            self.assertEqual(published["guest_flip_count"], 0)
            self.assertEqual(published["guest_steps"], 150)
            self.assertEqual(
                published["command_stream_generation"],
                bridge.command_stream_generation,
            )
            self.assertEqual(
                published["resource_stream_generation"],
                bridge.resource_stream_generation,
            )
            state = runtime.input.poll_controller(0)
            self.assertTrue(state.connected)
            self.assertEqual(state.buttons, 0x1000)
            self.assertEqual(state.left_trigger, 7)
            consumed = json.loads(
                bridge.controller_consumed_path.read_text(encoding="utf-8")
            )
            self.assertEqual(consumed["buttons"], 0x1000)
            self.assertEqual(consumed["format"], "b2-recomp-controller-consumed")
            self.assertEqual(bridge.summary()["render_publish_count"], 2)
            performance = bridge.summary()["performance"]
            self.assertGreaterEqual(
                performance["metrics"]["publish_render_total"]["count"],
                2,
            )
            self.assertEqual(
                performance["hot_paths"][0]["total_us"],
                max(
                    metric["total_us"]
                    for metric in performance["metrics"].values()
                ),
            )

            self.assertEqual(bridge.summary()["controller_update_count"], 1)

            controller_path.write_text(
                '{"ports":{"0":{"connected":true,"buttons":8192}}}',
                encoding="utf-8",
            )
            bridge.controller_mtime_ns = -1
            with patch.object(
                Path,
                "read_text",
                side_effect=PermissionError("controller snapshot is being replaced"),
            ):
                self.assertFalse(bridge.sample_controller())
            self.assertEqual(runtime.input.poll_controller(0).buttons, 0x1000)
            self.assertEqual(bridge.summary()["controller_read_defer_count"], 1)
            self.assertTrue(bridge.sample_controller())
            self.assertEqual(runtime.input.poll_controller(0).buttons, 0x2000)
            self.assertEqual(bridge.summary()["controller_update_count"], 2)

            controller_path.write_text('{"stop":true}', encoding="utf-8")
            bridge.controller_mtime_ns = -1
            self.assertFalse(bridge.on_slice(CpuState(), memory, 200))
            self.assertTrue(bridge.summary()["stop_requested"])

            for index in range(1300):
                watchpoint.observe(
                    0x80000000 + index * 4,
                    (index & 0xFFFFFFFF).to_bytes(4, "little"),
                )
            bridge.publish_render(memory, force=True)
            compact = json.loads(render_path.read_text(encoding="utf-8"))
            self.assertEqual(compact["captured_write_count"], 1303)
            compact_commands = Path(compact["command_snapshot_path"]).read_bytes()
            self.assertEqual(compact_commands[:8], b"B2APPND1")
            self.assertEqual((len(compact_commands) - 8) // 16, 1303)
            self.assertEqual(compact["published_command_record_count"], 1303)
            resource_path = Path(compact["resource_snapshot_path"])
            self.assertTrue(resource_path.is_file())
            self.assertEqual(resource_path.read_bytes()[:8], b"B2TEX001")

    def test_live_host_bridge_publishes_only_completed_flip_manifests(self) -> None:
        runtime = XboxRuntimeShims()
        watchpoint = RenderWriteWatchpoint()
        memory = SparseMemory()
        flip_header = (1 << 18) | 0x012C

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            render_path = root / "render.json"
            bridge = LiveHostBridge(
                runtime,
                watchpoint,
                render_stream_path=render_path,
                controller_state_path=root / "controller.json",
                render_publish_interval_seconds=0.0,
            )

            watchpoint.observe(0x80000000, flip_header.to_bytes(4, "little"))
            watchpoint.observe(0x80000004, (1).to_bytes(4, "little"))
            watchpoint.observe(0xFED00000, (1).to_bytes(4, "little"))
            bridge.on_slice(CpuState(), memory, 100)

            first = json.loads(render_path.read_text(encoding="utf-8"))
            self.assertEqual(first["guest_flip_count"], 1)
            self.assertEqual(first["presentable_command_record_count"], 3)
            self.assertEqual(bridge.summary()["render_publish_count"], 1)

            watchpoint.observe(0x80000008, (0xDEADBEEF).to_bytes(4, "little"))
            bridge.on_slice(CpuState(), memory, 200)
            unchanged = json.loads(render_path.read_text(encoding="utf-8"))
            self.assertEqual(unchanged, first)
            self.assertEqual(bridge.summary()["render_publish_count"], 1)
            self.assertEqual(bridge.published_command_record_count, 4)

            watchpoint.observe(0x8000000C, flip_header.to_bytes(4, "little"))
            watchpoint.observe(0x80000010, (2).to_bytes(4, "little"))
            watchpoint.observe(0xFED00004, (1).to_bytes(4, "little"))
            bridge.on_slice(CpuState(), memory, 300)

            second = json.loads(render_path.read_text(encoding="utf-8"))
            self.assertEqual(second["guest_flip_count"], 2)
            self.assertEqual(second["presentable_command_record_count"], 7)
            self.assertEqual(second["published_command_record_count"], 7)
            self.assertEqual(bridge.summary()["render_publish_count"], 2)

    def test_live_host_bridge_paces_completed_flips_without_catch_up_bursts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bridge = LiveHostBridge(
                XboxRuntimeShims(),
                RenderWriteWatchpoint(),
                render_stream_path=root / "render.json",
                controller_state_path=root / "controller.json",
            )
            bridge._next_video_frame_deadline_ns = 1_010_000_000
            with patch(
                "tools.playability.playability_probe.time.perf_counter_ns",
                side_effect=[1_000_000_000, 1_010_100_000, 1_010_200_000],
            ), patch(
                "tools.playability.playability_probe.time.sleep"
            ) as sleep:
                bridge._pace_completed_flip()

            sleep.assert_called_once_with(0.01)
            summary = bridge.summary()
            self.assertEqual(summary["video_pacing_target_hz"], 60)
            self.assertEqual(summary["video_pacing_sleep_count"], 1)
            self.assertEqual(summary["video_pacing_sleep_us"], 10_100)
            self.assertEqual(
                bridge._next_video_frame_deadline_ns,
                1_026_666_666,
            )

    def test_live_host_bridge_requests_native_yield_for_every_completed_flip(self) -> None:
        watchpoint = RenderWriteWatchpoint()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bridge = LiveHostBridge(
                XboxRuntimeShims(),
                watchpoint,
                render_stream_path=root / "render.json",
                controller_state_path=root / "controller.json",
            )
            self.assertFalse(bridge.should_yield_for_completed_flip())
            flip_header = (1 << 18) | 0x012C
            watchpoint.observe(0x80000000, flip_header.to_bytes(4, "little"))
            watchpoint.observe(0x80000004, (1).to_bytes(4, "little"))
            watchpoint.observe(0xFED00000, (1).to_bytes(4, "little"))
            self.assertTrue(bridge.should_yield_for_completed_flip())

    def test_live_host_bridge_waits_for_matching_presentation_ack(self) -> None:
        watchpoint = RenderWriteWatchpoint()
        memory = SparseMemory()
        flip_header = (1 << 18) | 0x012C
        watchpoint.observe(0x80000000, flip_header.to_bytes(4, "little"))
        watchpoint.observe(0x80000004, (1).to_bytes(4, "little"))
        watchpoint.observe(0xFED00000, (1).to_bytes(4, "little"))
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ack_path = root / "presented.bin"
            bridge = LiveHostBridge(
                XboxRuntimeShims(),
                watchpoint,
                render_stream_path=root / "render.json",
                controller_state_path=root / "controller.json",
                presentation_ack_path=ack_path,
            )
            ack_path.write_bytes(
                struct.pack(
                    "<8sQQ",
                    b"B2PRS001",
                    bridge.command_stream_generation,
                    1,
                )
            )

            self.assertTrue(bridge.on_slice(CpuState(), memory, 100))

            manifest = json.loads(
                bridge.render_stream_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                Path(manifest["presentation_ack_path"]),
                ack_path,
            )
            self.assertEqual(bridge.summary()["presentation_ack_wait_count"], 1)

    def test_live_host_bridge_advances_clock_at_flip_cadence(self) -> None:
        runtime = XboxRuntimeShims()
        resolver = ImportResolver()
        runtime.register_kernel_imports(resolver, imported_ordinals=[156])
        runtime_bridge = RuntimeAbiBridge(runtime)
        watchpoint = RenderWriteWatchpoint()
        flip_header = (1 << 18) | 0x012C
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            memory = SparseMemory()
            runtime_bridge.synchronize_data_exports(memory)
            tick_target = next(
                shim.target_address
                for shim in runtime.registered_shims
                if shim.name == "KeTickCount"
            )
            bridge = LiveHostBridge(
                runtime,
                watchpoint,
                render_stream_path=root / "render.json",
                controller_state_path=root / "controller.json",
                data_export_synchronizer=lambda synchronized_memory: (
                    runtime_bridge.synchronize_data_exports(
                        synchronized_memory,
                        volatile_only=True,
                    )
                ),
            )
            start = runtime.clock.query_interrupt_time()
            for index in range(3):
                watchpoint.observe(0x80000000 + index * 8, flip_header.to_bytes(4, "little"))
                watchpoint.observe(0x80000004 + index * 8, index.to_bytes(4, "little"))
            watchpoint.observe(0xFED00000, (1).to_bytes(4, "little"))

            bridge.on_slice(CpuState(), memory, 100)

            self.assertEqual(runtime.clock.query_interrupt_time() - start, 500_001)
            self.assertEqual(memory.read_u32(tick_target), 5)
            self.assertEqual(bridge.summary()["clock_advanced_flip_count"], 3)
            self.assertEqual(
                runtime_bridge.summary()["materialized_data_exports"][0]["name"],
                "KeTickCount",
            )

    def test_render_watchpoint_reports_each_submitted_flip_boundary(self) -> None:
        watchpoint = RenderWriteWatchpoint()
        flip_header = (1 << 18) | 0x012C

        watchpoint.observe(0x80000000, flip_header.to_bytes(4, "little"))
        watchpoint.observe(0x80000004, (9).to_bytes(4, "little"))
        self.assertFalse(watchpoint.has_pending_flip())
        watchpoint.observe(0xFED00000, (1).to_bytes(4, "little"))

        self.assertTrue(watchpoint.has_pending_flip())
        self.assertEqual(
            watchpoint.consume_pending_flip_boundaries(),
            [{"flip_index": 1, "write_count": 3, "flip_value": 9}],
        )
        self.assertFalse(watchpoint.has_pending_flip())

    def test_live_host_bridge_waits_for_lossless_flip_ack_before_ledger_commit(self) -> None:
        runtime = XboxRuntimeShims()
        watchpoint = RenderWriteWatchpoint()
        memory = SparseMemory()
        flip_header = (1 << 18) | 0x012C
        watchpoint.observe(0x80000000, flip_header.to_bytes(4, "little"))
        watchpoint.observe(0x80000004, (1).to_bytes(4, "little"))
        watchpoint.observe(0xFED00000, (1).to_bytes(4, "little"))

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            render_path = root / "render.json"
            controller_path = root / "controller.json"
            ack_path = root / "audit" / "ack.bin"
            bridge = LiveHostBridge(
                runtime,
                watchpoint,
                render_stream_path=render_path,
                controller_state_path=controller_path,
                render_publish_interval_seconds=0.0,
                flip_audit_ack_path=ack_path,
                flip_audit_timeout_seconds=2.0,
                flip_audit_max_flips=1,
            )

            def acknowledge() -> None:
                deadline = time.monotonic() + 2.0
                while not render_path.is_file():
                    if time.monotonic() >= deadline:
                        return
                    time.sleep(0.001)
                manifest = json.loads(render_path.read_text(encoding="utf-8"))
                ack_path.parent.mkdir(parents=True, exist_ok=True)
                ack_path.write_bytes(
                    struct.pack(
                        "<8sQI",
                        b"B2ACK001",
                        manifest["command_stream_generation"],
                        manifest["audit_flip_index"],
                    )
                )

            ack_thread = threading.Thread(target=acknowledge)
            ack_thread.start()
            self.assertFalse(bridge.on_slice(CpuState(), memory, 321))
            ack_thread.join(timeout=2.0)

            bridge.summary()
            ledger = bridge.flip_audit_ledger_path
            self.assertIsNotNone(ledger)
            records = [
                json.loads(line)
                for line in ledger.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(records[0]["audit_flip_index"], 1)
            self.assertEqual(records[0]["audit_guest_steps"], 321)
            self.assertEqual(bridge.summary()["audited_flip_count"], 1)
            self.assertTrue(bridge.summary()["stop_requested"])

    def test_flip_audit_selects_candidates_without_synchronizing_stable_flips(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bridge = LiveHostBridge(
                XboxRuntimeShims(),
                RenderWriteWatchpoint(),
                render_stream_path=root / "render.json",
                controller_state_path=root / "controller.json",
                flip_audit_ack_path=root / "audit" / "ack.bin",
                flip_audit_health_interval=30,
            )
            memory = SparseMemory()

            self.assertEqual(
                bridge._flip_audit_selection_reason(
                    {"flip_index": 1, "write_count": 100, "flip_value": 1},
                    memory,
                ),
                "first_flip",
            )
            bridge.audited_flip_count = 1
            self.assertEqual(
                bridge._flip_audit_selection_reason(
                    {"flip_index": 2, "write_count": 190, "flip_value": 1},
                    memory,
                ),
                "command_shape_changed",
            )
            bridge.audited_flip_count = 2
            self.assertIsNone(
                bridge._flip_audit_selection_reason(
                    {"flip_index": 3, "write_count": 280, "flip_value": 1},
                    memory,
                )
            )

if __name__ == "__main__":
    unittest.main()
