from __future__ import annotations

import ctypes
import json
import math
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
    _cooperative_wait_plan,
    _BoundedDiagnosticHistory,
    _read_text_file_shared,
    _read_dynamic_block_window,
    _resume_cooperative_wait,
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
    TITLE_D3D_CONTEXT_NV2A_BASE_OFFSET,
    TITLE_D3D_CONTEXT_SURFACE_STATE_ADDRESS,
    TITLE_D3D_CONTEXT_SYNTHETIC_ADDRESS,
    TITLE_D3D_FLUSH_ADDRESS,
    TITLE_D3D_PACKET_ALLOC_ADDRESS,
    TITLE_D3D_PACKET_ALLOC_SIZE,
    TITLE_D3D_PRIMITIVE_DRAW_ADDRESS,
    TITLE_D3D_PRIMITIVE_DRAW_STACK_CLEANUP,
    TITLE_D3D_INDEXED_DRAW_ADDRESS,
    TITLE_D3D_INDEXED_DRAW_CONTINUATION_ADDRESS,
    TITLE_D3D_INDEXED_STATE_PREPARE_ADDRESS,
    TITLE_D3D_INDEXED_DRAW_WRAPPER_ADDRESS,
    TITLE_D3D_TEXTURE_STATE_ADDRESS,
    TITLE_IMMEDIATE_DRAW_ADDRESS,
    TITLE_IMMEDIATE_DRAW_CONTINUATION_ADDRESS,
    TITLE_IMMEDIATE_DRAW_CORE_ADDRESS,
    TITLE_D3D_INDEXED_DRAW_WRAPPER_RETURN_ADDRESS,
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
    TITLE_WORLD_MATRIX_BUILD_ADDRESS,
    TITLE_WORLD_DRAW_CALLBACK_ADDRESS,
    TITLE_CAMERA_UPDATE_ADDRESS,
    TITLE_CAMERA_TARGET_FRAME_RETURN_ADDRESS,
    TITLE_CAMERA_UPDATE_RETURN_ADDRESS,
    TITLE_GENERIC_CAMERA_UPDATE_ADDRESS,
    TITLE_GENERIC_CAMERA_TARGET_FRAME_RETURN_ADDRESS,
    TITLE_GENERIC_CAMERA_UPDATE_RETURN_ADDRESSES,
    TITLE_WORLD_MATRIX_ROTATE_ADDRESS,
    TITLE_WORLD_MATRIX_ROTATE_RETURN_ADDRESSES,
    TITLE_WORLD_MATRIX_ROTATION_BUILD_ADDRESS,
    TITLE_WORLD_MATRIX_ROTATION_BUILD_RETURN_ADDRESS,
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
    TITLE_HEAP_LARGE_BIN_SENTINEL_OFFSET,
    TITLE_STATIC_DRIVE_ARRAY_ELEMENT_SIZE,
    TITLE_STATIC_DRIVE_ARRAY_REGISTRY_COUNT_ADDRESS,
    TITLE_STATIC_DRIVE_ARRAY_REGISTRY_DESCRIPTORS_ADDRESS,
    TITLE_STATIC_DRIVE_ARRAY_REGISTRY_OBJECTS_ADDRESS,
    TITLE_STATIC_DRIVE_ARRAY_SETUP_ADDRESS,
    TITLE_TEXT_DRAW_ADDRESS,
    TITLE_TEXT_DRAW_STACK_CLEANUP,
    TITLE_MATRIX_MULTIPLY_ADDRESS,
    TITLE_INDEXED_RESOURCE_DRAW_ADDRESS,
    TITLE_QUAD_BATCH_CONTINUATION_ADDRESS,
    TITLE_QUAD_CLIP_INTERPOLATE_ADDRESS,
    TITLE_QUAD_SUBMIT_ADDRESS,
    TITLE_QUAD_SUBMIT_END_ADDRESS,
    TITLE_SCENE_RECORD_DISTANCE_CULL_ADDRESS,
    TITLE_SCENE_RECORD_INDEXED_DRAW_ADDRESS,
    TITLE_SCENE_RECORD_RESOURCE_DRAW_ADDRESS,
    TITLE_VERTEX_APPEND_COMPACT_ADDRESS,
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
    TITLE_NV2A_MMIO_BASE_ADDRESS,
    TITLE_SPIN_DELAY_ADDRESS,
    TITLE_SPIN_DELAY_ITERATIONS,
    TITLE_STARTUP_WORK_QUEUE_LOOP_ENTRY,
    SCHEDULER_LOOP_CONVERGENCE_ERROR,
    SchedulerLoopConvergenceDetector,
    TitleAllocationListCountFastPath,
    TitleAssetStreamOpenFastPath,
    TitleGuestHeapFastPath,
    TitleHeapLargeBinIntegrityRepair,
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
    TitleSceneRecordAudit,
    TitleSceneRecordSourceLayout,
    TitleSceneRecordTableLayout,
    TitleWorldMatrixAudit,
    TitleDirectSoundBufferSyncFastPath,
    TitleGlobalListRegistrationFastPath,
    TitleStaticDriveArrayFastPath,
    TitleSpinDelayFastPath,
    TitleTextDrawFastPath,
    TitleVertexAppendFastPath,
    XbeBackedSparseMemory,
    _asset_io_summary_from_invocations,
    _append_native_frontier_module_batch,
    _NativeFrontierPromotion,
    _deterministic_service_validation_summary,
    _frontend_resource_boundary_from_step_limit,
    _gpu_idle_pump_boundary_from_step_limit,
    _guest_thread_requested_live_stop,
    _load_title_scene_record_sources,
    build_title_scene_record_audit_report,
    _heap_free_list_boundary_from_step_limit,
    _recover_missing_branch_targets,
    _playability_gaps,
    _recovered_render_command_stream,
    _scheduler_boundary_from_step_limit,
    _seed_guest_thread_fs_block,
    _title_native_fast_paths,
    _title_render_loop_boundary_from_step_limit,
    build_playability_probe_summary,
)
from tools.recomp.audit_x86_coverage import _dynamic_block_cache_seed_addresses
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
from tools.recomp.native_executor import NativeResumableExecutor


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
    def test_native_frontier_batches_keep_prior_module_stable(self) -> None:
        first = lift_x86_function(
            bytes.fromhex("40C3"),
            base_address=0x1000,
            symbol="frontier_first",
        )
        second = lift_x86_function(
            bytes.fromhex("48C3"),
            base_address=0x2000,
            symbol="frontier_second",
        )
        third = lift_x86_function(
            bytes.fromhex("90C3"),
            base_address=0x3000,
            symbol="frontier_third",
        )
        frontier_functions: list[LiftedFunction] = []
        frontier_modules: list[LiftedFunction] = []

        _append_native_frontier_module_batch(
            [first, second],
            frontier_functions=frontier_functions,
            frontier_modules=frontier_modules,
            symbol_prefix="guest_thread_00_frontier",
        )
        original_module = frontier_modules[0]
        original_addresses = [
            instruction.address for instruction in original_module.instructions
        ]
        _append_native_frontier_module_batch(
            [third],
            frontier_functions=frontier_functions,
            frontier_modules=frontier_modules,
            symbol_prefix="guest_thread_00_frontier",
        )

        self.assertEqual(len(frontier_functions), 3)
        self.assertEqual(len(frontier_modules), 2)
        self.assertIs(frontier_modules[0], original_module)
        self.assertEqual(
            [
                instruction.address
                for instruction in frontier_modules[0].instructions
            ],
            original_addresses,
        )
        self.assertEqual(
            [instruction.address for instruction in frontier_modules[1].instructions],
            [0x3000, 0x3001],
        )
        self.assertEqual(
            frontier_modules[1].symbol,
            "guest_thread_00_frontier_batch_0001",
        )

    def test_native_frontier_promotion_adopts_completed_background_build(self) -> None:
        frontier = lift_x86_function(
            bytes.fromhex("40C3"),
            base_address=0x2000,
            symbol="promoted_frontier",
        )

        class FakeExecutor:
            cache_summary = {
                "compile_wall_us": 1_000,
                "ahead_compiled_count": 1,
                "warm_hit_count": 2,
                "compile_worker_limit": 1,
                "low_priority_compilation": True,
                "executor_instance_id": "test-promotion",
            }

        promotion = _NativeFrontierPromotion(
            lambda _modules: FakeExecutor(),
            enabled=True,
            base_addresses={0x1000},
        )
        promotion.request([frontier])
        promotion.shutdown()

        self.assertIsNotNone(promotion.executor_for(0x1000))
        self.assertIsNotNone(promotion.executor_for(0x2000))
        summary = promotion.summary()
        self.assertEqual(summary["submission_count"], 1)
        self.assertEqual(summary["completion_count"], 1)
        self.assertEqual(summary["failure_count"], 0)
        self.assertEqual(summary["active_module_count"], 1)
        self.assertEqual(summary["active_address_count"], 3)
        self.assertEqual(summary["promoted_dispatch_count"], 2)
        self.assertTrue(summary["events"][0]["low_priority_compilation"])

    def test_live_stop_is_polled_between_native_frontier_recoveries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            controller_path = root / "controller.json"
            controller_path.write_text('{"stop":true}', encoding="utf-8")
            bridge = LiveHostBridge(
                XboxRuntimeShims(),
                RenderWriteWatchpoint(),
                render_stream_path=root / "render.json",
                controller_state_path=controller_path,
            )

            self.assertFalse(bridge.should_continue_native_recovery())
            self.assertTrue(bridge.stop_requested)

    def test_native_guest_loop_executes_recovered_vertex_append_directly(self) -> None:
        source = Path("tools/playability/playability_probe.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("title_vertex_append_handlers = (", source)
        self.assertIn("if native_guest_loop", source)
        self.assertIn("else title_vertex_append_fast_path.call_handlers()", source)
        self.assertIn("**title_vertex_append_handlers,", source)
        self.assertNotIn(
            "**title_vertex_append_fast_path.call_handlers(),",
            source,
        )
        self.assertIn("default=100_000", source)
        self.assertIn("completed flips still", source)
        self.assertIn("memory_write_observer_yield_header=(", source)
        self.assertIn("TITLE_D3D_FLIP_METHOD_HEADER", source)

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

    def test_title_xinput_fast_path_marshals_black_and_white_buttons(self) -> None:
        payload = TitleXInputFastPath._xbox_gamepad_payload(
            ControllerState(connected=True, buttons=0x0300)
        )

        self.assertEqual(payload[6:8], bytes((255, 255)))

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

    def test_render_watchpoint_normalizes_dynamic_title_push_buffer(self) -> None:
        watchpoint = RenderWriteWatchpoint()
        watchpoint.set_push_buffer_range_provider(
            lambda: (0x21B80000, 0x21BC0000)
        )
        flip_header = (1 << 18) | 0x012C

        watchpoint.observe(0x21B90100, flip_header.to_bytes(4, "little"))
        watchpoint.observe(0x21B90104, (7).to_bytes(4, "little"))
        watchpoint.observe(0xFED00000, (1).to_bytes(4, "little"))

        stream = watchpoint.to_stream()
        self.assertEqual(stream["dynamic_push_buffer_range"]["base_hex"], "0x21B80000")
        self.assertEqual(stream["writes"][0]["address_hex"], "0x80010100")
        self.assertEqual(stream["writes"][0]["guest_address_hex"], "0x21B90100")
        self.assertEqual(
            watchpoint.consume_pending_flip_boundaries(),
            [{"flip_index": 1, "write_count": 3, "flip_value": 7}],
        )

    def test_live_render_watchpoint_can_skip_expensive_diagnostic_records(self) -> None:
        watchpoint = RenderWriteWatchpoint(retain_diagnostic_writes=False)
        watchpoint.set_push_buffer_range_provider(
            lambda: (0x21B80000, 0x21BC0000)
        )
        flip_header = (1 << 18) | 0x012C

        watchpoint.observe(0x21B90100, flip_header.to_bytes(4, "little"))
        watchpoint.observe(0x21B90104, (7).to_bytes(4, "little"))
        watchpoint.observe(0xFED00000, (1).to_bytes(4, "little"))

        stream = watchpoint.to_stream()
        self.assertFalse(stream["diagnostic_write_retention"])
        self.assertEqual(stream["write_count"], 3)
        self.assertEqual(stream["captured_write_count"], 0)
        self.assertEqual(stream["writes"], [])
        self.assertEqual(len(watchpoint.live_command_records), 3)
        self.assertEqual(
            watchpoint.consume_pending_flip_boundaries(),
            [{"flip_index": 1, "write_count": 3, "flip_value": 7}],
        )

    def test_live_render_watchpoint_batches_native_push_buffer_writes(self) -> None:
        watchpoint = RenderWriteWatchpoint(retain_diagnostic_writes=False)
        watchpoint.set_push_buffer_range_provider(
            lambda: (0x21B80000, 0x21BC0000)
        )
        flip_header = (1 << 18) | 0x012C

        addresses = (ctypes.c_uint32 * 2)(0x21B90100, 0x21B90104)
        values = (ctypes.c_uint32 * 2)(flip_header, 7)
        sizes = (ctypes.c_uint8 * 2)(4, 4)
        eips = (ctypes.c_uint32 * 2)(0x1000, 0x1004)
        sources = (ctypes.c_uint32 * 2)(0x2000, 0x2004)
        steps = (ctypes.c_uint64 * 2)(10, 11)
        packed = (ctypes.c_uint8 * 32).from_buffer_copy(
            struct.pack(
                "<BBHIII",
                1,
                4,
                0,
                0x80010100,
                flip_header,
                0,
            )
            + struct.pack(
                "<BBHIII",
                1,
                4,
                0,
                0x80010104,
                7,
                0,
            )
        )
        watchpoint.observe_native_write_batch(
            packed,
            addresses,
            values,
            sizes,
            eips,
            sources,
            steps,
            2,
            0x21B80000,
            True,
        )
        watchpoint.observe(0xFED00000, (1).to_bytes(4, "little"))

        self.assertEqual(watchpoint.write_count, 3)
        self.assertEqual(len(watchpoint.live_command_records), 3)
        self.assertEqual(
            [
                struct.unpack("<BBHI8s", record)[3]
                for record in watchpoint.live_command_records
            ],
            [0x80010100, 0x80010104, 0xFED00000],
        )
        self.assertEqual(
            watchpoint.consume_pending_flip_boundaries(),
            [{"flip_index": 1, "write_count": 3, "flip_value": 7}],
        )
        batch = watchpoint.live_epoch_stream()["native_write_batch"]
        self.assertEqual(batch["batch_count"], 1)
        self.assertEqual(batch["write_count"], 2)
        self.assertEqual(batch["received_batch_count"], 1)
        self.assertEqual(batch["contiguous_batch_count"], 1)
        self.assertEqual(
            set(batch["phase_timings"]),
            {"command_forward", "texture_update"},
        )
        self.assertEqual(batch["phase_timings"]["texture_update"]["count"], 1)
        self.assertEqual(batch["phase_timings"]["command_forward"]["count"], 1)

    def test_live_bridge_forwards_packed_commands_without_provenance_rings(self) -> None:
        watchpoint = RenderWriteWatchpoint(retain_diagnostic_writes=False)
        range_start = 0x21B80000
        watchpoint.set_push_buffer_range_provider(
            lambda: (range_start, range_start + 0x40000)
        )
        addresses = (ctypes.c_uint32 * 2)(range_start + 0x100, range_start + 0x104)
        values = (ctypes.c_uint32 * 2)(0x12345678, 0x9ABCDEF0)
        sizes = (ctypes.c_uint8 * 2)(4, 4)
        provenance = (ctypes.c_uint32 * 2)(0x1111, 0x2222)
        steps = (ctypes.c_uint64 * 2)(10, 11)
        packed_bytes = b"".join(
            struct.pack(
                "<BBHIII",
                1,
                4,
                0,
                0x80000100 + index * 4,
                value,
                0,
            )
            for index, value in enumerate(values)
        )
        packed = (ctypes.c_uint8 * len(packed_bytes)).from_buffer_copy(packed_bytes)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bridge = LiveHostBridge(
                XboxRuntimeShims(),
                watchpoint,
                render_stream_path=root / "render.json",
                controller_state_path=root / "controller.json",
                presentation_ack_path=root / "presented.bin",
                render_publish_interval_seconds=0.0,
            )
            watchpoint.observe_native_write_batch(
                packed,
                addresses,
                values,
                sizes,
                provenance,
                provenance,
                steps,
                2,
                range_start,
                True,
            )

            self.assertTrue(bridge._flush_render_commands(force=True))
            span_stream = bridge.render_command_path.read_bytes()
            self.assertEqual(span_stream[:8], b"B2SPAN01")
            self.assertEqual(
                struct.unpack_from("<BBHIII", span_stream, 8),
                (1, 0, 0, 0x80000100, 4, 1),
            )
            self.assertEqual(span_stream[24:28], (0x12345678).to_bytes(4, "little"))
            self.assertEqual(
                struct.unpack_from("<BBHIII", span_stream, 28),
                (1, 0, 0, 0x80000104, 4, 1),
            )
            self.assertEqual(span_stream[44:48], (0x9ABCDEF0).to_bytes(4, "little"))
            self.assertEqual(len(watchpoint.live_command_records), 0)
            self.assertEqual(len(watchpoint.live_command_eips), 0)
            self.assertEqual(
                watchpoint.to_stream()["transform_constant_upload_provenance"][
                    "status"
                ],
                "disabled_for_direct_transport",
            )
            summary = bridge.summary()
            self.assertTrue(summary["direct_command_transport"])
            self.assertEqual(summary["direct_command_receive_batch_count"], 1)
            self.assertEqual(summary["direct_command_receive_record_count"], 2)
            self.assertEqual(summary["direct_command_receive_span_count"], 2)
            self.assertEqual(summary["direct_command_receive_payload_byte_count"], 8)

    def test_live_bridge_publishes_native_spans_at_exact_flip_boundary(self) -> None:
        watchpoint = RenderWriteWatchpoint(retain_diagnostic_writes=False)
        range_start = 0x21B80000
        range_end = range_start + 0x40000
        flip_header = (1 << 18) | 0x012C
        payload_bytes = struct.pack("<III", flip_header, 7, 0xDEADBEEF)
        payload = (ctypes.c_uint8 * len(payload_bytes)).from_buffer_copy(payload_bytes)
        addresses = (ctypes.c_uint32 * 2)(range_start, range_start + 8)
        offsets = (ctypes.c_uint32 * 2)(0, 8)
        sizes = (ctypes.c_uint32 * 2)(8, 4)
        write_counts = (ctypes.c_uint32 * 2)(2, 1)
        flags = (ctypes.c_uint8 * 2)(1, 0)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            render_path = root / "render.json"
            bridge = LiveHostBridge(
                XboxRuntimeShims(),
                watchpoint,
                render_stream_path=render_path,
                controller_state_path=root / "controller.json",
                presentation_ack_path=root / "presented.bin",
                render_publish_interval_seconds=0.0,
            )
            watchpoint.observe_native_write_span_batch(
                payload,
                len(payload_bytes),
                addresses,
                offsets,
                sizes,
                write_counts,
                flags,
                2,
                range_start,
                range_end,
                range_start,
            )

            with patch.object(
                bridge,
                "_wait_for_presentation_ack",
                return_value=True,
            ):
                bridge.on_slice(CpuState(), SparseMemory(), 100)

            manifest = json.loads(render_path.read_text(encoding="utf-8"))
            command_bytes = Path(manifest["command_snapshot_path"]).read_bytes()
            self.assertEqual(command_bytes[:8], b"B2SPAN01")
            self.assertEqual(manifest["command_transport_format"], "bulk_span_v1")
            self.assertEqual(manifest["command_snapshot_record_count"], 3)
            self.assertEqual(manifest["presentable_command_record_count"], 2)
            self.assertEqual(manifest["command_snapshot_span_count"], 2)
            self.assertEqual(manifest["presentable_command_span_count"], 1)
            self.assertEqual(manifest["command_snapshot_byte_count"], 44)
            self.assertEqual(manifest["presentable_command_byte_count"], 24)
            summary = bridge.summary()
            self.assertEqual(summary["direct_command_receive_record_count"], 3)
            self.assertEqual(summary["direct_command_receive_span_count"], 2)
            self.assertEqual(summary["direct_command_receive_payload_byte_count"], 12)

    def test_native_span_resource_scan_bypasses_payload_reassembly(self) -> None:
        watchpoint = RenderWriteWatchpoint(retain_diagnostic_writes=False)
        watchpoint.set_direct_command_span_sink(lambda *_args: None)
        range_start = 0x21B80000
        range_end = range_start + 0x40000
        vertex_address = 0x22010000
        texture_address = 0x01234000
        texture_format = 0x022F0600
        texture_rect = (64 << 16) | 32
        scan_calls: list[int] = []

        def scan_spans(*args):
            scan_calls.append(int(args[1]))
            return (
                [
                    0xFFF00000,
                    0xFFF00010,
                    0xFFF00020,
                    0xFFF00100,
                    0xFFF00104,
                ],
                [
                    texture_address,
                    texture_format,
                    texture_rect,
                    vertex_address,
                    vertex_address + 10 * 12,
                ],
                [1, 1, 1, 1, 1],
                5,
                521,
                9,
                512,
                4,
            )

        watchpoint.set_native_resource_span_scanner(
            scan_spans,
            lambda: ([], [], [], 0, 0, 0, 0, 0),
        )
        flip_header = (1 << 18) | 0x012C
        payload_bytes = struct.pack("<III", 0, flip_header, 7)
        payload = (ctypes.c_uint8 * len(payload_bytes)).from_buffer_copy(
            payload_bytes
        )
        addresses = (ctypes.c_uint32 * 2)(range_start, range_start + 4)
        offsets = (ctypes.c_uint32 * 2)(0, 4)
        sizes = (ctypes.c_uint32 * 2)(4, 8)
        write_counts = (ctypes.c_uint32 * 2)(1, 2)
        flags = (ctypes.c_uint8 * 2)(0, 1)

        watchpoint.observe_native_write_span_batch(
            payload,
            len(payload_bytes),
            addresses,
            offsets,
            sizes,
            write_counts,
            flags,
            2,
            range_start,
            range_end,
            range_start,
        )

        self.assertEqual(scan_calls, [len(payload_bytes)])
        self.assertFalse(watchpoint._texture_pending)
        self.assertEqual(
            watchpoint.resource_binding_stream()["vertex_buffer_ranges"],
            [[vertex_address, vertex_address + 10 * 12]],
        )
        self.assertEqual(
            watchpoint.resource_binding_stream()["texture_bindings"],
            [[0, texture_address, texture_format, texture_rect]],
        )
        self.assertEqual(
            watchpoint.consume_pending_flip_boundaries(),
            [{"flip_index": 1, "write_count": 3, "flip_value": 7}],
        )
        summary = watchpoint.live_epoch_stream()["native_write_batch"]
        self.assertEqual(summary["resource_method_native_span_batch_count"], 1)
        self.assertEqual(summary["resource_method_native_span_event_count"], 5)
        self.assertEqual(summary["resource_method_native_binding_count"], 1)
        self.assertEqual(summary["resource_method_native_range_count"], 1)
        self.assertEqual(
            summary["resource_method_payload_rescan_bypass_byte_count"],
            len(payload_bytes),
        )

    def test_live_render_watchpoint_uses_packed_general_batch_records(self) -> None:
        watchpoint = RenderWriteWatchpoint(retain_diagnostic_writes=False)
        range_start = 0x21B80000
        range_end = range_start + 0x40000
        watchpoint.set_push_buffer_range_provider(
            lambda: (range_start, range_end)
        )
        flip_header = (1 << 18) | 0x012C
        raw_addresses = [
            range_start + 0x100,
            range_start + 0x104,
            range_start + 0x200,
            range_start + 0x201,
        ]
        raw_values = [flip_header, 7, 0xAA, 0xBB]
        raw_sizes = [4, 4, 1, 1]
        count = len(raw_addresses)
        addresses = (ctypes.c_uint32 * count)(*raw_addresses)
        values = (ctypes.c_uint32 * count)(*raw_values)
        sizes = (ctypes.c_uint8 * count)(*raw_sizes)
        eips = (ctypes.c_uint32 * count)(0x1000, 0x1004, 0x1008, 0x100C)
        sources = (ctypes.c_uint32 * count)(0x2000, 0x2004, 0x2008, 0x200C)
        steps = (ctypes.c_uint64 * count)(10, 11, 12, 13)
        packed = bytearray(count * 16)
        for index, (address, value, size) in enumerate(
            zip(raw_addresses, raw_values, raw_sizes)
        ):
            struct.pack_into(
                "<BBHIII",
                packed,
                index * 16,
                1,
                size,
                0,
                0x80000000 + address - range_start,
                value,
                0,
            )
        packed_records = (ctypes.c_uint8 * len(packed)).from_buffer_copy(packed)
        texture_payload_bytes = (
            flip_header.to_bytes(4, "little")
            + (7).to_bytes(4, "little")
            + b"\xAA\xBB"
        )
        texture_payload = (
            ctypes.c_uint8 * len(texture_payload_bytes)
        ).from_buffer_copy(texture_payload_bytes)
        texture_runs = (ctypes.c_uint32 * 8)(
            0x80000100,
            0,
            8,
            0,
            0x80000200,
            8,
            2,
            2,
        )

        watchpoint.observe_native_write_batch(
            packed_records,
            addresses,
            values,
            sizes,
            eips,
            sources,
            steps,
            count,
            range_start,
            False,
            range_end,
            texture_payload,
            texture_runs,
            2,
        )
        self.assertEqual(watchpoint.live_command_records.tail_bytes(count), bytes(packed))
        self.assertEqual(watchpoint.live_command_eips.tail_bytes(count), bytes(eips))
        self.assertEqual(
            watchpoint.live_command_sources.tail_bytes(count),
            bytes(sources),
        )
        self.assertEqual(watchpoint.live_command_steps.tail_bytes(count), bytes(steps))
        watchpoint.observe(0xFED00000, (1).to_bytes(4, "little"))

        self.assertEqual(
            watchpoint.consume_pending_flip_boundaries(),
            [{"flip_index": 1, "write_count": 2, "flip_value": 7}],
        )
        batch = watchpoint.live_epoch_stream()["native_write_batch"]
        self.assertEqual(batch["batch_count"], 1)
        self.assertEqual(batch["general_batch_count"], 1)
        self.assertEqual(batch["direct_packed_general_batch_count"], 1)
        self.assertEqual(batch["texture_run_count"], 2)
        self.assertEqual(batch["texture_packed_payload_run_count"], 2)
        self.assertEqual(
            set(batch["phase_timings"]),
            {"command_forward", "texture_update"},
        )
        self.assertEqual(batch["phase_timings"]["texture_update"]["count"], 1)
        self.assertEqual(batch["phase_timings"]["command_forward"]["count"], 1)

    def test_render_watchpoint_attributes_near_zero_position_matrix_upload(self) -> None:
        watchpoint = RenderWriteWatchpoint(retain_diagnostic_writes=False)
        range_start = 0x21B80000
        watchpoint.set_push_buffer_range_provider(
            lambda: (range_start, range_start + 0x40000)
        )
        matrix = (
            0.0001,
            0.0,
            0.0,
            -2.0,
            0.0,
            0.0002,
            0.0,
            3.0,
            0.0,
            0.0,
            0.0003,
            55.0,
            0.0,
            0.0,
            0.0003,
            56.0,
        )
        words = [
            (1 << 18) | 0x1EA4,
            96,
            (16 << 18) | 0x0B80,
            *struct.unpack("<16I", struct.pack("<16f", *matrix)),
            (1 << 18) | 0x012C,
            1,
        ]
        count = len(words)
        guest_start = range_start + 0x100
        addresses = (ctypes.c_uint32 * count)(
            *(guest_start + index * 4 for index in range(count))
        )
        values = (ctypes.c_uint32 * count)(*words)
        sizes = (ctypes.c_uint8 * count)(*([4] * count))
        eips = (ctypes.c_uint32 * count)(
            *(0x2222 if 3 <= index < 19 else 0x1111 for index in range(count))
        )
        sources = (ctypes.c_uint32 * count)(
            *(0x5000 + index * 4 for index in range(count))
        )
        steps = (ctypes.c_uint64 * count)(*(100 + index for index in range(count)))
        packed = bytearray(count * 16)
        for index, word in enumerate(words):
            struct.pack_into(
                "<BBHIII",
                packed,
                index * 16,
                1,
                4,
                0,
                0x80000100 + index * 4,
                word,
                0,
            )
        packed_records = (ctypes.c_uint8 * len(packed)).from_buffer_copy(packed)

        watchpoint.observe_native_write_batch(
            packed_records,
            addresses,
            values,
            sizes,
            eips,
            sources,
            steps,
            count,
            range_start,
            True,
        )

        provenance = watchpoint.to_stream()[
            "transform_constant_upload_provenance"
        ]
        self.assertEqual(provenance["status"], "captured")
        self.assertEqual(provenance["matrix_upload_count"], 1)
        self.assertEqual(provenance["near_zero_basis_matrix_upload_count"], 1)
        self.assertEqual(
            provenance["near_zero_producer_instruction_counts"],
            [{"instruction_address_hex": "0x00002222", "matrix_upload_count": 1}],
        )
        matrix_upload = provenance["near_zero_matrix_uploads"][0]
        self.assertEqual(matrix_upload["next_guest_flip_count"], 1)
        self.assertEqual(
            matrix_upload["position_c96_c99_max_abs_basis"],
            0.0003,
        )

    def test_render_watchpoint_preserves_full_dynamic_push_buffer_offsets(self) -> None:
        watchpoint = RenderWriteWatchpoint()
        watchpoint.set_push_buffer_range_provider(
            lambda: (0x21B80000, 0x21D80000)
        )

        watchpoint.observe(0x21B80020, (1).to_bytes(4, "little"))
        watchpoint.observe(0x21B90020, (2).to_bytes(4, "little"))

        stream = watchpoint.to_stream()
        self.assertEqual(
            [write["address_hex"] for write in stream["writes"]],
            ["0x80000020", "0x80010020"],
        )
        self.assertEqual(
            [
                struct.unpack("<BBHI8s", record)[3]
                for record in watchpoint.live_command_records
            ],
            [0x80000020, 0x80010020],
        )

    def test_live_command_record_ring_retains_newest_records_in_order(self) -> None:
        watchpoint = RenderWriteWatchpoint(max_writes=2)

        watchpoint.observe(0x80000000, (1).to_bytes(4, "little"))
        watchpoint.observe(0x80000004, (2).to_bytes(4, "little"))
        watchpoint.observe(0x80000008, (3).to_bytes(4, "little"))

        self.assertEqual(
            [
                struct.unpack("<BBHI8s", record)[3]
                for record in watchpoint.live_command_records
            ],
            [0x80000004, 0x80000008],
        )
        self.assertEqual(
            len(watchpoint.live_command_records.to_bytes()),
            2 * 16,
        )

    def test_render_watchpoint_rejects_large_non_command_copy_before_chunking(self) -> None:
        class CountingWatchpoint(RenderWriteWatchpoint):
            normalization_count = 0

            def _normalize_dynamic_push_buffer_address(self, address: int):
                self.normalization_count += 1
                return super()._normalize_dynamic_push_buffer_address(address)

        watchpoint = CountingWatchpoint()
        watchpoint.set_push_buffer_range_provider(
            lambda: (0x21B80000, 0x21BC0000)
        )

        watchpoint.observe(0x10000000, bytes(1024 * 1024))

        self.assertEqual(watchpoint.normalization_count, 0)
        self.assertEqual(watchpoint.write_count, 0)

    def test_render_watchpoint_retains_relevant_span_of_large_dynamic_copy(self) -> None:
        watchpoint = RenderWriteWatchpoint()
        watchpoint.set_push_buffer_range_provider(
            lambda: (0x21B80000, 0x21BC0000)
        )
        payload = bytes(range(32))

        watchpoint.observe(0x21B7FFF8, payload)

        writes = watchpoint.to_stream()["writes"]
        self.assertEqual(len(writes), 3)
        self.assertEqual(writes[0]["guest_address_hex"], "0x21B80000")
        self.assertEqual(writes[-1]["guest_address_hex"], "0x21B80010")

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
        self.assertEqual(bindings, [(0, texture_address, texture_format, 0)])

        payload = bytes.fromhex("00F8E00700000000")
        memory = SparseMemory()
        memory.write(texture_address, payload)
        stream = _snapshot_render_texture_resources(
            watchpoint.to_stream(), watchpoint.history_stream(), memory
        )
        self.assertEqual(stream["resource_snapshot_count"], 1)
        self.assertEqual(stream["resource_snapshots"][0]["format"], "DXT1")
        self.assertEqual(stream["resource_snapshots"][0]["bytes_hex"], payload.hex().upper())

    def test_texture_snapshot_retains_declared_compressed_mip_chain(self) -> None:
        texture_address = 0x22001000
        texture_format = (
            (3 << 24) | (3 << 20) | (3 << 16) | (0x0F << 8) | (2 << 4)
        )
        payload = bytes(range(96))
        memory = SparseMemory({texture_address: payload})

        stream = _snapshot_render_texture_resources(
            {},
            {
                "texture_bindings": [
                    [0, texture_address, texture_format, 0],
                ]
            },
            memory,
        )

        resource = stream["resource_snapshots"][0]
        self.assertEqual(resource["format"], "DXT5")
        self.assertEqual(resource["mipmap_levels"], 3)
        self.assertEqual(resource["byte_count"], 64 + 16 + 16)
        self.assertEqual(resource["bytes_hex"], payload.hex().upper())

    def test_indexed_draw_snapshot_retains_vertex_buffer_bytes(self) -> None:
        watchpoint = RenderWriteWatchpoint()
        vertex_address = 0x22010000
        vertex_format = (12 << 8) | (3 << 4) | 2
        words = (
            (1 << 18) | 0x1720,
            vertex_address,
            (1 << 18) | 0x1760,
            vertex_format,
            (1 << 18) | 0x17FC,
            6,
            (1 << 18) | 0x1800,
            (2 << 16) | 0,
            (1 << 18) | 0x17FC,
            0,
        )
        watchpoint.observe(0x80000000, struct.pack(f"<{len(words)}I", *words))
        payload = bytes(range(36))
        memory = SparseMemory({vertex_address: payload})

        history = watchpoint.resource_binding_stream()
        stream = _snapshot_render_texture_resources(
            watchpoint.to_stream(),
            history,
            memory,
        )

        self.assertEqual(
            history["vertex_buffer_ranges"],
            [[vertex_address, vertex_address + len(payload)]],
        )
        self.assertEqual(stream["resource_snapshot_count"], 1)
        resource = stream["resource_snapshots"][0]
        self.assertEqual(resource["format"], "VERTEX_BUFFER")
        self.assertEqual(resource["bytes_hex"], payload.hex().upper())

    def test_resource_scan_filters_methods_and_aggregates_index_packets(self) -> None:
        watchpoint = RenderWriteWatchpoint(retain_diagnostic_writes=False)
        vertex_address = 0x22010000
        vertex_format = (12 << 8) | (3 << 4) | 2
        irrelevant_count = 512
        indexed_words = (
            (2 << 16) | 0,
            (7 << 16) | 3,
            (4 << 16) | 9,
            (8 << 16) | 1,
        )
        words = (
            (irrelevant_count << 18) | 0x0400,
            *([0] * irrelevant_count),
            (1 << 18) | 0x1720,
            vertex_address,
            (1 << 18) | 0x1760,
            vertex_format,
            (1 << 18) | 0x17FC,
            6,
            0x40000000 | (len(indexed_words) << 18) | 0x1800,
            *indexed_words,
            (1 << 18) | 0x17FC,
            0,
            (1 << 18) | 0x012C,
            1,
        )
        watchpoint._texture_pending = bytearray(
            struct.pack(f"<{len(words)}I", *words)
        )
        watchpoint._texture_pending_end = 0x80000000 + len(words) * 4

        watchpoint._flush_texture_writes(boundary_write_count=len(words))

        self.assertEqual(
            watchpoint.resource_binding_stream()["vertex_buffer_ranges"],
            [[vertex_address, vertex_address + 10 * 12]],
        )
        self.assertEqual(
            watchpoint.consume_pending_flip_boundaries(),
            [{"flip_index": 1, "write_count": len(words), "flip_value": 1}],
        )
        summary = watchpoint.live_epoch_stream()["native_write_batch"]
        self.assertEqual(summary["resource_method_data_word_count"], 521)
        self.assertEqual(summary["resource_method_tracked_word_count"], 9)
        self.assertEqual(summary["resource_method_skipped_word_count"], 512)
        self.assertEqual(
            summary["resource_method_aggregated_index_word_count"],
            len(indexed_words),
        )
    def test_resource_scan_applies_native_compacted_methods_in_order(self) -> None:
        watchpoint = RenderWriteWatchpoint(retain_diagnostic_writes=False)
        vertex_address = 0x22010000
        vertex_format = (12 << 8) | (3 << 4) | 2
        watchpoint.set_native_resource_method_scanner(
            lambda _payload, _byte_size: (
                [0x1720, 0x1760, 0x17FC, 0x1808, 0x17FC, 0x012C],
                [vertex_address, vertex_format, 6, 9, 0, 1],
                6,
                521,
                9,
                512,
                4,
            )
        )
        watchpoint._texture_pending = bytearray(4)
        watchpoint._texture_pending_end = 0x80000004

        watchpoint._flush_texture_writes(boundary_write_count=530)

        self.assertEqual(
            watchpoint.resource_binding_stream()["vertex_buffer_ranges"],
            [[vertex_address, vertex_address + 10 * 12]],
        )
        self.assertEqual(
            watchpoint.consume_pending_flip_boundaries(),
            [{"flip_index": 1, "write_count": 530, "flip_value": 1}],
        )
        summary = watchpoint.live_epoch_stream()["native_write_batch"]
        self.assertEqual(summary["resource_method_data_word_count"], 521)
        self.assertEqual(summary["resource_method_tracked_word_count"], 9)
        self.assertEqual(summary["resource_method_skipped_word_count"], 512)
        self.assertEqual(summary["resource_method_native_scan_count"], 1)
        self.assertEqual(
            summary["resource_method_native_compacted_method_count"],
            6,
        )

    def test_vertex_snapshot_resolves_xbox_cpu_direct_map(self) -> None:
        watchpoint = RenderWriteWatchpoint()
        vertex_address = 0x03380000
        cpu_direct_address = vertex_address | 0x80000000
        vertex_format = (12 << 8) | (3 << 4) | 2
        words = (
            (1 << 18) | 0x1720,
            vertex_address,
            (1 << 18) | 0x1760,
            vertex_format,
            (1 << 18) | 0x17FC,
            6,
            (1 << 18) | 0x1800,
            (2 << 16) | 0,
            (1 << 18) | 0x17FC,
            0,
        )
        watchpoint.observe(0x80000000, struct.pack(f"<{len(words)}I", *words))
        payload = bytes(range(36))
        memory = SparseMemory({cpu_direct_address: payload})

        stream = _snapshot_render_texture_resources(
            watchpoint.to_stream(),
            watchpoint.resource_binding_stream(),
            memory,
        )

        resource = stream["resource_snapshots"][0]
        self.assertEqual(resource["address"], vertex_address)
        self.assertEqual(resource["source_address"], cpu_direct_address)
        self.assertEqual(resource["bytes_hex"], payload.hex().upper())

    def test_texture_snapshot_preserves_distinct_formats_at_reused_address(self) -> None:
        texture_address = 0x2272A080
        dxt1_format = (9 << 24) | (9 << 20) | (1 << 16) | (0x0C << 8) | (2 << 4)
        dxt5_format = (8 << 24) | (8 << 20) | (1 << 16) | (0x0F << 8) | (2 << 4)
        memory = SparseMemory()
        memory.write(texture_address, bytes(range(256)) * 512)

        stream = _snapshot_render_texture_resources(
            {},
            {
                "texture_bindings": [
                    [0, texture_address, dxt1_format, 0],
                    [0, texture_address, dxt5_format, 0],
                ]
            },
            memory,
        )

        self.assertEqual(stream["resource_snapshot_count"], 2)
        self.assertEqual(
            {
                (resource["format"], resource["width"], resource["height"])
                for resource in stream["resource_snapshots"]
            },
            {("DXT1", 512, 512), ("DXT5", 256, 256)},
        )

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
                (0, background_address, background_format, 0),
                (0, logo_address, logo_format, 0),
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
        self.assertEqual(
            history["texture_bindings"],
            [[0, texture_address, texture_format, 0]],
        )
        self.assertEqual(stream["resource_snapshot_count"], 1)
        self.assertEqual(stream["resource_snapshots"][0]["format"], "DXT1")

    def test_linear_texture_snapshot_uses_image_rect_extent(self) -> None:
        watchpoint = RenderWriteWatchpoint()
        texture_address = 0x01D94000
        texture_format = 0x00011229
        image_rect = (640 << 16) | 480
        words = (
            (2 << 18) | 0x1B00,
            texture_address,
            texture_format,
            (1 << 18) | 0x1B18,
            image_rect,
            (1 << 18) | 0x17FC,
            8,
        )
        watchpoint.observe(0x80000000, struct.pack(f"<{len(words)}I", *words))

        history = watchpoint.resource_binding_stream()
        stream = _snapshot_render_texture_resources(
            watchpoint.to_stream(),
            history,
            SparseMemory(),
        )

        self.assertEqual(
            history["texture_bindings"],
            [[0, texture_address, texture_format, image_rect]],
        )
        self.assertEqual(stream["resource_snapshot_count"], 1)
        resource = stream["resource_snapshots"][0]
        self.assertEqual(resource["format"], "A8R8G8B8_LINEAR")
        self.assertEqual((resource["width"], resource["height"]), (640, 480))
        self.assertEqual(resource["byte_count"], 640 * 480 * 4)
        self.assertEqual(resource["nonzero_byte_count"], 0)

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
        self.assertTrue(memory.native_cache_physical_aliases)
        self.assertEqual(memory.native_cache_address(cpu_alias), physical_address)
        self.assertTrue(memory.native_cacheable_address(0x83380000))
        self.assertFalse(memory.native_cacheable_address(0x80000000))
        self.assertFalse(memory.native_cacheable_address(0x84000000))

        function = lift_x86_function(
            b"\xA1" + struct.pack("<I", cpu_alias) + b"\xC3",
            base_address=0x1000,
            symbol="native_physical_alias_read",
        )
        memory.write_u32(0x8000, 0)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
            )
            first_state = CpuState.with_registers(esp=0x8000)
            self.assertEqual(executor.run(first_state, memory, max_steps=10), 0)
            second_state = CpuState.with_registers(esp=0x8000)
            self.assertEqual(executor.run(second_state, memory, max_steps=10), 0)

        self.assertEqual(first_state.get_register("eax"), 0x55667788)
        self.assertEqual(second_state.get_register("eax"), 0x55667788)
        self.assertEqual(
            executor.last_run_summary["performance"]["page_cache_fill_count"],
            0,
        )

    def test_xbe_memory_declares_exact_callback_dependencies(self) -> None:
        memory = XbeBackedSparseMemory(load_xbe_bytes(_synthetic_xbe()[0]))

        self.assertEqual(
            memory.native_memory_callback_dependency_addresses(
                TITLE_D3D_CONTEXT_GET_POINTER_ADDRESS,
                4,
                is_write=False,
            ),
            (TITLE_D3D_CONTEXT_SYNTHETIC_ADDRESS + 0x2C,),
        )
        self.assertEqual(
            memory.native_memory_callback_dependency_addresses(
                TITLE_GPU_COMMAND_KICK_ADDRESS,
                4,
                is_write=True,
            ),
            (
                TITLE_GPU_SUBMISSION_BASE_ADDRESS
                + TITLE_GPU_COMPLETION_DMA_POINTER_OFFSET,
            ),
        )
        self.assertEqual(
            memory.native_memory_callback_dependency_addresses(
                TITLE_AUDIO_DSP_STATUS_ADDRESS,
                4,
                is_write=False,
            ),
            (),
        )
        self.assertIsNone(
            memory.native_memory_callback_dependency_addresses(
                0x00123450,
                4,
                is_write=False,
            )
        )
        self.assertFalse(
            memory.native_memory_callback_requires_observer_drain(
                TITLE_D3D_CONTEXT_GLOBAL_ADDRESS,
                4,
                is_write=False,
            )
        )
        self.assertFalse(
            memory.native_memory_callback_requires_observer_drain(
                TITLE_GPU_SUBMISSION_BASE_ADDRESS,
                4,
                is_write=False,
            )
        )
        self.assertTrue(
            memory.native_memory_callback_requires_observer_drain(
                TITLE_AUDIO_DSP_STATUS_ADDRESS,
                4,
                is_write=False,
            )
        )
        self.assertTrue(
            memory.native_memory_callback_requires_observer_drain(
                TITLE_D3D_CONTEXT_GLOBAL_ADDRESS,
                4,
                is_write=True,
            )
        )

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

    def test_native_handlers_read_shared_page_view_without_full_copyback(self) -> None:
        address = 0x22080080
        handler_target = 0x2000
        base_address = 0x1000
        code = bytearray(b"\xC7\x05" + struct.pack("<I", address))
        code.extend(struct.pack("<I", 0x12345678))
        code.extend(
            b"\xE8"
            + struct.pack(
                "<i",
                handler_target - (base_address + len(code) + 5),
            )
        )
        code.extend(b"\xC3")
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_shared_page_handler_read",
        )
        memory = XbeBackedSparseMemory(
            load_xbe_bytes(_synthetic_xbe()[0]),
            initial={0x8000: 0},
        )
        observed: list[int] = []

        def handler(
            _state: CpuState,
            observed_memory: SparseMemory,
            _target: int,
            _trace: ExecutionTrace,
        ) -> None:
            observed.append(observed_memory.read_u32(address))
            observed_memory.write_u32(address + 4, 0xAABBCCDD)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                callback_addresses={handler_target},
            )
            returned_to = executor.run(
                CpuState.with_registers(esp=0x8000),
                memory,
                call_handlers={handler_target: handler},
                dispatch_host_calls_in_native=True,
                max_steps=8,
            )
            shared_performance = executor.last_run_summary["performance"]
            fallback_memory = XbeBackedSparseMemory(
                load_xbe_bytes(_synthetic_xbe()[0]),
                initial={0x8000: 0},
            )
            fallback_returned_to = executor.run(
                CpuState.with_registers(esp=0x8000),
                fallback_memory,
                call_handlers={handler_target: handler},
                shared_memory_handler_predicate=lambda _target: False,
                max_steps=8,
            )
            fallback_performance = executor.last_run_summary["performance"]

        self.assertEqual(returned_to, 0)
        self.assertEqual(fallback_returned_to, 0)
        self.assertEqual(observed, [0x12345678, 0x12345678])
        self.assertEqual(memory.read_u32(address), 0x12345678)
        self.assertEqual(memory.read_u32(address + 4), 0xAABBCCDD)
        self.assertEqual(fallback_memory.read_u32(address), 0x12345678)
        self.assertTrue(shared_performance["shared_memory_view_enabled"])
        self.assertTrue(shared_performance["native_host_call_dispatch_enabled"])
        self.assertEqual(
            shared_performance["native_host_call_dispatch_count"],
            1,
        )
        self.assertEqual(
            shared_performance["shared_memory_handler_sync_bypass_count"],
            1,
        )
        self.assertEqual(
            fallback_performance["shared_memory_handler_sync_bypass_count"],
            0,
        )
        self.assertEqual(
            fallback_performance["shared_memory_handler_sync_fallback_count"],
            1,
        )
        self.assertEqual(
            memory.native_page_cache_view_summary()["host_write_commit_count"],
            1,
        )

    def test_native_slice_yields_keep_dirty_memory_resident_until_exit(self) -> None:
        address = 0x22080080
        base_address = 0x1000
        code = bytearray(b"\xA3" + struct.pack("<I", address))
        code.extend(b"\x40\xEB\xF8")  # inc eax; repeat the store
        function = lift_x86_function(
            bytes(code),
            base_address=base_address,
            symbol="native_resident_dirty_slices",
        )
        memory = XbeBackedSparseMemory(
            load_xbe_bytes(_synthetic_xbe()[0]),
            initial={0x8000: 0},
        )
        observed: list[tuple[int, int, int]] = []

        def on_slice(
            _state: CpuState,
            observed_memory: SparseMemory,
            steps: int,
        ) -> None:
            observed.append(
                (
                    steps,
                    observed_memory.read_u32(address),
                    observed_memory.visible_page_generation(address),
                )
            )
            self.assertEqual(observed_memory.page_generation(address), 0)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(function, build_dir=Path(temp_dir))
            executor.run(
                CpuState.with_registers(eax=1, esp=0x8000),
                memory,
                max_steps=12,
                slice_steps=4,
                yield_handler=on_slice,
                defer_dirty_sync_at_yield=True,
            )

        self.assertEqual([record[0] for record in observed], [4, 8, 12])
        self.assertEqual([record[1] for record in observed], [2, 3, 4])
        visible_generations = [record[2] for record in observed]
        self.assertTrue(
            all(
                earlier < later
                for earlier, later in zip(
                    visible_generations,
                    visible_generations[1:],
                )
            )
        )
        self.assertEqual(memory.read_u32(address), 4)
        self.assertEqual(memory.page_generation(address), 1)
        performance = executor.last_run_summary["performance"]
        self.assertTrue(performance["deferred_dirty_sync_at_yield"])
        self.assertEqual(
            performance["shared_memory_yield_sync_bypass_count"],
            3,
        )
        self.assertEqual(performance["dirty_page_writeback_count"], 1)

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
        self.assertEqual(
            memory.read_u32(TITLE_ASSET_STREAM_SYNTHETIC_OBJECT_ADDRESS + 0x14),
            0,
        )
        self.assertEqual(
            memory.read_u32(TITLE_ASSET_STREAM_SYNTHETIC_OBJECT_ADDRESS + 0x18),
            0,
        )
        self.assertEqual(
            memory.read_u32(TITLE_ASSET_STREAM_SYNTHETIC_OBJECT_ADDRESS + 0x2C),
            2,
        )
        summary = fast_path.summary()
        self.assertEqual(summary["successful_open_count"], 1)
        self.assertEqual(summary["recent_invocations"][0]["guest_path"], "D:\\audio\\special.rws")
        status_stack = 0x8080
        memory.write_u32(status_stack, 0xFEEDFACE)
        status_state = CpuState.with_registers(
            ecx=TITLE_ASSET_STREAM_SYNTHETIC_OBJECT_ADDRESS,
            esp=status_stack,
        )
        fast_path.status_handler(
            status_state,
            memory,
            fast_path.status_target,
            ExecutionTrace(),
        )
        self.assertEqual(status_state.get_register("eax"), 1)
        self.assertEqual(
            memory.read_u32(TITLE_ASSET_STREAM_SYNTHETIC_OBJECT_ADDRESS + 0x2C),
            1,
        )
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
        self.assertEqual(
            memory.read_u32(TITLE_ASSET_STREAM_SYNTHETIC_OBJECT_ADDRESS + 0x18),
            9,
        )
        self.assertEqual(
            memory.read_u32(TITLE_ASSET_STREAM_SYNTHETIC_OBJECT_ADDRESS + 0x1C),
            0,
        )
        seek_stack = 0x8200
        memory.write_u32(seek_stack + 4, 0)
        memory.write_u32(seek_stack + 8, 0)
        memory.write_u32(seek_stack + 0xC, 2)
        seek_state = CpuState.with_registers(
            ecx=TITLE_ASSET_STREAM_SYNTHETIC_OBJECT_ADDRESS,
            esp=seek_stack,
        )
        fast_path.seek_handler(
            seek_state,
            memory,
            fast_path.seek_target,
            ExecutionTrace(),
        )
        self.assertEqual(seek_state.get_register("eax"), len(b"recovered-asset"))
        self.assertEqual(
            memory.read_u32(TITLE_ASSET_STREAM_SYNTHETIC_OBJECT_ADDRESS + 0x18),
            len(b"recovered-asset"),
        )
        self.assertTrue(fast_path.summary()["stream_states"][0]["complete"])
        self.assertEqual(fast_path.summary()["read_count"], 1)
        self.assertEqual(fast_path.summary()["ready_transition_count"], 1)

    def test_title_asset_stream_publishes_track_pss_prelinked_image(self) -> None:
        runtime = XboxRuntimeShims()
        fast_path = TitleAssetStreamOpenFastPath(runtime)
        object_address = TITLE_ASSET_STREAM_SYNTHETIC_OBJECT_ADDRESS
        image_base = 0x8381C000
        table_a_address = image_base + 0x200
        table_b_address = image_base + 0x220
        scene_record_address = image_base + 0x240
        scene_index_address = image_base + 0x2F0
        descriptors = (
            (0x00000000, 0x0003C000),
            (0x0003C000, 0x0003C000),
            (0x00078000, 0x00012345),
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
        struct.pack_into("<II", payload, 0x24C, scene_index_address, 42)
        fast_path._states[object_address] = {
            "payload": bytes(payload),
            "position": 0,
            "title_path": "trackpss/forward/fwyl.pss",
            "status": 2,
            "terminal": False,
        }
        stack = 0x8100
        destination = 0x9300
        memory = SparseMemory(
            {
                object_address + 0x2C: 2,
                stack + 4: destination,
                stack + 8: len(payload),
                # One table is already present. Publication should repair only
                # the missing prelinked target while retaining exact bytes.
                table_b_address: descriptor_payload,
            }
        )
        state = CpuState.with_registers(ecx=object_address, esp=stack)
        trace = ExecutionTrace()

        fast_path.read_handler(
            state,
            memory,
            TITLE_ASSET_STREAM_SYNTHETIC_READ_TARGET_ADDRESS,
            trace,
        )

        self.assertEqual(state.get_register("eax"), len(payload))
        self.assertEqual(memory.read(destination, len(payload)), bytes(payload))
        self.assertEqual(
            memory.read(table_a_address, len(descriptor_payload)),
            descriptor_payload,
        )
        self.assertEqual(
            memory.read(table_b_address, len(descriptor_payload)),
            descriptor_payload,
        )
        self.assertEqual(memory.read(image_base, len(payload)), bytes(payload))
        self.assertEqual(
            memory.read(scene_record_address + 0x0C, 8),
            struct.pack("<II", scene_index_address, 42),
        )
        summary = fast_path.summary()
        self.assertEqual(summary["track_descriptor_publication_count"], 1)
        self.assertEqual(summary["track_descriptor_table_count"], 2)
        self.assertEqual(summary["track_descriptor_table_repair_count"], 1)
        self.assertEqual(summary["track_descriptor_entry_count"], 6)
        self.assertEqual(summary["track_descriptor_validation_failure_count"], 0)
        self.assertEqual(summary["track_prelinked_image_publication_count"], 1)
        self.assertEqual(summary["track_prelinked_image_repair_count"], 1)
        self.assertEqual(summary["track_prelinked_image_bytes"], len(payload))
        self.assertEqual(summary["track_scene_record_table_count"], 1)
        self.assertEqual(summary["track_scene_record_table_repair_count"], 1)
        self.assertEqual(summary["track_scene_record_entry_count"], 1)
        stream = summary["stream_states"][0]
        self.assertTrue(stream["track_descriptor_publication_attempted"])
        self.assertTrue(stream["track_descriptor_tables_published"])
        self.assertTrue(stream["track_prelinked_image_published"])
        self.assertEqual(
            stream["track_descriptor_publication"]["descriptor_count"],
            len(descriptors),
        )
        self.assertTrue(
            stream["track_descriptor_publication"]["prelinked_image_repaired"]
        )
        self.assertEqual(
            stream["track_descriptor_publication"]["scene_record_count"], 1
        )
        self.assertIn(
            "title_track_descriptor_publication",
            {event["operation"] for event in trace.to_list()},
        )

    def test_title_asset_stream_rejects_invalid_track_pss_descriptor_table(self) -> None:
        runtime = XboxRuntimeShims()
        fast_path = TitleAssetStreamOpenFastPath(runtime)
        object_address = TITLE_ASSET_STREAM_SYNTHETIC_OBJECT_ADDRESS
        image_base = 0x8381C000
        invalid_table_address = image_base + 0x400
        payload = bytearray(0x68)
        struct.pack_into("<I", payload, 0x14, image_base + 0x1C0)
        struct.pack_into("<I", payload, 0x5C, 1)
        struct.pack_into("<I", payload, 0x60, invalid_table_address)
        struct.pack_into("<I", payload, 0x64, invalid_table_address)
        fast_path._states[object_address] = {
            "payload": bytes(payload),
            "position": 0,
            "title_path": "trackpss/reverse/fwyl.pss",
            "status": 2,
            "terminal": False,
        }
        stack = 0x8100
        memory = SparseMemory(
            {
                object_address + 0x2C: 2,
                stack + 4: 0x9300,
                stack + 8: len(payload),
            }
        )
        state = CpuState.with_registers(ecx=object_address, esp=stack)
        trace = ExecutionTrace()

        fast_path.read_handler(
            state,
            memory,
            TITLE_ASSET_STREAM_SYNTHETIC_READ_TARGET_ADDRESS,
            trace,
        )

        summary = fast_path.summary()
        self.assertEqual(summary["track_descriptor_publication_count"], 0)
        self.assertEqual(summary["track_descriptor_table_repair_count"], 0)
        self.assertEqual(summary["track_descriptor_validation_failure_count"], 1)
        stream = summary["stream_states"][0]
        self.assertTrue(stream["track_descriptor_publication_attempted"])
        self.assertFalse(stream["track_descriptor_tables_published"])
        self.assertIn("outside its payload", stream["track_descriptor_validation_error"])
        self.assertEqual(memory.read(invalid_table_address, 8), b"\x00" * 8)
        self.assertIn(
            "title_track_descriptor_publication_rejected",
            {event["operation"] for event in trace.to_list()},
        )

    def test_title_asset_stream_records_terminal_and_repeated_eof_reads(self) -> None:
        runtime = XboxRuntimeShims()
        fast_path = TitleAssetStreamOpenFastPath(runtime)
        object_address = TITLE_ASSET_STREAM_SYNTHETIC_OBJECT_ADDRESS
        fast_path._states[object_address] = {
            "payload": b"abc",
            "position": 0,
            "title_path": "racecars/driveed/driveede.rws",
            "status": 2,
            "read_count": 0,
            "requested_bytes": 0,
            "bytes_read": 0,
            "short_read_count": 0,
            "eof_read_count": 0,
            "terminal_read_count": 0,
            "terminal": False,
        }
        memory = SparseMemory(
            {
                object_address + 0x2C: 2,
                0x8104: 0x9300,
                0x8108: 5,
                0x8204: 0x9400,
                0x8208: 5,
            }
        )

        first = CpuState.with_registers(ecx=object_address, esp=0x8100)
        fast_path.read_handler(
            first,
            memory,
            TITLE_ASSET_STREAM_SYNTHETIC_READ_TARGET_ADDRESS,
            ExecutionTrace(),
        )
        second = CpuState.with_registers(ecx=object_address, esp=0x8200)
        fast_path.read_handler(
            second,
            memory,
            TITLE_ASSET_STREAM_SYNTHETIC_READ_TARGET_ADDRESS,
            ExecutionTrace(),
        )

        self.assertEqual(first.get_register("eax"), 3)
        self.assertEqual(second.get_register("eax"), 0)
        self.assertEqual(memory.read(0x9300, 3), b"abc")
        self.assertEqual(memory.read_u32(object_address + 0x2C), 1)
        summary = fast_path.summary()
        self.assertEqual(summary["short_read_count"], 2)
        self.assertEqual(summary["eof_read_count"], 1)
        self.assertEqual(summary["terminal_read_count"], 2)
        self.assertEqual(summary["ready_transition_count"], 1)
        stream = summary["stream_states"][0]
        self.assertEqual(stream["status"], 1)
        self.assertEqual(stream["read_count"], 2)
        self.assertEqual(stream["requested_bytes"], 10)
        self.assertEqual(stream["bytes_read"], 3)
        self.assertEqual(stream["short_read_count"], 2)
        self.assertEqual(stream["eof_read_count"], 1)
        self.assertEqual(stream["terminal_read_count"], 2)
        self.assertEqual(
            stream["terminal_transition_read_count"],
            1,
        )
        self.assertEqual(
            [
                (read["requested"], read["bytes_read"], read["eof_read"])
                for read in summary["recent_reads"]
            ],
            [(5, 3, False), (5, 0, True)],
        )

    def test_title_asset_stream_seek_publishes_ready_mode(self) -> None:
        runtime = XboxRuntimeShims()
        fast_path = TitleAssetStreamOpenFastPath(runtime)
        object_address = TITLE_ASSET_STREAM_SYNTHETIC_OBJECT_ADDRESS
        fast_path._states[object_address] = {
            "payload": b"abc",
            "position": 0,
            "status": 2,
            "terminal": False,
        }
        stack = 0x8100
        memory = SparseMemory(
            {
                object_address + 0x2C: 2,
                stack + 4: 3,
                stack + 8: 0,
                stack + 0xC: 0,
            }
        )
        state = CpuState.with_registers(ecx=object_address, esp=stack)

        fast_path.seek_handler(
            state,
            memory,
            fast_path.seek_target,
            ExecutionTrace(),
        )

        self.assertEqual(state.get_register("eax"), 3)
        self.assertEqual(memory.read_u32(object_address + 0x18), 3)
        self.assertEqual(memory.read_u32(object_address + 0x2C), 1)
        self.assertEqual(fast_path.summary()["ready_transition_count"], 1)
        self.assertTrue(fast_path.summary()["stream_states"][0]["terminal"])

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

    def test_xbe_u32_fast_path_matches_general_read_semantics(self) -> None:
        class DummyArena:
            def read(self, _address: int, size: int) -> bytes:
                return bytes(size)

        class DummyLoaded:
            arena = DummyArena()

        initial = {
            TITLE_GPU_PROGRESS_COUNTER_ADDRESS: 3,
            TITLE_GPU_SOFTWARE_COMPLETION_FLAG_ADDRESS: (
                TITLE_GPU_SOFTWARE_COMPLETION_PENDING_BIT | 0x24440000
            ),
            TITLE_AUDIO_DSP_CONTROL_ADDRESS: TITLE_AUDIO_DSP_RESET_REQUEST_BIT,
        }
        fast = XbeBackedSparseMemory(  # type: ignore[arg-type]
            DummyLoaded(),
            initial,
            enable_title_sentinel_fallbacks=True,
        )
        general = fast.clone_for_speculative_execution()
        addresses = (
            0x1000,
            TITLE_D3D_CONTEXT_GLOBAL_ADDRESS,
            TITLE_D3D_CONTEXT_GET_POINTER_ADDRESS,
            TITLE_GPU_SUBMISSION_BASE_ADDRESS,
            TITLE_GPU_SUBMISSION_LIMIT_ADDRESS,
            TITLE_GPU_COMPLETION_REGISTER_ADDRESS,
            TITLE_D3D_CONTEXT_DMA_STATE_ADDRESS
            + TITLE_GPU_COMPLETION_DMA_STATUS_OFFSET,
            TITLE_GPU_PFIFO_RUNOUT_STATUS_ADDRESS,
            TITLE_GPU_PFIFO_CACHE1_STATUS_ADDRESS,
            TITLE_GPU_PROGRESS_COUNTER_ADDRESS,
            TITLE_GPU_SOFTWARE_COMPLETION_FLAG_ADDRESS,
            TITLE_MCPX_FRAME_COUNTER_ADDRESS,
            TITLE_AUDIO_DSP_STATUS_ADDRESS,
            TITLE_CLEANUP_LIST_SENTINEL_ADDRESS,
            TITLE_CLEANUP_LIST_SENTINEL_ADDRESS + 4,
            TITLE_FRONTEND_RESOURCE_CACHE_SENTINEL_ADDRESS,
            TITLE_FRONTEND_REGISTRY_LIST_SENTINEL_ADDRESS,
            TITLE_FRONTEND_REGISTRY_LIST_SENTINEL_ADDRESS + 4,
            TITLE_FRONTEND_INITIALIZER_LIST_SENTINEL_ADDRESS,
        )

        for address in addresses:
            with self.subTest(address=f"0x{address:08X}"):
                fast_value = fast.read_u32(address)
                general_value = struct.unpack("<I", general.read(address, 4))[0]
                self.assertEqual(fast_value, general_value)
                self.assertEqual(
                    fast.title_hardware_completion_summary(),
                    general.title_hardware_completion_summary(),
                )

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
                + TITLE_D3D_CONTEXT_NV2A_BASE_OFFSET
            ),
            TITLE_NV2A_MMIO_BASE_ADDRESS,
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
            _push_u32(2)
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

    def test_title_d3d_packet_alloc_flush_advances_get_completion(
        self,
    ) -> None:
        base_address = 0x2180
        function = lift_x86_function(
            _push_u32(0)
            + _call_relative_bytes(base_address + 5, TITLE_D3D_PACKET_ALLOC_ADDRESS)
            + b"\xC3",
            base_address=base_address,
            symbol="title_d3d_packet_alloc_flush_call",
        )
        context_address = 0x3000
        dma_state_address = 0x5000
        get_pointer_address = 0x6000
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
                context_address + 0x2C: 9,
                context_address + 0x30: get_pointer_address,
                context_address + 0x17F4: dma_state_address,
                get_pointer_address: 3,
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
        self.assertEqual(result.state.get_register("eax"), 9)
        self.assertEqual(memory.read_u32(context_address + 0x2C), 11)
        self.assertEqual(memory.read_u32(get_pointer_address), 9)
        self.assertEqual(memory.read_u32(dma_state_address + 0x40), 9)
        self.assertEqual(fast_path.summary()["flush_requested_count"], 1)

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

    def test_title_native_d3d_allocator_paths_use_bulk_command_spans(self) -> None:
        functions = [
            lift_x86_function(
                b"\xC3",
                base_address=address,
                symbol=f"native_d3d_allocator_{address:08x}",
            )
            for address in (
                TITLE_D3D_PACKET_ALLOC_ADDRESS,
                TITLE_D3D_RESERVE_ADDRESS,
            )
        ]
        instructions = tuple(
            instruction
            for function in functions
            for instruction in function.instructions
        )
        function = LiftedFunction(
            symbol="title_native_d3d_allocator_paths",
            base_address=TITLE_D3D_PACKET_ALLOC_ADDRESS,
            code_size=max(item.next_address for item in instructions)
            - TITLE_D3D_PACKET_ALLOC_ADDRESS,
            instructions=instructions,
        )
        context_address = 0x400000
        dma_state_address = 0x410000
        get_pointer_address = 0x420000
        stack_address = 0x700000
        ring_base = 0x21000000
        ring_end = ring_base + TITLE_D3D_PUSH_BUFFER_SIZE
        packet_address = ring_base + 0x80
        direct_spans: list[
            tuple[bytes, list[tuple[int, int, int, int, int]]]
        ] = []

        def observe_direct_spans(
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
            direct_spans.append(
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
                module_functions=functions,
                native_fast_paths=_title_native_fast_paths(),
            )
            allocation_state = CpuState.with_registers(esp=stack_address)
            allocation_state.eip = TITLE_D3D_PACKET_ALLOC_ADDRESS
            allocation_memory = SparseMemory(
                {
                    TITLE_D3D_CONTEXT_GLOBAL_ADDRESS: context_address,
                    context_address: packet_address,
                    context_address + 4: ring_end,
                    context_address + 0x24: ring_base,
                    context_address + 0x28: ring_end,
                    context_address + 0x2C: 0x20,
                    context_address + 0x30: get_pointer_address,
                    context_address + 0x17F4: dma_state_address,
                    stack_address: 0,
                    stack_address + 4: 2,
                }
            )
            self.assertEqual(
                executor.run(
                    allocation_state,
                    allocation_memory,
                    memory_write_span_observer=observe_direct_spans,
                    memory_write_batch_address_base=(
                        TITLE_D3D_PUSH_BUFFER_BASE_ADDRESS
                    ),
                    memory_write_observer_ranges_provider=lambda: (
                        (ring_base, ring_end),
                    ),
                    capture_observed_write_provenance=False,
                    direct_observed_write_transport=True,
                    max_steps=4,
                ),
                0,
            )
            allocation_performance = executor.last_run_summary["performance"]

            reserve_packet = ring_base + 0x7ED8
            reserve_state = CpuState.with_registers(esp=stack_address)
            reserve_state.eip = TITLE_D3D_RESERVE_ADDRESS
            reserve_memory = SparseMemory(
                {
                    TITLE_D3D_CONTEXT_GLOBAL_ADDRESS: context_address,
                    context_address: reserve_packet,
                    context_address + 4: ring_end,
                    context_address + 0x24: ring_base,
                    context_address + 0x28: ring_end,
                    context_address + 0x2C: 0x6A52,
                    context_address + 0x30: get_pointer_address,
                    context_address + 0x17F4: dma_state_address,
                    stack_address: 0,
                    stack_address + 4: 0x8000,
                    stack_address + 8: 0x10000,
                }
            )
            self.assertEqual(
                executor.run(
                    reserve_state,
                    reserve_memory,
                    memory_write_span_observer=observe_direct_spans,
                    memory_write_batch_address_base=(
                        TITLE_D3D_PUSH_BUFFER_BASE_ADDRESS
                    ),
                    memory_write_observer_ranges_provider=lambda: (
                        (ring_base, ring_end),
                    ),
                    capture_observed_write_provenance=False,
                    direct_observed_write_transport=True,
                    max_steps=4,
                ),
                0,
            )
            reserve_performance = executor.last_run_summary["performance"]

        self.assertEqual(allocation_state.get_register("eax"), 0x20)
        self.assertEqual(allocation_state.get_register("esp"), stack_address + 8)
        self.assertEqual(
            allocation_memory.read_u32(context_address),
            packet_address + TITLE_D3D_PACKET_ALLOC_SIZE,
        )
        self.assertEqual(allocation_memory.read_u32(context_address + 0x2C), 0x22)
        self.assertEqual(direct_spans[0][1], [(packet_address, 0, 24, 3, 0)])
        self.assertEqual(
            direct_spans[0][0],
            struct.pack("<6I", 0x00041D70, 0x20, 0x00041D90, 0, 0x00041D90, 0),
        )
        self.assertEqual(allocation_performance["handler_call_count"], 0)
        self.assertEqual(allocation_performance["direct_observed_write_count"], 3)
        allocation_counts = {
            item["address"]: item["invocation_count"]
            for item in allocation_performance["native_fast_paths"]
        }
        self.assertEqual(allocation_counts[TITLE_D3D_PACKET_ALLOC_ADDRESS], 1)

        expected_limit = ring_end - TITLE_D3D_RESERVE_LIMIT_MARGIN
        expected_return = reserve_packet + TITLE_D3D_PACKET_ALLOC_SIZE
        self.assertEqual(reserve_state.get_register("eax"), expected_return)
        self.assertEqual(reserve_state.get_register("esp"), stack_address + 12)
        self.assertEqual(reserve_memory.read_u32(context_address), expected_return)
        self.assertEqual(reserve_memory.read_u32(context_address + 4), expected_limit)
        self.assertEqual(
            reserve_memory.read_u32(TITLE_GPU_SUBMISSION_BASE_ADDRESS),
            expected_return,
        )
        self.assertEqual(
            reserve_memory.read_u32(TITLE_GPU_SUBMISSION_LIMIT_ADDRESS),
            expected_limit,
        )
        self.assertEqual(direct_spans[1][1], [(reserve_packet, 0, 24, 3, 0)])
        self.assertEqual(
            direct_spans[1][0],
            struct.pack(
                "<6I",
                0x00041D70,
                0x6A52,
                0x00041D90,
                0,
                0x00041D90,
                0,
            ),
        )
        self.assertEqual(reserve_performance["handler_call_count"], 0)
        self.assertEqual(reserve_performance["direct_observed_write_count"], 3)
        allocator_counts = {
            item["address"]: item["invocation_count"]
            for item in reserve_performance["native_fast_paths"]
        }
        self.assertEqual(allocator_counts[TITLE_D3D_RESERVE_ADDRESS], 1)

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

    def test_title_native_draw_fast_paths_emit_expected_push_words(self) -> None:
        functions = [
            lift_x86_function(
                b"\xC3",
                base_address=address,
                symbol=f"native_draw_fast_path_{address:08x}",
            )
            for address in (
                TITLE_D3D_INDEXED_DRAW_WRAPPER_ADDRESS,
                TITLE_IMMEDIATE_DRAW_ADDRESS,
                TITLE_IMMEDIATE_DRAW_CORE_ADDRESS,
                TITLE_D3D_INDEXED_DRAW_ADDRESS,
            )
        ]
        instructions = tuple(
            instruction
            for function in functions
            for instruction in function.instructions
        )
        function = LiftedFunction(
            symbol="title_native_draw_fast_paths",
            base_address=TITLE_D3D_INDEXED_DRAW_WRAPPER_ADDRESS,
            code_size=max(item.next_address for item in instructions)
            - TITLE_D3D_INDEXED_DRAW_WRAPPER_ADDRESS,
            instructions=instructions,
        )
        context_address = 0x400000
        source_address = 0x500000
        push_address = 0x600000
        stack_address = 0x800000

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                module_functions=functions,
                native_fast_paths=_title_native_fast_paths(),
            )
            indexed_state = CpuState.with_registers(esp=stack_address)
            indexed_state.eip = TITLE_D3D_INDEXED_DRAW_ADDRESS
            indexed_memory = SparseMemory(
                {
                    0x002256B8: context_address,
                    0x00225218: 0x40000000,
                    context_address: push_address,
                    context_address + 4: push_address + 0x10000,
                    context_address + 8: 0,
                    stack_address: 0,
                    stack_address + 4: 6,
                    stack_address + 8: 5,
                    stack_address + 12: source_address,
                    source_address: struct.pack("<5H", 1, 2, 3, 4, 5),
                    push_address: bytes(0x1000),
                }
            )
            self.assertEqual(
                executor.run(indexed_state, indexed_memory, max_steps=4),
                0,
            )
            self.assertEqual(
                struct.unpack("<9I", indexed_memory.read(push_address, 36)),
                (
                    0x000417FC,
                    6,
                    0x40081800,
                    0x00020001,
                    0x00040003,
                    0x00041808,
                    5,
                    0x000417FC,
                    0,
                ),
            )
            self.assertEqual(indexed_memory.read_u32(context_address), push_address + 36)
            self.assertEqual(indexed_state.get_register("esp"), stack_address + 16)

            indexed_wrapper_state = CpuState.with_registers(esp=stack_address)
            indexed_wrapper_state.eip = TITLE_D3D_INDEXED_DRAW_WRAPPER_ADDRESS
            indexed_wrapper_memory = SparseMemory(
                {
                    0x002256B8: context_address,
                    0x00225218: 0x40000000,
                    context_address: push_address,
                    context_address + 4: push_address + 0x10000,
                    context_address + 8: 0,
                    stack_address: 0,
                    stack_address + 4: 6,
                    stack_address + 8: 5,
                    stack_address + 12: source_address,
                    source_address: struct.pack("<5H", 1, 2, 3, 4, 5),
                    push_address: bytes(0x1000),
                }
            )
            self.assertEqual(
                executor.run(
                    indexed_wrapper_state,
                    indexed_wrapper_memory,
                    max_steps=4,
                ),
                0,
            )
            self.assertEqual(
                indexed_wrapper_memory.read(push_address, 36),
                indexed_memory.read(push_address, 36),
            )
            self.assertEqual(
                indexed_wrapper_state.get_register("esp"),
                stack_address + 4,
            )

            live_context_address = 0x002256C0
            deferred_push_address = push_address + 0x2000
            deferred_wrapper_state = CpuState.with_registers(esp=stack_address)
            deferred_wrapper_state.eip = TITLE_D3D_INDEXED_DRAW_WRAPPER_ADDRESS
            deferred_wrapper_memory = SparseMemory(
                {
                    0x002256B8: live_context_address,
                    0x00225218: 0x40000000,
                    live_context_address: deferred_push_address,
                    live_context_address + 4: deferred_push_address + 0x10000,
                    live_context_address + 8: 0,
                    0x005AD740: 1,
                    0x005ADA00: 0x43,
                    0x005AD18C: 0x010101,
                    0x005AD86C: 1,
                    0x0022552C: 0,
                    0x002940FC: 0x00040300,
                    stack_address: 0,
                    stack_address + 4: 6,
                    stack_address + 8: 5,
                    stack_address + 12: source_address,
                    source_address: struct.pack("<5H", 1, 2, 3, 4, 5),
                    deferred_push_address: bytes(0x1000),
                }
            )
            self.assertEqual(
                executor.run(
                    deferred_wrapper_state,
                    deferred_wrapper_memory,
                    max_steps=4,
                ),
                0,
            )
            self.assertEqual(
                struct.unpack(
                    "<11I",
                    deferred_wrapper_memory.read(deferred_push_address, 44),
                ),
                (
                    0x00040300,
                    0x00010101,
                    0x000417FC,
                    6,
                    0x40081800,
                    0x00020001,
                    0x00040003,
                    0x00041808,
                    5,
                    0x000417FC,
                    0,
                ),
            )
            self.assertEqual(deferred_wrapper_memory.read_u32(0x005AD740), 0)
            self.assertEqual(deferred_wrapper_memory.read_u32(0x005AD86C), 0)
            self.assertEqual(
                deferred_wrapper_memory.read_u32(0x0022552C),
                0x00010101,
            )
            self.assertEqual(
                deferred_wrapper_memory.read_u32(live_context_address),
                deferred_push_address + 44,
            )
            self.assertEqual(
                deferred_wrapper_state.get_register("esp"),
                stack_address + 4,
            )

            vertex_state_address = 0x410000
            resource_address = 0x420000
            force_rebind_data: dict[int, int | bytes] = {
                0x002256B8: context_address,
                0x00225218: 0x40,
                context_address: push_address,
                context_address + 4: push_address + 0x10000,
                context_address + 8: 0,
                context_address + 0x1C: 2,
                context_address + 0x20: 2,
                context_address + 0x37C: vertex_state_address,
                vertex_state_address + 4: 0,
                0x003430B8: bytes(range(16)),
                0x002242F0: 12,
                0x002242F4: 8,
                0x002242F8: resource_address,
                resource_address + 4: 0x90000000,
                stack_address: 0,
                stack_address + 4: 6,
                stack_address + 8: 2,
                stack_address + 12: source_address,
                source_address: struct.pack("<2H", 1, 2),
                push_address: bytes(0x1000),
            }
            force_rebind_data[vertex_state_address + 0x14] = 0
            force_rebind_data[vertex_state_address + 0x18] = 0x20
            force_rebind_data[vertex_state_address + 0x1C] = 0
            for stream in range(1, 16):
                force_rebind_data[vertex_state_address + stream * 16 + 0x1C] = 2
            force_rebind_state = CpuState.with_registers(esp=stack_address)
            force_rebind_state.eip = TITLE_D3D_INDEXED_DRAW_ADDRESS
            force_rebind_memory = SparseMemory(force_rebind_data)
            self.assertEqual(
                executor.run(force_rebind_state, force_rebind_memory, max_steps=4),
                0,
            )
            self.assertEqual(
                struct.unpack("<8I", force_rebind_memory.read(push_address, 32)),
                (
                    0x00041720,
                    0x90000040,
                    0x000417FC,
                    6,
                    0x40041800,
                    0x00020001,
                    0x000417FC,
                    0,
                ),
            )
            self.assertEqual(force_rebind_memory.read_u32(0x00225218), 0)
            self.assertEqual(
                force_rebind_memory.read_u32(context_address), push_address + 32
            )
            self.assertEqual(force_rebind_state.get_register("esp"), stack_address + 16)

            direct_spans: list[
                tuple[bytes, list[tuple[int, int, int, int, int]]]
            ] = []

            def observe_direct_spans(
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
                direct_spans.append(
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

            direct_state = CpuState.with_registers(esp=stack_address)
            direct_state.eip = TITLE_D3D_INDEXED_DRAW_ADDRESS
            direct_memory = SparseMemory(force_rebind_data)
            self.assertEqual(
                executor.run(
                    direct_state,
                    direct_memory,
                    memory_write_span_observer=observe_direct_spans,
                    memory_write_batch_address_base=push_address,
                    memory_write_observer_ranges_provider=lambda: (
                        (push_address, push_address + 0x1000),
                    ),
                    capture_observed_write_provenance=False,
                    direct_observed_write_transport=True,
                    max_steps=4,
                ),
                0,
            )
            self.assertEqual(direct_memory.read(push_address, 32), bytes(32))
            self.assertEqual(
                direct_spans[0][1],
                [(push_address, 0, 32, 4, 0)],
            )
            self.assertEqual(
                direct_spans[0][0],
                struct.pack(
                    "<8I",
                    0x00041720,
                    0x90000040,
                    0x000417FC,
                    6,
                    0x40041800,
                    0x00020001,
                    0x000417FC,
                    0,
                ),
            )
            direct_performance = executor.last_run_summary["performance"]
            self.assertEqual(direct_performance["native_observed_write_count"], 4)
            self.assertEqual(direct_performance["direct_observed_write_count"], 4)
            self.assertEqual(
                direct_performance["direct_observed_write_byte_count"], 32
            )

            immediate_state = CpuState.with_registers(esp=stack_address)
            immediate_state.eip = TITLE_IMMEDIATE_DRAW_CORE_ADDRESS
            immediate_data = {
                0x002256B8: context_address,
                0x00225218: 0,
                context_address: push_address,
                context_address + 4: push_address + 0x10000,
                context_address + 8: 0,
                context_address + 0x7A8: 2,
                context_address + 0x7AC: 0,
                context_address + 0x7B0: 0,
                context_address + 0x7B4: 2,
                context_address + 0x7B8: 0,
                context_address + 0x834: 1,
                stack_address: 0,
                stack_address + 4: 6,
                stack_address + 8: 2,
                stack_address + 12: source_address,
                stack_address + 16: 8,
                source_address: struct.pack("<8I", *range(0x11, 0x19)),
                push_address: bytes(0x1000),
            }
            immediate_memory = SparseMemory(immediate_data)
            self.assertEqual(
                executor.run(immediate_state, immediate_memory, max_steps=4),
                0,
            )

            immediate_wrapper_state = CpuState.with_registers(esp=stack_address)
            immediate_wrapper_state.eip = TITLE_IMMEDIATE_DRAW_ADDRESS
            immediate_wrapper_memory = SparseMemory(immediate_data)
            self.assertEqual(
                executor.run(
                    immediate_wrapper_state,
                    immediate_wrapper_memory,
                    max_steps=4,
                ),
                0,
            )
            self.assertEqual(
                immediate_wrapper_memory.read(push_address, 36),
                immediate_memory.read(push_address, 36),
            )
            self.assertEqual(
                immediate_wrapper_state.get_register("esp"),
                stack_address + 4,
            )
            wrapper_performance = executor.last_run_summary["performance"]

        self.assertEqual(
            struct.unpack("<9I", immediate_memory.read(push_address, 36)),
            (
                0x000417FC,
                6,
                0x40101818,
                0x11,
                0x12,
                0x15,
                0x16,
                0x000417FC,
                0,
            ),
        )
        self.assertEqual(immediate_memory.read_u32(context_address), push_address + 36)
        self.assertEqual(immediate_memory.read_u32(context_address + 0x7B8), 8)
        self.assertEqual(immediate_state.get_register("esp"), stack_address + 20)
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_fast_path_invocation_count"], 1)
        wrapper_counts = {
            item["address"]: item["invocation_count"]
            for item in wrapper_performance["native_fast_paths"]
        }
        self.assertEqual(wrapper_counts[TITLE_IMMEDIATE_DRAW_ADDRESS], 1)

    def test_title_native_indexed_prepare_and_scene_callers(self) -> None:
        addresses = (
            TITLE_SCENE_RECORD_DISTANCE_CULL_ADDRESS,
            TITLE_D3D_INDEXED_STATE_PREPARE_ADDRESS,
            TITLE_INDEXED_RESOURCE_DRAW_ADDRESS,
            TITLE_SCENE_RECORD_RESOURCE_DRAW_ADDRESS,
            TITLE_SCENE_RECORD_INDEXED_DRAW_ADDRESS,
        )
        functions = [
            lift_x86_function(
                b"\xC3",
                base_address=address,
                symbol=f"native_indexed_caller_{address:08x}",
            )
            for address in addresses
        ]
        instructions = tuple(
            instruction
            for function in functions
            for instruction in function.instructions
        )
        function = LiftedFunction(
            symbol="title_native_indexed_callers",
            base_address=min(addresses),
            code_size=max(item.next_address for item in instructions) - min(addresses),
            instructions=instructions,
        )
        context_address = 0x400000
        vertex_state_address = 0x410000
        resource_address = 0x420000
        resource_record = 0x430000
        resource_object = 0x440000
        scene_record = 0x450000
        scene_owner = 0x460000
        source_address = 0x500000
        stack_address = 0x800000

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                module_functions=functions,
                native_fast_paths=_title_native_fast_paths(),
            )

            frame_address = 0x810000
            cull_object = 0x480000
            cull_state = CpuState.with_registers(
                eax=0xABCD0000,
                ebp=frame_address,
                esp=frame_address - 0x13C,
            )
            cull_state.eip = TITLE_SCENE_RECORD_DISTANCE_CULL_ADDRESS
            cull_state.fpu_stack = [150.0]
            cull_memory = SparseMemory(
                {
                    frame_address - 8: cull_object,
                    frame_address - 4: 0,
                    frame_address: 0x812000,
                    frame_address + 4: 0,
                    cull_object + 0x14: struct.unpack(
                        "<I",
                        struct.pack("<f", 100.0),
                    )[0],
                    cull_object + 0x24: 1,
                }
            )

            self.assertEqual(
                executor.run(cull_state, cull_memory, max_steps=4),
                0,
            )
            self.assertEqual(cull_memory.read_u32(cull_object + 0x24), 0)
            self.assertEqual(
                cull_memory.read_u32(frame_address - 4),
                struct.unpack("<I", struct.pack("<f", 150.0))[0],
            )
            self.assertEqual(cull_state.fpu_stack, [])
            self.assertEqual(cull_state.get_register("ecx"), cull_object)
            self.assertEqual(cull_state.get_register("ebp"), 0x812000)
            self.assertEqual(cull_state.get_register("esp"), frame_address + 0x10)

            visible_state = CpuState.with_registers(
                ebp=frame_address,
                esp=frame_address - 0x13C,
            )
            visible_state.eip = TITLE_SCENE_RECORD_DISTANCE_CULL_ADDRESS
            visible_state.fpu_stack = [50.0]
            visible_memory = SparseMemory(
                {
                    frame_address - 8: cull_object,
                    frame_address - 4: 0,
                    frame_address - 0x13C: 0,
                    cull_object + 0x14: struct.unpack(
                        "<I",
                        struct.pack("<f", 100.0),
                    )[0],
                    cull_object + 0x24: 1,
                }
            )

            self.assertEqual(
                executor.run(visible_state, visible_memory, max_steps=4),
                0,
            )
            self.assertEqual(visible_memory.read_u32(cull_object + 0x24), 1)
            self.assertEqual(visible_memory.read_u32(frame_address - 4), 0)
            self.assertEqual(visible_state.fpu_stack, [50.0])

            prepare_push = 0x600000
            prepare_data: dict[int, int | bytes] = {
                0x00225218: 0x40,
                context_address: prepare_push,
                context_address + 4: prepare_push + 0x1000,
                context_address + 0x20: 2,
                context_address + 0x37C: vertex_state_address,
                vertex_state_address + 4: 0,
                vertex_state_address + 0x14: 0,
                vertex_state_address + 0x18: 0x20,
                vertex_state_address + 0x1C: 0,
                0x003430B8: bytes(range(16)),
                0x002242F0: 12,
                0x002242F4: 8,
                0x002242F8: resource_address,
                resource_address + 4: 0x90000000,
                stack_address: 0,
                stack_address + 4: 2,
                prepare_push: bytes(0x100),
            }
            for stream in range(1, 16):
                prepare_data[vertex_state_address + stream * 16 + 0x1C] = 2
            prepare_state = CpuState.with_registers(
                ecx=context_address,
                esp=stack_address,
            )
            prepare_state.eip = TITLE_D3D_INDEXED_STATE_PREPARE_ADDRESS
            prepare_memory = SparseMemory(prepare_data)

            self.assertEqual(
                executor.run(prepare_state, prepare_memory, max_steps=4),
                0,
            )
            self.assertEqual(
                struct.unpack("<2I", prepare_memory.read(prepare_push, 8)),
                (0x00041720, 0x90000040),
            )
            self.assertEqual(prepare_memory.read_u32(context_address), prepare_push + 8)
            self.assertEqual(prepare_memory.read_u32(0x00225218), 0)
            self.assertEqual(prepare_state.get_register("eax"), context_address)
            self.assertEqual(prepare_state.get_register("ecx"), prepare_push + 8)
            self.assertEqual(prepare_state.get_register("esp"), stack_address + 8)

            bound_push = 0x610000
            bound_data = {
                0x002256B8: context_address,
                0x00225218: 0x40000000,
                0x002242F0: 0,
                0x002242F8: 0,
                context_address: bound_push,
                context_address + 4: bound_push + 0x1000,
                context_address + 8: 0,
                resource_object + 8: 7,
                resource_record: 0,
                resource_record + 0xC: source_address,
                resource_record + 0x10: 5,
                source_address: struct.pack("<5H", 1, 2, 3, 4, 5),
                stack_address: 0,
                stack_address + 4: resource_record,
                bound_push: bytes(0x100),
            }
            bound_state = CpuState.with_registers(
                ecx=resource_object,
                esi=0x12345678,
                esp=stack_address,
            )
            bound_state.eip = TITLE_INDEXED_RESOURCE_DRAW_ADDRESS
            bound_memory = SparseMemory(bound_data)

            self.assertEqual(
                executor.run(bound_state, bound_memory, max_steps=4),
                0,
            )
            self.assertEqual(
                struct.unpack("<9I", bound_memory.read(bound_push, 36)),
                (
                    0x000417FC,
                    6,
                    0x40081800,
                    0x00020001,
                    0x00040003,
                    0x00041808,
                    5,
                    0x000417FC,
                    0,
                ),
            )
            self.assertEqual(bound_memory.read_u32(resource_record), 0x00080000)
            self.assertEqual(bound_memory.read_u32(0x002242F0), 7)
            self.assertEqual(bound_memory.read_u32(0x002242F8), resource_record)
            self.assertEqual(bound_state.get_register("esi"), 0x12345678)
            self.assertEqual(bound_state.get_register("esp"), stack_address + 8)

            fallback_data = {
                **bound_data,
                context_address: bound_push + 0x1300,
                context_address + 4: bound_push + 0x1000,
            }
            fallback_state = CpuState.with_registers(
                ecx=resource_object,
                esp=stack_address,
            )
            fallback_state.eip = TITLE_INDEXED_RESOURCE_DRAW_ADDRESS
            fallback_memory = SparseMemory(fallback_data)

            self.assertEqual(
                executor.run(fallback_state, fallback_memory, max_steps=4),
                0,
            )
            self.assertEqual(fallback_memory.read_u32(resource_record), 0)
            self.assertEqual(fallback_memory.read_u32(0x002242F8), 0)

            release_resource = 0x470000
            release_data = {
                **bound_data,
                0x002242F8: release_resource,
                release_resource: 0x00080000,
                release_resource + 8: 0,
            }
            release_state = CpuState.with_registers(
                ecx=resource_object,
                esp=stack_address,
            )
            release_state.eip = TITLE_INDEXED_RESOURCE_DRAW_ADDRESS
            release_memory = SparseMemory(release_data)

            self.assertEqual(
                executor.run(release_state, release_memory, max_steps=4),
                0,
            )
            self.assertEqual(release_memory.read_u32(resource_record), 0)
            self.assertEqual(release_memory.read_u32(release_resource), 0x00080000)
            self.assertEqual(
                release_memory.read_u32(0x002242F8),
                release_resource,
            )

            scene_resource_push = 0x618000
            scene_resource_data = {
                **bound_data,
                context_address: scene_resource_push,
                context_address + 4: scene_resource_push + 0x1000,
                resource_object + 0x24: 1,
                scene_resource_push: bytes(0x100),
            }
            scene_resource_state = CpuState.with_registers(
                ecx=resource_object,
                esi=0x12345678,
                esp=stack_address,
            )
            scene_resource_state.eip = TITLE_SCENE_RECORD_RESOURCE_DRAW_ADDRESS
            scene_resource_memory = SparseMemory(scene_resource_data)

            self.assertEqual(
                executor.run(
                    scene_resource_state,
                    scene_resource_memory,
                    max_steps=4,
                ),
                0,
            )
            self.assertEqual(
                scene_resource_memory.read(scene_resource_push, 36),
                bound_memory.read(bound_push, 36),
            )
            self.assertEqual(scene_resource_state.get_register("esp"), stack_address + 8)

            scene_push = 0x620000
            scene_data = {
                0x002256B8: context_address,
                0x00225218: 0x40000000,
                context_address: scene_push,
                context_address + 4: scene_push + 0x1000,
                context_address + 8: 0,
                scene_owner + 0x24: 1,
                scene_record + 0xC: source_address,
                scene_record + 0x10: 5,
                source_address: struct.pack("<5H", 1, 2, 3, 4, 5),
                stack_address: 0,
                stack_address + 4: scene_record,
                scene_push: bytes(0x100),
            }
            scene_state = CpuState.with_registers(
                ecx=scene_owner,
                esp=stack_address,
            )
            scene_state.eip = TITLE_SCENE_RECORD_INDEXED_DRAW_ADDRESS
            scene_memory = SparseMemory(scene_data)

            self.assertEqual(
                executor.run(scene_state, scene_memory, max_steps=4),
                0,
            )
            self.assertEqual(
                scene_memory.read(scene_push, 36),
                bound_memory.read(bound_push, 36),
            )
            self.assertEqual(scene_state.get_register("esp"), stack_address + 8)

        counts = {
            item["address"]: item["invocation_count"]
            for item in executor.last_run_summary["performance"]["native_fast_paths"]
        }
        self.assertEqual(counts[TITLE_SCENE_RECORD_INDEXED_DRAW_ADDRESS], 1)

    def test_title_native_dirty_draw_continuations_use_bulk_superpaths(self) -> None:
        def f32(value: float) -> int:
            return struct.unpack("<I", struct.pack("<f", value))[0]

        addresses = (
            TITLE_IMMEDIATE_DRAW_CONTINUATION_ADDRESS,
            TITLE_D3D_INDEXED_DRAW_CONTINUATION_ADDRESS,
            TITLE_D3D_TEXTURE_STATE_ADDRESS,
        )
        functions = [
            lift_x86_function(
                b"\xC3",
                base_address=address,
                symbol=f"native_dirty_draw_{address:08x}",
            )
            for address in addresses
        ]
        instructions = tuple(
            instruction
            for function in functions
            for instruction in function.instructions
        )
        function = LiftedFunction(
            symbol="title_native_dirty_draw_superpaths",
            base_address=min(addresses),
            code_size=max(item.next_address for item in instructions) - min(addresses),
            instructions=instructions,
        )
        context_address = 0x400000
        source_address = 0x500000
        push_address = 0x600000
        frame_address = 0x800000

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                module_functions=functions,
                native_fast_paths=_title_native_fast_paths(),
            )

            texture_source = 0x00225224
            texture_stack = 0x810000
            texture_state = CpuState.with_registers(esp=texture_stack)
            texture_state.eip = TITLE_D3D_TEXTURE_STATE_ADDRESS
            texture_memory = SparseMemory(
                {
                    texture_stack: 0,
                    texture_stack + 4: context_address,
                    texture_stack + 8: 1,
                    context_address: push_address,
                    context_address + 4: push_address + 0x10000,
                    context_address + 0x550: f32(0.0),
                    context_address + 0xB68: 0,
                    texture_source - 4: 0x100,
                    texture_source: 3,
                    texture_source + 4: 2,
                    texture_source + 8: 1,
                    texture_source + 0xC: 1,
                    texture_source + 0x10: 0,
                    texture_source + 0x14: f32(0.0),
                    texture_source + 0x18: 0,
                    texture_source + 0x1C: 0,
                    texture_source + 0x20: 0x20,
                    texture_source + 0x24: 0,
                    texture_source + 0x28: 0x10,
                    texture_source + 0x2C: 0x19,
                    texture_source + 0x6C: 0,
                    0x002255A8: 0x10000000,
                    0x00223498 + 3 * 4: 0x20000000,
                    0x002B8EE8: f32(1.0),
                    0x002943BC: f32(0.0),
                    push_address: bytes(0x1000),
                }
            )
            self.assertEqual(
                executor.run(texture_state, texture_memory, max_steps=4),
                0,
            )
            self.assertEqual(
                struct.unpack("<5I", texture_memory.read(push_address, 20)),
                (0x00081B08, 0x10020300, 0x0003FFF0, 0x00041B14, 0xE1002000),
            )
            self.assertEqual(
                texture_memory.read_u32(context_address + 0x35C),
                0x4003FFF0,
            )
            self.assertEqual(texture_memory.read_u32(context_address), push_address + 20)
            self.assertEqual(texture_state.get_register("esp"), texture_stack + 12)
            texture_counts = {
                item["address"]: item["invocation_count"]
                for item in executor.last_run_summary["performance"]["native_fast_paths"]
            }

            indexed_stack = frame_address - 0x14
            indexed_state = CpuState.with_registers(
                esi=context_address,
                ebp=frame_address,
                esp=indexed_stack,
            )
            indexed_state.eip = TITLE_D3D_INDEXED_DRAW_CONTINUATION_ADDRESS
            indexed_memory = SparseMemory(
                {
                    indexed_stack: 0x11111111,
                    indexed_stack + 4: 0x22222222,
                    indexed_stack + 8: 0x33333333,
                    frame_address: 0x44444444,
                    frame_address + 4: 0,
                    frame_address + 8: 6,
                    frame_address + 0xC: 5,
                    frame_address + 0x10: source_address,
                    context_address: push_address,
                    context_address + 4: push_address + 0x10000,
                    context_address + 8: 0,
                    source_address: struct.pack("<5H", 1, 2, 3, 4, 5),
                    push_address: bytes(0x1000),
                }
            )
            self.assertEqual(
                executor.run(indexed_state, indexed_memory, max_steps=4),
                0,
            )
            self.assertEqual(
                struct.unpack("<9I", indexed_memory.read(push_address, 36)),
                (0x000417FC, 6, 0x40081800, 0x00020001, 0x00040003, 0x00041808, 5, 0x000417FC, 0),
            )
            self.assertEqual(indexed_state.get_register("edi"), 0x11111111)
            self.assertEqual(indexed_state.get_register("esi"), 0x22222222)
            self.assertEqual(indexed_state.get_register("ebx"), 0x33333333)
            self.assertEqual(indexed_state.get_register("ebp"), 0x44444444)
            self.assertEqual(indexed_state.get_register("esp"), frame_address + 20)
            indexed_counts = {
                item["address"]: item["invocation_count"]
                for item in executor.last_run_summary["performance"]["native_fast_paths"]
            }

            immediate_stack = frame_address - 0x20
            immediate_state = CpuState.with_registers(
                edi=context_address,
                ebp=frame_address,
                esp=immediate_stack,
            )
            immediate_state.eip = TITLE_IMMEDIATE_DRAW_CONTINUATION_ADDRESS
            immediate_memory = SparseMemory(
                {
                    immediate_stack: 0xAAAAAAAA,
                    immediate_stack + 4: 0xBBBBBBBB,
                    immediate_stack + 8: 0xCCCCCCCC,
                    frame_address: 0xDDDDDDDD,
                    frame_address + 4: 0,
                    frame_address + 8: 6,
                    frame_address + 0xC: 2,
                    frame_address + 0x10: source_address,
                    frame_address + 0x14: 8,
                    context_address: push_address,
                    context_address + 4: push_address + 0x10000,
                    context_address + 8: 0,
                    context_address + 0x7A8: 2,
                    context_address + 0x7AC: 0,
                    context_address + 0x7B0: 0,
                    context_address + 0x7B4: 2,
                    context_address + 0x7B8: 0,
                    context_address + 0x834: 1,
                    source_address: struct.pack("<8I", *range(0x11, 0x19)),
                    push_address: bytes(0x1000),
                }
            )
            self.assertEqual(
                executor.run(immediate_state, immediate_memory, max_steps=4),
                0,
            )
            self.assertEqual(
                struct.unpack("<9I", immediate_memory.read(push_address, 36)),
                (0x000417FC, 6, 0x40101818, 0x11, 0x12, 0x15, 0x16, 0x000417FC, 0),
            )
            self.assertEqual(immediate_state.get_register("edi"), 0xAAAAAAAA)
            self.assertEqual(immediate_state.get_register("esi"), 0xBBBBBBBB)
            self.assertEqual(immediate_state.get_register("ebx"), 0xCCCCCCCC)
            self.assertEqual(immediate_state.get_register("ebp"), 0xDDDDDDDD)
            self.assertEqual(immediate_state.get_register("esp"), frame_address + 24)

            immediate_counts = {
                item["address"]: item["invocation_count"]
                for item in executor.last_run_summary["performance"]["native_fast_paths"]
            }

        self.assertEqual(texture_counts[TITLE_D3D_TEXTURE_STATE_ADDRESS], 1)
        self.assertEqual(
            indexed_counts[TITLE_D3D_INDEXED_DRAW_CONTINUATION_ADDRESS],
            1,
        )
        self.assertEqual(
            immediate_counts[TITLE_IMMEDIATE_DRAW_CONTINUATION_ADDRESS],
            1,
        )

    def test_title_native_quad_batch_continuation_flushes_and_restores_state(self) -> None:
        function = lift_x86_function(
            b"\xC3",
            base_address=TITLE_QUAD_BATCH_CONTINUATION_ADDRESS,
            symbol="title_quad_batch_continuation",
        )
        context_address = 0x002256C0
        object_address = 0x500000
        push_address = 0x600000
        stack_address = 0x800000
        state = CpuState.with_registers(
            esi=object_address,
            edi=0x12345678,
            esp=stack_address,
        )
        state.eip = TITLE_QUAD_BATCH_CONTINUATION_ADDRESS
        memory = SparseMemory(
            {
                0x002256B8: context_address,
                0x00225218: 0,
                context_address: push_address,
                context_address + 4: push_address + 0x10000,
                context_address + 8: 0,
                context_address + 0x7A8: 7,
                context_address + 0x7AC: 0,
                context_address + 0x7B0: 0,
                context_address + 0x7B4: 7,
                context_address + 0x7B8: 0,
                context_address + 0x834: 1,
                object_address: struct.pack("<7I", 1, 2, 3, 4, 5, 6, 7),
                object_address + 0x1C00: 1,
                object_address + 0x1C08: 1,
                object_address + 0x1C14: 2,
                object_address + 0x1C34: 3,
                object_address + 0x1C54: 4,
                object_address + 0x1C84: 0,
                0x005AD178: 2,
                0x005AD17C: 3,
                0x005AD18C: 0x00010101,
                0x005AD1A8: 4,
                0x00225518: 2,
                0x0022551C: 3,
                0x0022552C: 0x00010101,
                0x00225548: 4,
                0x002940E8: 0x00003E00,
                0x002940EC: 0x00003F00,
                0x002940FC: 0x00004300,
                0x00294118: 0x00004A00,
                stack_address + 0x18: 0xDEADBEEF,
                stack_address + 0x1C: 0,
                push_address: bytes(0x1000),
            }
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                native_fast_paths=_title_native_fast_paths(),
            )
            self.assertEqual(executor.run(state, memory, max_steps=4), 0)

        self.assertEqual(
            struct.unpack("<20I", memory.read(push_address, 80)),
            (
                0x00004300,
                0x01000000,
                0x00003E00,
                1,
                0x00003F00,
                0,
                0x00004A00,
                0x00008006,
                0x000417FC,
                6,
                0x401C1818,
                1,
                2,
                3,
                4,
                5,
                6,
                7,
                0x000417FC,
                0,
            ),
        )
        self.assertEqual(memory.read_u32(context_address), push_address + 80)
        self.assertEqual(memory.read_u32(object_address + 0x1C00), 0)
        self.assertEqual(memory.read_u32(0x005AD740), 4)
        self.assertEqual(
            struct.unpack("<4I", memory.read(0x005ADA00, 16)),
            (0x43, 0x3E, 0x3F, 0x4A),
        )
        self.assertEqual(memory.read_u32(0x005AD18C), 0x00010101)
        self.assertEqual(memory.read_u32(0x005AD178), 2)
        self.assertEqual(memory.read_u32(0x005AD17C), 3)
        self.assertEqual(memory.read_u32(0x005AD1A8), 4)
        self.assertEqual(memory.read_u32(0x0022552C), 0x01000000)
        self.assertEqual(memory.read_u32(0x00225518), 1)
        self.assertEqual(memory.read_u32(0x0022551C), 0)
        self.assertEqual(memory.read_u32(0x00225548), 0x00008006)
        self.assertEqual(state.get_register("esi"), 0xDEADBEEF)
        self.assertEqual(state.get_register("edi"), 0x12345678)
        self.assertEqual(state.get_register("esp"), stack_address + 0x20)

    def test_title_native_quad_and_matrix_fast_paths(self) -> None:
        def f32(value: float) -> int:
            return struct.unpack("<I", struct.pack("<f", value))[0]

        functions = [
            lift_x86_function(
                b"\xC3",
                base_address=address,
                symbol=f"native_fast_path_{address:08x}",
            )
            for address in (
                TITLE_QUAD_CLIP_INTERPOLATE_ADDRESS,
                TITLE_VERTEX_APPEND_COMPACT_ADDRESS,
                TITLE_VERTEX_APPEND_ADDRESS,
                TITLE_MATRIX_MULTIPLY_ADDRESS,
            )
        ]
        instructions = tuple(
            instruction
            for function in functions
            for instruction in function.instructions
        )
        function = LiftedFunction(
            symbol="title_native_quad_and_matrix_fast_paths",
            base_address=TITLE_QUAD_CLIP_INTERPOLATE_ADDRESS,
            code_size=max(item.next_address for item in instructions)
            - TITLE_QUAD_CLIP_INTERPOLATE_ADDRESS,
            instructions=instructions,
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                module_functions=functions,
                native_fast_paths=_title_native_fast_paths(),
            )

            object_address = 0x500000
            color_address = 0x510000
            stack_address = 0x800000
            full_state = CpuState.with_registers(
                ecx=object_address,
                esp=stack_address,
            )
            full_state.eip = TITLE_VERTEX_APPEND_ADDRESS
            full_memory = SparseMemory(
                {
                    stack_address: 0,
                    stack_address + 4: f32(10.0),
                    stack_address + 8: f32(20.0),
                    stack_address + 12: color_address,
                    stack_address + 16: f32(0.25),
                    stack_address + 20: f32(0.5),
                    object_address + TITLE_VERTEX_APPEND_COUNT_OFFSET: 3,
                    object_address + TITLE_VERTEX_APPEND_DEFAULT_Z_OFFSET: f32(1.5),
                    color_address: f32(64.0),
                    color_address + 4: f32(32.0),
                    color_address + 8: f32(16.0),
                    color_address + 12: f32(255.0),
                }
            )
            self.assertEqual(executor.run(full_state, full_memory, max_steps=4), 0)
            record_address = object_address + 3 * TITLE_VERTEX_APPEND_STRIDE
            self.assertEqual(full_state.get_register("esp"), stack_address + 24)
            self.assertEqual(full_memory.read_u32(record_address), f32(10.0))
            self.assertEqual(full_memory.read_u32(record_address + 4), f32(20.0))
            self.assertEqual(full_memory.read_u32(record_address + 8), f32(1.5))
            self.assertEqual(full_memory.read_u32(record_address + 0x10), 0xFF402010)
            self.assertEqual(full_memory.read_u32(record_address + 0x14), f32(0.25))
            self.assertEqual(full_memory.read_u32(record_address + 0x18), f32(0.5))

            compact_object = 0x520000
            compact_state = CpuState.with_registers(
                ecx=compact_object,
                esp=stack_address,
            )
            compact_state.eip = TITLE_VERTEX_APPEND_COMPACT_ADDRESS
            compact_memory = SparseMemory(
                {
                    stack_address: 0,
                    stack_address + 4: f32(30.0),
                    stack_address + 8: f32(40.0),
                    stack_address + 12: color_address,
                    compact_object + TITLE_VERTEX_APPEND_COUNT_OFFSET: 2,
                    compact_object + TITLE_VERTEX_APPEND_DEFAULT_Z_OFFSET: f32(2.0),
                    color_address: f32(64.0),
                    color_address + 4: f32(32.0),
                    color_address + 8: f32(16.0),
                    color_address + 12: f32(255.0),
                }
            )
            self.assertEqual(
                executor.run(compact_state, compact_memory, max_steps=4),
                0,
            )
            compact_record = compact_object + 2 * TITLE_VERTEX_APPEND_STRIDE
            self.assertEqual(compact_state.get_register("esp"), stack_address + 16)
            self.assertEqual(compact_memory.read_u32(compact_record), f32(30.0))
            self.assertEqual(compact_memory.read_u32(compact_record + 4), f32(40.0))
            self.assertEqual(compact_memory.read_u32(compact_record + 8), f32(2.0))

            rectangle_address = 0x530000
            clip_state = CpuState.with_registers(esp=stack_address)
            clip_state.eip = TITLE_QUAD_CLIP_INTERPOLATE_ADDRESS
            clip_memory = SparseMemory(
                {
                    stack_address: 0,
                    stack_address + 4: rectangle_address,
                    rectangle_address + 0x10: f32(10.0),
                    rectangle_address + 0x14: f32(20.0),
                    rectangle_address + 0x18: f32(700.0),
                    rectangle_address + 0x1C: f32(500.0),
                }
            )
            self.assertEqual(executor.run(clip_state, clip_memory, max_steps=4), 0)
            self.assertEqual(clip_state.get_register("esp"), stack_address + 8)

            left_address = 0x540000
            right_address = 0x550000
            left = tuple(float(index + 1) / 3.0 for index in range(16))
            right = tuple(float(17 - index) / 7.0 for index in range(16))
            matrix_state = CpuState.with_registers(esp=stack_address)
            matrix_state.eip = TITLE_MATRIX_MULTIPLY_ADDRESS
            matrix_memory = SparseMemory(
                {
                    stack_address: 0,
                    stack_address + 4: left_address,
                    stack_address + 8: left_address,
                    stack_address + 12: right_address,
                    left_address: struct.pack("<16f", *left),
                    right_address: struct.pack("<16f", *right),
                }
            )
            self.assertEqual(
                executor.run(matrix_state, matrix_memory, max_steps=4),
                0,
            )

        rounded_left = struct.unpack("<16f", struct.pack("<16f", *left))
        rounded_right = struct.unpack("<16f", struct.pack("<16f", *right))
        expected = []
        for row in range(4):
            for column in range(4):
                products = [
                    struct.unpack(
                        "<f",
                        struct.pack(
                            "<f",
                            rounded_left[row * 4 + inner]
                            * rounded_right[inner * 4 + column],
                        ),
                    )[0]
                    for inner in range(4)
                ]
                sum_02 = struct.unpack(
                    "<f", struct.pack("<f", products[0] + products[2])
                )[0]
                sum_31 = struct.unpack(
                    "<f", struct.pack("<f", products[3] + products[1])
                )[0]
                expected.append(
                    struct.unpack("<f", struct.pack("<f", sum_02 + sum_31))[0]
                )
        self.assertEqual(
            matrix_memory.read(left_address, 64),
            struct.pack("<16f", *expected),
        )
        performance = executor.last_run_summary["performance"]
        self.assertEqual(performance["native_fast_path_invocation_count"], 1)
        matrix_path = next(
            item
            for item in performance["native_fast_paths"]
            if item["address"] == TITLE_MATRIX_MULTIPLY_ADDRESS
        )
        self.assertEqual(matrix_path["invocation_count"], 1)

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
        memory = SparseMemory(
            {
                0x8000: 0xFEEDFACE,
                string_address: b"PLAY\x00",
                descriptor_address: struct.pack(
                    "<4f", 255.0, 255.0, 255.0, 128.0
                ),
            }
        )

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
        self.assertEqual(summary["sampled_strings"][0]["color_argb"], 0x80FFFFFF)
        self.assertEqual(fast_path.text_for_completed_flip(8), "PLAY")
        self.assertIsNone(fast_path.text_for_completed_flip(7))
        self.assertIsNone(fast_path.text_for_completed_flip(9))
        self.assertEqual(
            fast_path.manifest_for_completed_flip(8),
            {
                "frontend_text": "PLAY",
                "frontend_text_x_bits": 0x43A00000,
                "frontend_text_y_bits": 0x43AA0000,
                "frontend_text_size_bits": 0x3F800000,
                "frontend_text_color_argb": 0x80FFFFFF,
            },
        )
        self.assertEqual(
            summary["sampled_strings"][0]["caller_return_address_hex"],
            f"0x{base_address + len(code) - 1:08X}",
        )
        self.assertEqual(summary["caller_return_counts"][0]["invocation_count"], 1)
        self.assertIn(
            "title_text_draw_fast_path",
            {event["operation"] for event in result.trace.to_list()},
        )

    def test_guest_thread_receives_title_text_fast_path_for_live_publishing(
        self,
    ) -> None:
        source = Path("tools/playability/playability_probe.py").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "title_text_draw_fast_path=title_text_draw_fast_path,",
            source,
        )
        self.assertIn(
            "title_text_draw_fast_path: TitleTextDrawFastPath,",
            source,
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

    def test_title_world_matrix_audit_captures_singular_input_read_only(self) -> None:
        def f32(value: float) -> int:
            return struct.unpack("<I", struct.pack("<f", value))[0]

        source_address = 0x9000
        matrix_values = (
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            0.0,
            10.0,
            20.0,
            30.0,
            0.0,
        )
        memory_values = {
            0x8000: 0x000860C6,
            0x8004: source_address,
        }
        memory_values.update(
            {
                source_address + index * 4: f32(value)
                for index, value in enumerate(matrix_values)
            }
        )
        memory = SparseMemory(memory_values)
        state = CpuState.with_registers(
            eax=source_address,
            ebx=0x1234,
            ecx=0x00521B28,
            esp=0x8000,
        )
        state.eip = TITLE_WORLD_MATRIX_BUILD_ADDRESS
        watchpoint = RenderWriteWatchpoint()
        watchpoint.flip_count = 42
        audit = TitleWorldMatrixAudit(
            enabled=True,
            render_watchpoint=watchpoint,
        )
        before_state = state.to_dict()
        before_stack_and_matrix = memory.read(0x8000, 8) + memory.read(
            source_address, 0x40
        )

        audit.observer(state, memory, ExecutionTrace(enabled=False), 123)

        self.assertEqual(state.to_dict(), before_state)
        self.assertEqual(
            memory.read(0x8000, 8) + memory.read(source_address, 0x40),
            before_stack_and_matrix,
        )
        summary = audit.summary()
        self.assertEqual(
            audit.observer_addresses,
            {
                TITLE_WORLD_MATRIX_BUILD_ADDRESS,
                TITLE_WORLD_DRAW_CALLBACK_ADDRESS,
                TITLE_D3D_INDEXED_DRAW_ADDRESS,
                TITLE_CAMERA_UPDATE_ADDRESS,
                TITLE_CAMERA_TARGET_FRAME_RETURN_ADDRESS,
                TITLE_CAMERA_UPDATE_RETURN_ADDRESS,
                TITLE_GENERIC_CAMERA_UPDATE_ADDRESS,
                TITLE_GENERIC_CAMERA_TARGET_FRAME_RETURN_ADDRESS,
                *TITLE_GENERIC_CAMERA_UPDATE_RETURN_ADDRESSES,
            },
        )
        self.assertEqual(summary["invocation_count"], 1)
        self.assertEqual(summary["singular_matrix_count"], 1)
        self.assertEqual(summary["source_records"][0]["source_address_hex"], "0x00009000")
        self.assertEqual(
            summary["source_records"][0]["matrix_owner_candidate_hex"],
            "0x00008300",
        )
        self.assertEqual(summary["singular_samples"][0]["guest_flip_count"], 42)
        self.assertIn(
            "degenerate_basis_axis",
            summary["singular_samples"][0]["reasons"],
        )

    def test_title_world_matrix_audit_attributes_zero_index_world_draws(self) -> None:
        def f32(value: float) -> int:
            return struct.unpack("<I", struct.pack("<f", value))[0]

        mesh_entry_address = 0x9000
        owner_address = 0xA000
        index_data_address = 0xB000
        matrix_stack = 0x7F00
        callback_stack = 0x8000
        indexed_stack = 0x8100
        object_address = 0xC000
        mesh_root_address = 0xD000
        matrix_address = 0xE000
        matrix_values = (
            1.0,
            0.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            10.0,
            20.0,
            30.0,
            1.0,
        )
        memory_values = {
            matrix_stack: 0x000860C6,
            matrix_stack + 4: matrix_address,
            callback_stack: 0x000860DC,
            callback_stack + 4: mesh_entry_address,
            mesh_entry_address + 0x0C: index_data_address,
            mesh_entry_address + 0x10: 0,
            owner_address + 0x14: 0,
            object_address + 0xCEC: mesh_root_address,
            mesh_root_address + 0x28: 1,
            mesh_root_address + 0x38: mesh_entry_address,
            indexed_stack: TITLE_D3D_INDEXED_DRAW_WRAPPER_RETURN_ADDRESS,
            indexed_stack + 4: 6,
            indexed_stack + 8: 0,
            indexed_stack + 12: index_data_address,
            indexed_stack + 0x10: 0x000C4AA3,
        }
        memory_values.update(
            {
                matrix_address + index * 4: f32(value)
                for index, value in enumerate(matrix_values)
            }
        )
        memory = SparseMemory(
            memory_values
        )
        watchpoint = RenderWriteWatchpoint()
        watchpoint.flip_count = 44
        audit = TitleWorldMatrixAudit(
            enabled=True,
            render_watchpoint=watchpoint,
        )
        state = CpuState.with_registers(
            ebx=object_address,
            ecx=owner_address,
            esi=0x34,
            edi=1,
            esp=callback_stack,
        )
        state.set_register("esp", matrix_stack)
        state.eip = TITLE_WORLD_MATRIX_BUILD_ADDRESS
        audit.observer(state, memory, ExecutionTrace(enabled=False), 90)
        state.set_register("esp", callback_stack)
        state.eip = TITLE_WORLD_DRAW_CALLBACK_ADDRESS
        before_memory = memory.read(callback_stack, 0x1200)

        audit.observer(state, memory, ExecutionTrace(enabled=False), 100)
        state.set_register("esp", indexed_stack)
        state.eip = TITLE_D3D_INDEXED_DRAW_ADDRESS
        audit.observer(state, memory, ExecutionTrace(enabled=False), 110)

        self.assertEqual(memory.read(callback_stack, 0x1200), before_memory)
        indexed = audit.summary()["indexed_draw_audit"]
        self.assertEqual(indexed["world_draw_callback_count"], 1)
        self.assertEqual(indexed["world_draw_callback_zero_index_count"], 1)
        self.assertEqual(indexed["indexed_draw_count"], 1)
        self.assertEqual(indexed["zero_index_draw_count"], 1)
        self.assertEqual(
            indexed["indexed_draw_caller_counts"][0],
            {
                "caller_return_address_hex": "0x000C4AA3",
                "invocation_count": 1,
                "zero_index_count": 1,
            },
        )
        self.assertEqual(
            indexed["world_draw_callback_samples"][0]["mesh_entry_address_hex"],
            "0x00009000",
        )
        caller_sample = indexed["world_draw_callback_caller_samples"][0]
        self.assertEqual(
            caller_sample["caller_return_address_hex"], "0x000860DC"
        )
        self.assertEqual(caller_sample["mesh_root_address_hex"], "0x0000D000")
        self.assertEqual(
            caller_sample["mesh_entry_index_count_address_hex"], "0x00009010"
        )
        self.assertEqual(caller_sample["registers_hex"]["ebx"], "0x0000C000")
        self.assertEqual(caller_sample["active_matrix_step_delta"], 10)
        self.assertEqual(
            caller_sample["active_matrix"]["source_address_hex"], "0x0000E000"
        )
        self.assertEqual(caller_sample["active_matrix"]["position"], [10.0, 20.0, 30.0])
        self.assertEqual(
            indexed["indexed_draw_samples"][0]["guest_flip_count"], 44
        )

    def test_scene_record_source_loader_reads_prelinked_track_tables(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            extracted_root = Path(temporary)
            path = extracted_root / "trackpss" / "forward" / "test.pss"
            path.parent.mkdir(parents=True)
            image_base = 0x8381C000
            record_address = image_base + 0x100
            payload = bytearray(0x200)
            struct.pack_into("<I", payload, 0x14, image_base + 0x1C0)
            struct.pack_into("<II", payload, 0x18, 1, record_address)
            struct.pack_into(
                "<II", payload, 0x10C, image_base + 0x180, 42
            )
            path.write_bytes(payload)

            sources, errors = _load_title_scene_record_sources(extracted_root)

        self.assertEqual(errors, ())
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].title_path, "trackpss/forward/test.pss")
        primary = sources[0].tables[0]
        self.assertEqual(primary.record_address, record_address)
        self.assertEqual(primary.record_count, 1)
        self.assertEqual(struct.unpack_from("<II", primary.records[0], 0x0C), (
            image_base + 0x180,
            42,
        ))

    def test_scene_record_audit_distinguishes_native_reads_from_ram(self) -> None:
        image_base = 0x9000
        root_address = 0xB000
        record_address = 0x9100
        expected_pointer = 0xA000
        header = bytearray(0x68)
        struct.pack_into("<II", header, 0x18, 1, record_address)
        source_record = bytearray(0x78)
        struct.pack_into("<II", source_record, 0x0C, expected_pointer, 42)
        source = TitleSceneRecordSourceLayout(
            "trackpss/forward/test.pss",
            image_base,
            bytes(header),
            (
                TitleSceneRecordTableLayout(
                    "primary", 1, record_address, (bytes(source_record),)
                ),
            ),
        )
        memory = SparseMemory()
        memory.write_u32(0x004B85B8, root_address)
        memory.write(root_address, header)
        memory.write(record_address, source_record)
        stack = 0x8000
        memory.write_u32(stack, TITLE_D3D_INDEXED_DRAW_WRAPPER_RETURN_ADDRESS)
        memory.write_u32(stack + 0x08, 0)
        memory.write_u32(stack + 0x0C, 0)
        memory.write_u32(stack + 0x10, 0x000C3C4A)
        memory.write_u32(stack + 0x20, 0x00091158)
        memory.write_u32(stack + 0x24, record_address)
        watchpoint = RenderWriteWatchpoint()
        watchpoint.flip_count = 50
        audit = TitleSceneRecordAudit(
            enabled=True,
            source_layouts=(source,),
            render_watchpoint=watchpoint,
        )
        state = CpuState.with_registers(esp=stack)
        state.eip = TITLE_D3D_INDEXED_DRAW_ADDRESS

        audit.observer(state, memory, ExecutionTrace(enabled=False), 123)

        summary = audit.summary()
        self.assertEqual(summary["status"], "native_read_divergence")
        self.assertEqual(summary["argument_memory_divergence_count"], 1)
        self.assertEqual(summary["runtime_source_divergence_count"], 0)
        self.assertTrue(summary["complete"])
        sample = summary["record_samples"][0]
        self.assertEqual(sample["passed_index_count"], 0)
        self.assertEqual(sample["authoritative_index_count"], 42)
        self.assertEqual(sample["expected_pairs"][0]["index_count"], 42)

        report = build_title_scene_record_audit_report(
            {
                "entry_recovery": {
                    "execution": {
                        "guest_thread_executions": [
                            {"title_scene_record_audit": summary}
                        ]
                    }
                }
            }
        )
        self.assertEqual(report["status"], "native_read_divergence")
        self.assertEqual(report["scene_draw_count"], 1)

    def test_scene_record_audit_tracks_runtime_source_corruption_and_writes(self) -> None:
        image_base = 0x9000
        root_address = 0xB000
        record_address = 0x9100
        header = bytearray(0x68)
        struct.pack_into("<II", header, 0x18, 1, record_address)
        source_record = bytearray(0x78)
        struct.pack_into("<II", source_record, 0x0C, 0xA000, 42)
        source = TitleSceneRecordSourceLayout(
            "trackpss/forward/test.pss",
            image_base,
            bytes(header),
            (
                TitleSceneRecordTableLayout(
                    "primary", 1, record_address, (bytes(source_record),)
                ),
            ),
        )
        memory = SparseMemory()
        memory.write_u32(0x004B85B8, root_address)
        memory.write(root_address, header)
        memory.write(record_address, bytes(0x78))
        stack = 0x8000
        memory.write_u32(stack, TITLE_D3D_INDEXED_DRAW_WRAPPER_RETURN_ADDRESS)
        memory.write_u32(stack + 0x10, 0x000C3C4A)
        memory.write_u32(stack + 0x20, 0x00091BED)
        memory.write_u32(stack + 0x24, record_address)
        audit = TitleSceneRecordAudit(enabled=True, source_layouts=(source,))
        state = CpuState.with_registers(esp=stack)
        state.eip = TITLE_D3D_INDEXED_DRAW_ADDRESS

        audit.observer(state, memory, ExecutionTrace(enabled=False), 200)
        audit.observe_memory_write(
            memory,
            source="host_model",
            instruction_address=None,
            write_address=record_address + 0x0C,
            payload=struct.pack("<II", 0xA000, 42),
            steps=None,
        )
        memory.write(record_address + 0x0C, struct.pack("<II", 0xA000, 42))
        audit.observe_memory_write(
            memory,
            source="native_page_writeback",
            instruction_address=None,
            write_address=record_address + 0x0C,
            payload=bytes(8),
            steps=None,
        )

        summary = audit.summary()
        self.assertEqual(summary["status"], "runtime_record_corruption")
        self.assertEqual(summary["runtime_source_divergence_count"], 1)
        self.assertEqual(summary["write_audit"]["transition_counts"], {
            "initialized": 2,
            "cleared": 2,
        })
        self.assertEqual(
            summary["write_audit"]["first_clearing_write"]["source"],
            "native_page_writeback",
        )

    def test_title_world_matrix_audit_captures_exact_singularity_writes(self) -> None:
        source_address = 0x9000
        matrix = struct.pack(
            "<16f",
            1.0,
            0.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            10.0,
            20.0,
            30.0,
            0.0,
        )
        memory = SparseMemory({source_address: matrix})
        watchpoint = RenderWriteWatchpoint()
        watchpoint.flip_count = 43
        audit = TitleWorldMatrixAudit(
            render_watchpoint=watchpoint,
            write_watch_address=source_address,
        )

        audit.observe_memory_write(
            memory,
            instruction_address=0x00081234,
            write_address=source_address,
            size=4,
            value=0,
            steps=456,
        )

        self.assertEqual(memory.read(source_address, 0x40), matrix)
        self.assertEqual(
            audit.memory_callback_addresses,
            set(range(source_address, source_address + 0x40, 4)),
        )
        summary = audit.summary()["write_audit"]
        self.assertTrue(summary["enabled"])
        self.assertEqual(summary["address_hex"], "0x00009000")
        self.assertEqual(summary["write_count"], 1)
        self.assertEqual(summary["singularizing_write_count"], 1)
        self.assertEqual(
            summary["instruction_counts"][0]["instruction_address_hex"],
            "0x00081234",
        )
        sample = summary["transition_samples"][0]
        self.assertEqual(sample["guest_flip_count"], 43)
        self.assertEqual(sample["transition"], "became_singular")
        self.assertFalse(sample["before"]["singular"])
        self.assertTrue(sample["after"]["singular"])

    def test_title_world_matrix_audit_pairs_rotation_input_and_output(self) -> None:
        source_address = 0x9000
        axis_address = 0xA000
        stack_address = 0x8000
        return_address = min(TITLE_WORLD_MATRIX_ROTATE_RETURN_ADDRESSES)
        healthy_matrix = struct.pack(
            "<16f",
            1.0,
            0.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            10.0,
            20.0,
            30.0,
            0.0,
        )
        singular_matrix = struct.pack(
            "<16f",
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            0.0,
            10.0,
            20.0,
            30.0,
            0.0,
        )
        angle_raw = struct.unpack("<I", struct.pack("<f", 12.5))[0]
        memory = SparseMemory(
            {
                source_address: healthy_matrix,
                axis_address: struct.pack("<3f", 0.0, 0.0, 1.0),
                stack_address: struct.pack(
                    "<5I",
                    return_address,
                    source_address,
                    axis_address,
                    angle_raw,
                    1,
                ),
            }
        )
        state = CpuState.with_registers(esp=stack_address)
        state.eip = TITLE_WORLD_MATRIX_ROTATE_ADDRESS
        state.fpu_stack = [3.0, 2.0]
        audit = TitleWorldMatrixAudit(write_watch_address=source_address)
        trace = ExecutionTrace(enabled=False)

        audit.observer(state, memory, trace, 500)
        builder_stack_address = 0x8100
        one_minus_cosine_raw = struct.unpack(
            "<I", struct.pack("<f", 1.0 - math.cos(math.radians(30.0)))
        )[0]
        sine_raw = struct.unpack(
            "<I", struct.pack("<f", math.sin(math.radians(30.0)))
        )[0]
        memory.write(
            builder_stack_address,
            struct.pack(
                "<6I",
                TITLE_WORLD_MATRIX_ROTATION_BUILD_RETURN_ADDRESS,
                source_address,
                axis_address,
                one_minus_cosine_raw,
                sine_raw,
                1,
            ),
        )
        state.set_register("esp", builder_stack_address)
        state.eip = TITLE_WORLD_MATRIX_ROTATION_BUILD_ADDRESS
        audit.observer(state, memory, trace, 550)
        memory.write(source_address, singular_matrix)
        state.eip = return_address
        audit.observer(state, memory, trace, 600)

        summary = audit.summary()["rotation_audit"]
        self.assertTrue(summary["enabled"])
        self.assertEqual(summary["entry_count"], 1)
        self.assertEqual(summary["completion_count"], 1)
        self.assertEqual(summary["pending_count"], 0)
        self.assertEqual(summary["singular_input_count"], 0)
        self.assertEqual(summary["singular_output_count"], 1)
        sample = summary["samples"][0]
        self.assertEqual(sample["axis"], [0.0, 0.0, 1.0])
        self.assertEqual(sample["angle_degrees"], 12.5)
        self.assertEqual(sample["combine_mode"], 1)
        self.assertEqual(sample["entry_fpu"]["stack_depth"], 2)
        self.assertEqual(sample["builder"]["normalized_axis"], [0.0, 0.0, 1.0])
        self.assertAlmostEqual(sample["builder"]["sine"], 0.5)
        self.assertLess(sample["builder"]["unit_circle_error"], 1.0e-6)
        self.assertFalse(sample["input"]["singular"])
        self.assertTrue(sample["output"]["singular"])

    def test_legacy_title_frontend_asset_seed_is_not_registered_over_real_init(self) -> None:
        object_address = 0x00400000
        fast_path = TitleFrontendAssetInitFastPath()
        state = CpuState.with_registers(ecx=object_address, esp=0x8000)
        memory = SparseMemory({0x8000: 0xFEEDFACE})
        trace = ExecutionTrace()

        self.assertNotIn(TITLE_FRONTEND_ASSET_INIT_ADDRESS, fast_path.call_handlers())
        self.assertNotIn(
            TITLE_FRONTEND_ASSET_METHOD_TRAMPOLINE_ADDRESS,
            fast_path.call_handlers(),
        )
        self.assertNotIn(
            TITLE_FRONTEND_ASSET_SECOND_METHOD_TRAMPOLINE_ADDRESS,
            fast_path.call_handlers(),
        )
        self.assertIn(
            TITLE_FRONTEND_SYNTHETIC_ASSET_METHOD_TARGET_ADDRESS,
            fast_path.call_handlers(),
        )
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
            TITLE_FRONTEND_SYNTHETIC_ASSET_METHOD_TARGET_ADDRESS,
            ExecutionTrace(),
        )
        self.assertEqual(method_state.get_register("eax"), 1)
        self.assertEqual(fast_path.summary()["method_invocation_count"], 1)
        fast_path.method_handler(
            method_state,
            memory,
            TITLE_FRONTEND_SYNTHETIC_ASSET_METHOD_TARGET_ADDRESS,
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

    def test_title_heap_large_bin_integrity_repair_leaves_healthy_list_untouched(
        self,
    ) -> None:
        repair = TitleHeapLargeBinIntegrityRepair()
        heap_base = 0x10000000
        sentinel = heap_base + TITLE_HEAP_LARGE_BIN_SENTINEL_OFFSET
        node = 0x828A2458
        watched = (sentinel, sentinel + 4, node - 8, node, node + 4)
        memory = SparseMemory(
            {
                sentinel: node,
                sentinel + 4: node,
                node - 8: 0x100,
                node: sentinel,
                node + 4: sentinel,
            }
        )
        before = memory.snapshot_u32(watched)
        state = CpuState.with_registers(ebx=heap_base, ecx=node)
        state.eip = 0x000E497D

        self.assertFalse(repair.service_native_slice(state, memory, 20_000))

        self.assertEqual(memory.snapshot_u32(watched), before)
        self.assertEqual(state.eip, 0x000E497D)
        summary = repair.summary()
        self.assertEqual(summary["observation_count"], 1)
        self.assertEqual(summary["healthy_count"], 1)
        self.assertEqual(summary["repair_count"], 0)

    def test_title_heap_large_bin_integrity_repair_removes_observed_zero_size_link(
        self,
    ) -> None:
        repair = TitleHeapLargeBinIntegrityRepair()
        heap_base = 0x10000000
        sentinel = heap_base + TITLE_HEAP_LARGE_BIN_SENTINEL_OFFSET
        node = 0x828A2458
        memory = SparseMemory(
            {
                sentinel: node,
                sentinel + 4: node,
                node - 8: 0,
                node: node,
                node + 4: node,
            }
        )
        state = CpuState.with_registers(ebx=heap_base, ecx=node)
        state.eip = 0x000E497D

        self.assertTrue(repair.service_native_slice(state, memory, 130_680_000))

        self.assertEqual(memory.read_u32(sentinel), sentinel)
        self.assertEqual(memory.read_u32(sentinel + 4), sentinel)
        self.assertEqual(state.eip, 0x000E4978)
        self.assertEqual(state.get_register("ecx"), sentinel)
        summary = repair.summary()
        self.assertEqual(summary["repair_count"], 1)
        self.assertEqual(summary["termination_counts"], {"invalid_chunk_size": 1})
        self.assertEqual(summary["repairs"][0]["invalid_chunk_size"], 0)
        self.assertEqual(summary["repairs"][0]["preserved_node_count"], 0)
        self.assertTrue(summary["repairs"][0]["rerouted_to_loop_head"])

    def test_title_heap_large_bin_integrity_repair_batches_all_scan_loops(
        self,
    ) -> None:
        heap_base = 0x10000000
        sentinel = heap_base + TITLE_HEAP_LARGE_BIN_SENTINEL_OFFSET
        node = 0x828A2458
        cases = (
            ("allocation", 0x000E499D, 0x000E4978, "ecx"),
            ("coalesce_insert_a", 0x000E4AE4, 0x000E4ACA, "eax"),
            ("coalesce_insert_b", 0x000E4BC5, 0x000E4BAB, "eax"),
            ("coalesce_insert_c", 0x000E4D17, 0x000E4CFD, "eax"),
            ("free_insert", 0x000E5087, 0x000E506F, "ecx"),
        )
        for scan_kind, instruction, loop_entry, cursor_register in cases:
            with self.subTest(scan_kind=scan_kind):
                repair = TitleHeapLargeBinIntegrityRepair()
                memory = SparseMemory(
                    {
                        sentinel: node,
                        sentinel + 4: node,
                        node - 8: 0x100,
                        node: node,
                        node + 4: node,
                    }
                )
                state = CpuState.with_registers(
                    ebx=heap_base,
                    eax=node,
                    ecx=node,
                    esi=sentinel,
                )
                state.eip = instruction

                self.assertTrue(
                    repair.service_native_slice(state, memory, 40_000)
                )

                self.assertEqual(memory.read_u32(sentinel), node)
                self.assertEqual(memory.read_u32(sentinel + 4), node)
                self.assertEqual(memory.read_u32(node), sentinel)
                self.assertEqual(memory.read_u32(node + 4), sentinel)
                self.assertEqual(state.eip, loop_entry)
                self.assertEqual(state.get_register(cursor_register), sentinel)
                summary = repair.summary()
                self.assertEqual(summary["termination_counts"], {"cycle": 1})
                self.assertEqual(summary["repair_counts_by_scan"], {scan_kind: 1})
                self.assertEqual(summary["repairs"][0]["preserved_node_count"], 1)

    def test_title_heap_large_bin_integrity_repair_restores_backlinks_without_reroute(
        self,
    ) -> None:
        repair = TitleHeapLargeBinIntegrityRepair()
        heap_base = 0x10000000
        sentinel = heap_base + TITLE_HEAP_LARGE_BIN_SENTINEL_OFFSET
        node = 0x828A2458
        memory = SparseMemory(
            {
                sentinel: node,
                sentinel + 4: 0,
                node - 8: 0x100,
                node: sentinel,
                node + 4: node,
            }
        )
        state = CpuState.with_registers(
            ebx=heap_base,
            ecx=node,
            esi=sentinel,
        )
        state.eip = 0x000E506F

        self.assertTrue(repair.service_native_slice(state, memory, 60_000))

        self.assertEqual(memory.read_u32(sentinel + 4), node)
        self.assertEqual(memory.read_u32(node + 4), sentinel)
        self.assertEqual(state.eip, 0x000E506F)
        self.assertEqual(state.get_register("ecx"), node)
        summary = repair.summary()
        self.assertEqual(summary["termination_counts"], {"backlink_mismatch": 1})
        self.assertFalse(summary["repairs"][0]["rerouted_to_loop_head"])

    def test_title_heap_large_bin_integrity_repair_does_not_mutate_at_scan_limit(
        self,
    ) -> None:
        repair = TitleHeapLargeBinIntegrityRepair(max_nodes=1)
        heap_base = 0x10000000
        sentinel = heap_base + TITLE_HEAP_LARGE_BIN_SENTINEL_OFFSET
        first = 0x828A2458
        second = 0x828A3458
        watched = (sentinel, sentinel + 4, first, first + 4, second, second + 4)
        memory = SparseMemory(
            {
                sentinel: first,
                sentinel + 4: second,
                first - 8: 0x100,
                first: second,
                first + 4: sentinel,
                second - 8: 0x100,
                second: sentinel,
                second + 4: first,
            }
        )
        before = memory.snapshot_u32(watched)
        state = CpuState.with_registers(ebx=heap_base, ecx=first)
        state.eip = 0x000E497D

        self.assertFalse(repair.service_native_slice(state, memory, 80_000))

        self.assertEqual(memory.snapshot_u32(watched), before)
        self.assertEqual(state.eip, 0x000E497D)
        summary = repair.summary()
        self.assertEqual(summary["scan_limit_count"], 1)
        self.assertEqual(summary["repair_count"], 0)

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

    def test_title_global_list_fast_path_leaves_cleanup_to_guest_code(self) -> None:
        fast_path = TitleGlobalListRegistrationFastPath()

        self.assertNotIn(
            TITLE_GLOBAL_LIST_CLEANUP_ADDRESS,
            fast_path.call_handlers(),
        )
        self.assertIn(
            TITLE_GLOBAL_LIST_REGISTER_ADDRESS,
            fast_path.call_handlers(),
        )
        summary = fast_path.summary()
        self.assertFalse(summary["cleanup_fast_path_enabled"])

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

    def test_native_gpu_idle_pump_uses_seeded_nv2a_base_and_returns(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "56578BF98B37"
                "F68614320000107412"
                "F68600240000107409"
                "F6862032000010742C"
                "8BCFE83EF9FFFF"
                "83BE00014000007407"
                "8BCFE87EF8FFFF"
                "F786000100000000000174C2"
                "8BCFE85BF2FFFFEBB9"
                "8B54240C8D86203200008B085F890AC700000000005E"
                "F6001075FBC20400"
            ),
            base_address=0x0021DD78,
            symbol="gpu_idle_pump_seeded_nv2a_base",
        )
        context_address = TITLE_D3D_CONTEXT_SYNTHETIC_ADDRESS
        gpu_service_address = (
            context_address + TITLE_D3D_CONTEXT_NV2A_BASE_OFFSET
        )
        output_address = 0x9000

        memory = XbeBackedSparseMemory(load_xbe_bytes(_synthetic_xbe()[0]))
        self.assertEqual(
            memory.read_u32(TITLE_D3D_CONTEXT_GLOBAL_ADDRESS),
            context_address,
        )
        memory.write_u32(0x8000, 0)
        memory.write_u32(0x8004, output_address)
        state = CpuState.with_registers(
            ecx=gpu_service_address,
            esi=0x11223344,
            edi=0x55667788,
            esp=0x8000,
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
            )
            returned_to = executor.run(state, memory, max_steps=64)

        self.assertEqual(returned_to, 0)
        self.assertEqual(memory.read_u32(output_address), 0)
        self.assertEqual(state.get_register("esi"), 0x11223344)
        self.assertEqual(state.get_register("edi"), 0x55667788)
        hardware = memory.title_hardware_completion_summary()
        self.assertEqual(hardware["pfifo_cache1_status_read_count"], 1)
        self.assertEqual(hardware["pfifo_runout_status_read_count"], 1)
        self.assertEqual(
            memory.read_u32(gpu_service_address),
            TITLE_NV2A_MMIO_BASE_ADDRESS,
        )

    def test_gpu_idle_pump_uses_normal_native_slice_cadence(self) -> None:
        self.assertNotIn(
            TITLE_GPU_IDLE_PUMP_LOOP_BRANCH,
            _title_native_fast_paths(),
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
        self.assertEqual(invocation.handler_arguments, invocation.arguments)
        self.assertEqual(invocation.stack_cleanup_bytes, 16)
        self.assertEqual(memory.read_u32(0x00225208), 0x00400101)
        self.assertEqual(
            invocation.memory_writes[0]["label"],
            "encoder_option_result",
        )

    def test_runtime_abi_bridge_cleans_ke_wait_dispatcher_arguments(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[159])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        function = lift_x86_function(
            _push_u32(0)
            + _push_u32(0)
            + _push_u32(1)
            + _push_u32(6)
            + _push_u32(0x0022704C)
            + _call_indirect_bytes(0x3000)
            + b"\xC3",
            base_address=0x1000,
            symbol="ke_wait_for_single_object_callsite",
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
        self.assertEqual(invocation.shim_name, "KeWaitForSingleObject")
        self.assertEqual(
            invocation.arguments,
            (0x0022704C, 6, 1, 0, 0),
        )
        self.assertEqual(invocation.handler_arguments, invocation.arguments)
        self.assertEqual(invocation.stack_cleanup_bytes, 20)
        self.assertEqual(invocation.eax, XboxStatus.WAIT_0)

    def test_runtime_abi_bridge_cleans_av_set_display_mode_arguments(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[3])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        arguments = (
            0xFD000000,
            0,
            0x040F0D0F,
            0x12,
            0xA00,
            0x21D8D000,
        )
        function = lift_x86_function(
            b"".join(_push_u32(argument) for argument in reversed(arguments))
            + _call_indirect_bytes(0x3000)
            + b"\xC3",
            base_address=0x1000,
            symbol="av_set_display_mode_callsite",
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
        self.assertEqual(invocation.shim_name, "AvSetDisplayMode")
        self.assertEqual(invocation.arguments, arguments)
        self.assertEqual(invocation.handler_arguments, arguments)
        self.assertEqual(invocation.stack_cleanup_bytes, 24)
        self.assertEqual(invocation.eax, XboxStatus.SUCCESS)
        self.assertEqual(runtime.graphics.display_mode.width, 640)

    def test_runtime_abi_bridge_cleans_nt_wait_ex_arguments(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[193, 234])
        bridge = RuntimeAbiBridge(runtime)
        semaphore = runtime.nt_create_semaphore(initial_count=1, limit=1)
        target = next(
            shim.target_address
            for shim in runtime.registered_shims
            if shim.name == "NtWaitForSingleObjectEx"
        )
        arguments = (semaphore, 1, 0, 0)
        function = lift_x86_function(
            b"".join(_push_u32(argument) for argument in reversed(arguments))
            + _call_indirect_bytes(0x3000)
            + b"\xC3",
            base_address=0x1000,
            symbol="nt_wait_for_single_object_ex_callsite",
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
        self.assertEqual(invocation.shim_name, "NtWaitForSingleObjectEx")
        self.assertEqual(invocation.arguments, arguments)
        self.assertEqual(invocation.handler_arguments, arguments)
        self.assertEqual(invocation.stack_cleanup_bytes, 16)
        self.assertEqual(invocation.eax, XboxStatus.WAIT_0)

    def test_runtime_abi_bridge_defers_three_argument_guest_delay(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[99])
        bridge = RuntimeAbiBridge(runtime)
        bridge.defer_guest_thread_delays = True
        target = next(
            shim.target_address
            for shim in runtime.registered_shims
            if shim.name == "KeDelayExecutionThread"
        )
        interval_address = 0x7000
        interval_100ns = -10_000_000
        arguments = (1, 0, interval_address)
        function = lift_x86_function(
            b"".join(_push_u32(argument) for argument in reversed(arguments))
            + _call_indirect_bytes(0x3000)
            + b"\xC3",
            base_address=0x1000,
            symbol="ke_delay_execution_thread_callsite",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x3000: target, 0x9000: 0xDEADC0DE})
        memory.write(
            interval_address,
            interval_100ns.to_bytes(8, "little", signed=True),
        )

        before = runtime.clock.snapshot()["interrupt_time_100ns"]
        result = execute_lifted_function(
            function,
            state=state,
            memory=memory,
            call_handlers=bridge.call_handlers(),
        )
        after = runtime.clock.snapshot()["interrupt_time_100ns"]

        invocation = bridge.invocations[0]
        wait = _cooperative_wait_plan(
            invocation,
            current_interrupt_time_100ns=before,
        )
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertEqual(invocation.arguments, arguments)
        self.assertEqual(invocation.handler_arguments, (interval_100ns,))
        self.assertEqual(invocation.stack_cleanup_bytes, 12)
        self.assertEqual(after, before)
        self.assertEqual(wait["kind"], "deadline")
        self.assertEqual(wait["wake_interrupt_time_100ns"], 10_000_000)

    def test_cooperative_runtime_wait_resumes_only_after_signal(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[193, 234])
        bridge = RuntimeAbiBridge(runtime)
        semaphore = runtime.nt_create_semaphore(initial_count=0, limit=1)
        target = next(
            shim.target_address
            for shim in runtime.registered_shims
            if shim.name == "NtWaitForSingleObjectEx"
        )
        arguments = (semaphore, 1, 0, 0)
        function = lift_x86_function(
            b"".join(_push_u32(argument) for argument in reversed(arguments))
            + _call_indirect_bytes(0x3000)
            + b"\xC3",
            base_address=0x1000,
            symbol="cooperative_nt_wait_for_single_object_ex",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x3000: target, 0x9000: 0xDEADC0DE})
        result = execute_lifted_function(
            function,
            state=state,
            memory=memory,
            call_handlers=bridge.call_handlers(),
        )
        wait = _cooperative_wait_plan(
            bridge.invocations[0],
            current_interrupt_time_100ns=0,
        )
        session = {"state": result.state, "wait": wait}

        self.assertFalse(_resume_cooperative_wait(session, runtime))
        runtime.nt_release_semaphore(semaphore, 1)
        self.assertTrue(_resume_cooperative_wait(session, runtime))
        self.assertEqual(result.state.get_register("eax"), XboxStatus.WAIT_0)
        self.assertEqual(
            runtime.sync.wait_for_single_object(semaphore, timeout_100ns=0),
            XboxStatus.WAIT_TIMEOUT,
        )

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

    def test_runtime_abi_bridge_cleans_phy_initialize_arguments(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[253])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        return_address = 0x000E7294
        arguments = (0, 0)
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory({0x8000: return_address})
        for index, argument in enumerate(arguments):
            memory.write_u32(0x8004 + index * 4, argument)

        bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "PhyInitialize")
        self.assertEqual(invocation.arguments, arguments)
        self.assertEqual(invocation.handler_arguments, ())
        self.assertEqual(invocation.stack_cleanup_bytes, 8)
        self.assertEqual(state.get_register("eax"), XboxStatus.SUCCESS)
        self.assertEqual(state.get_register("esp"), 0x8008)
        self.assertEqual(memory.read_u32(0x8008), return_address)

    def test_runtime_abi_bridge_cleans_phy_get_link_state_argument(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[252])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        return_address = 0x000E729B
        arguments = (0,)
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory({0x8000: return_address})
        memory.write_u32(0x8004, arguments[0])

        bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "PhyGetLinkState")
        self.assertEqual(invocation.arguments, arguments)
        self.assertEqual(invocation.handler_arguments, ())
        self.assertEqual(invocation.stack_cleanup_bytes, 4)
        self.assertEqual(state.get_register("eax"), XboxStatus.SUCCESS)
        self.assertEqual(state.get_register("esp"), 0x8004)
        self.assertEqual(memory.read_u32(0x8004), return_address)

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
            path = Path(temp_dir) / "decoded-blocks.sqlite3"
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
            cache.close()

            loaded_cache = DynamicBlockCache(path)
            cached = loaded_cache.get(key)
            backend = loaded_cache.summary()["backend"]
            loaded_cache.close()
            audit_record_count, audit_seeds = _dynamic_block_cache_seed_addresses(path)

        self.assertIsNotNone(cached)
        assert cached is not None
        self.assertEqual(cached.symbol, "dynamic_block_00001000")
        self.assertEqual(cached.instructions[0].mnemonic, "mov")
        self.assertEqual(cached.instructions[0].operands[1].immediate, 7)
        self.assertEqual(backend, "sqlite-zlib-json-blob")
        self.assertEqual(audit_record_count, 1)
        self.assertEqual(audit_seeds, {0x1000})

    def test_dynamic_block_cache_refreshes_legacy_decoder_semantics(self) -> None:
        legacy_function = LiftedFunction(
            symbol="dynamic_block_00001000",
            base_address=0x1000,
            code_size=5,
            instructions=(
                X86Instruction(
                    address=0x1000,
                    size=2,
                    mnemonic="fnstcw",
                    operands=(Operand.memory(size=16),),
                ),
                X86Instruction(
                    address=0x1002,
                    size=2,
                    mnemonic="fnstcw",
                    operands=(Operand.memory(size=16),),
                ),
                X86Instruction(address=0x1004, size=1, mnemonic="ret"),
            ),
        )
        legacy_key = "1:ABCDEF:0x00001000:512:224"
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "decoded-blocks.sqlite3"
            legacy_path = Path(temp_dir) / "dynamic-block-cache.json"
            empty_store = DynamicBlockCache(path)
            empty_store.close()
            legacy_path.write_text(
                json.dumps(
                    {
                        "format": "b2-recomp-dynamic-block-cache",
                        "public_safe": False,
                        "version": 1,
                        "records": {
                            legacy_key: legacy_function.to_dict(
                                include_bytes=False
                            )
                        },
                    }
                ),
                encoding="utf-8",
            )
            loaded = type(
                "Loaded",
                (),
                {"arena": SparseMemory({0x1000: bytes.fromhex("D9FED9FFC3")})},
            )()
            cache = DynamicBlockCache(path, legacy_json_path=legacy_path)

            cache.prepare_for_image(loaded, image_sha256="ABCDEF")
            current_key = cache.key(
                image_sha256="ABCDEF",
                target=0x1000,
                entry_bytes=0x200,
                max_block_instructions=224,
            )
            refreshed = cache.get(current_key)
            cache.save()
            summary = cache.summary()
            sqlite_prefix = path.read_bytes()[:16]
            legacy_backups = list(
                legacy_path.parent.glob(f"{legacy_path.name}.legacy-v1.json*")
            )
            cache.close()

        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(
            [instruction.mnemonic for instruction in refreshed.instructions],
            ["fsin", "fcos", "ret"],
        )
        self.assertTrue(summary["migration_performed"])
        self.assertEqual(summary["migration_validated_record_count"], 1)
        self.assertEqual(summary["migration_decode_count"], 1)
        self.assertEqual(summary["migration_refreshed_record_count"], 1)
        self.assertEqual(summary["migration_rejected_record_count"], 0)
        self.assertEqual(
            [
                change["refreshed_mnemonic"]
                for change in summary["migration_samples"][0][
                    "instruction_changes"
                ]
            ],
            ["fsin", "fcos"],
        )
        self.assertTrue(sqlite_prefix.startswith(b"SQLite format 3"))
        self.assertEqual(len(legacy_backups), 1)
        self.assertEqual(summary["record_count"], 1)
        self.assertEqual(summary["migration_source_path"], str(legacy_path))
        self.assertEqual(summary["migration_backup_path"], str(legacy_backups[0]))

    def test_dynamic_block_cache_prunes_least_recently_used_records(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("C3"),
            base_address=0x1000,
            symbol="pruned_dynamic_block",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = DynamicBlockCache(
                Path(temp_dir) / "decoded-blocks.sqlite3",
                max_records=1,
            )
            first_key = cache.key(
                image_sha256="ABCDEF",
                target=0x1000,
                entry_bytes=512,
                max_block_instructions=224,
            )
            second_key = cache.key(
                image_sha256="ABCDEF",
                target=0x2000,
                entry_bytes=512,
                max_block_instructions=224,
            )
            cache.put(first_key, function)
            cache.put(second_key, function)
            cache.save()
            summary = cache.summary()
            remaining = [cache.get(first_key), cache.get(second_key)]
            cache.close()

        self.assertEqual(summary["record_count"], 1)
        self.assertEqual(summary["pruned_records"], 1)
        self.assertEqual(sum(item is not None for item in remaining), 1)

    def test_dynamic_block_cache_recovers_from_preserved_legacy_backup_once(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("40C3"),
            base_address=0x2000,
            symbol="legacy_recovery_block",
        )
        legacy_key = "2:ABCDEF:0x00002000:512:224"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "decoded-blocks.sqlite3"
            backup = root / "dynamic-block-cache.json.legacy-v2.json"
            empty_store = DynamicBlockCache(path)
            empty_store.close()
            backup.write_text(
                json.dumps(
                    {
                        "format": "b2-recomp-dynamic-block-cache",
                        "version": 2,
                        "records": {legacy_key: function.to_dict()},
                    }
                ),
                encoding="utf-8",
            )
            cache = DynamicBlockCache(
                path,
                legacy_json_path=backup,
                preserve_legacy_source=True,
            )
            cache.prepare_for_image(object(), image_sha256="ABCDEF")
            current_key = cache.key(
                image_sha256="ABCDEF",
                target=0x2000,
                entry_bytes=512,
                max_block_instructions=224,
            )
            recovered = cache.get(current_key)
            summary = cache.summary()
            cache.close()
            marker = DynamicBlockCache.metadata_value(
                path,
                DynamicBlockCache.LEGACY_RECOVERY_MARKER,
            )
            backup_preserved = backup.exists()

        self.assertIsNotNone(recovered)
        self.assertTrue(backup_preserved)
        self.assertTrue(summary["migration_source_preserved"])
        self.assertGreaterEqual(summary["max_records"], 131072)
        self.assertEqual(marker, "1")

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

    def test_runtime_abi_bridge_compares_guest_ansi_strings(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[279])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        state = CpuState.with_registers(esp=0x8000)
        left = b"TDATA"
        right = b"tdata"
        memory = SparseMemory(
            {
                0x6000: (
                    len(left).to_bytes(2, "little")
                    + len(left).to_bytes(2, "little")
                    + (0x7000).to_bytes(4, "little")
                ),
                0x6010: (
                    len(right).to_bytes(2, "little")
                    + len(right).to_bytes(2, "little")
                    + (0x7100).to_bytes(4, "little")
                ),
                0x7000: left,
                0x7100: right,
                0x8000: 0xDEADC0DE,
                0x8004: 0x6000,
                0x8008: 0x6010,
                0x800C: 1,
            }
        )

        bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "RtlEqualString")
        self.assertEqual(invocation.stack_cleanup_bytes, 12)
        self.assertTrue(invocation.result)
        self.assertEqual(state.get_register("eax"), 1)

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
        self.assertEqual(memory.read_u32(0x6214), 2)

    def test_runtime_abi_bridge_opens_or_creates_save_directory(self) -> None:
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
            path = b"\\Device\\Harddisk0\\Partition1\\TDATA"
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
                    0x8008: 0x00100001,
                    0x800C: 0x6100,
                    0x8010: 0x6210,
                    0x8014: 0,
                    0x8018: 0,
                    0x801C: 3,
                    0x8020: 3,
                    0x8024: 0x00004021,
                    0x8028: 0,
                    0x802C: 0,
                }
            )

            bridge.invoke(state, memory, target, ExecutionTrace())

            self.assertTrue((save_root / "TDATA").is_dir())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "NtCreateFile")
        self.assertEqual(invocation.eax, XboxStatus.SUCCESS)
        self.assertTrue(invocation.result["is_directory"])
        self.assertFalse(invocation.result["created"])
        self.assertEqual(memory.read_u32(0x6200), invocation.result["handle"])
        self.assertEqual(memory.read_u32(0x6210), XboxStatus.SUCCESS)
        self.assertEqual(memory.read_u32(0x6214), 1)

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
        self.assertTrue(
            _guest_thread_requested_live_stop(
                {
                    "status": "live_stop",
                    "native_run": {"reason": "unhandled_target"},
                }
            )
        )
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
                guest_metrics_provider=lambda: {
                    "compiled_blocks": 37,
                    "invalidations": 4,
                },
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
            self.assertEqual(published["guest_compiled_blocks"], 37)
            self.assertEqual(published["guest_invalidations"], 4)
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
            with patch(
                "tools.playability.playability_probe._read_text_file_shared",
                side_effect=PermissionError("controller snapshot is being replaced"),
            ):
                self.assertFalse(bridge.sample_controller(force=True))
            self.assertEqual(runtime.input.poll_controller(0).buttons, 0x1000)
            self.assertEqual(bridge.summary()["controller_read_defer_count"], 1)
            self.assertTrue(bridge.sample_controller(force=True))
            self.assertEqual(runtime.input.poll_controller(0).buttons, 0x2000)
            self.assertEqual(bridge.summary()["controller_update_count"], 2)

            polls_before = bridge.summary()["controller_poll_count"]
            self.assertFalse(bridge.sample_controller())
            self.assertEqual(bridge.summary()["controller_poll_count"], polls_before)
            self.assertGreater(bridge.summary()["controller_poll_skip_count"], 0)

            controller_path.write_text('{"stop":true}', encoding="utf-8")
            bridge.controller_mtime_ns = -1
            bridge.next_controller_poll_time = 0.0
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

    def test_controller_snapshot_reader_uses_windows_delete_sharing(self) -> None:
        source = Path("tools/playability/playability_probe.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("_WINDOWS_FILE_SHARE_READ", source)
        self.assertIn("_WINDOWS_FILE_SHARE_WRITE", source)
        self.assertIn("_WINDOWS_FILE_SHARE_DELETE", source)
        self.assertIn("kernel.CreateFileW", source)
        self.assertIn("json.loads(_read_text_file_shared", source)

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "controller.json"
            payload = '{"ports":{"0":{"connected":true}}}'
            path.write_text(payload, encoding="utf-8")
            self.assertEqual(_read_text_file_shared(path), payload)

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
                frontend_text_provider=lambda flip: (
                    {
                        "frontend_text": "Loading - please wait",
                        "frontend_text_x_bits": 0x43A00000,
                        "frontend_text_y_bits": 0x43AA0000,
                        "frontend_text_size_bits": 0x41C80000,
                        "frontend_text_color_argb": 0x80FFFFFF,
                    }
                    if flip == 1
                    else None
                ),
            )

            watchpoint.observe(0x80000000, flip_header.to_bytes(4, "little"))
            watchpoint.observe(0x80000004, (1).to_bytes(4, "little"))
            watchpoint.observe(0xFED00000, (1).to_bytes(4, "little"))
            bridge.on_slice(CpuState(), memory, 100)

            first = json.loads(render_path.read_text(encoding="utf-8"))
            self.assertEqual(first["guest_flip_count"], 1)
            self.assertEqual(first["presentable_command_record_count"], 3)
            self.assertEqual(first["frontend_text"], "Loading - please wait")
            self.assertEqual(first["frontend_text_x_bits"], 0x43A00000)
            self.assertEqual(first["frontend_text_y_bits"], 0x43AA0000)
            self.assertEqual(first["frontend_text_size_bits"], 0x41C80000)
            self.assertEqual(first["frontend_text_color_argb"], 0x80FFFFFF)
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
            self.assertNotIn("frontend_text", second)
            self.assertEqual(bridge.summary()["render_publish_count"], 2)
            self.assertEqual(
                second["resource_stream_generation"],
                first["resource_stream_generation"],
            )
            self.assertEqual(
                second["resource_snapshot_path"],
                first["resource_snapshot_path"],
            )
            self.assertTrue(second["resource_snapshots_unchanged"])
            self.assertEqual(bridge.summary()["render_resource_scan_count"], 1)

    def test_live_host_bridge_scans_only_changed_resource_pages(self) -> None:
        runtime = XboxRuntimeShims()
        watchpoint = RenderWriteWatchpoint()
        source_address = 0x80004000
        memory = SparseMemory({source_address: b"\x01\x02"})
        binding = (0, 0x00004000, 0x00000500, 0)
        watchpoint._texture_bindings.append(binding)
        watchpoint._texture_binding_seen.add(binding)
        watchpoint.resource_binding_generation += 1
        watchpoint.observe(0xFED00000, bytes.fromhex("01000000"))

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            render_path = root / "render.json"
            bridge = LiveHostBridge(
                runtime,
                watchpoint,
                render_stream_path=render_path,
                controller_state_path=root / "controller.json",
            )

            self.assertTrue(bridge.publish_render(memory, force=True))
            first = json.loads(render_path.read_text(encoding="utf-8"))
            self.assertTrue(bridge.publish_render(memory, force=True))
            unchanged = json.loads(render_path.read_text(encoding="utf-8"))
            self.assertEqual(bridge.summary()["render_resource_scan_count"], 1)
            self.assertEqual(
                unchanged["resource_stream_generation"],
                first["resource_stream_generation"],
            )
            self.assertTrue(unchanged["resource_snapshots_unchanged"])

            memory.write(source_address, b"\x03\x04")
            self.assertTrue(bridge.publish_render(memory, force=True))
            changed = json.loads(render_path.read_text(encoding="utf-8"))

            summary = bridge.summary()
            self.assertEqual(summary["render_resource_scan_count"], 2)
            self.assertEqual(summary["render_resource_dirty_check_count"], 2)
            self.assertEqual(summary["render_resource_dirty_count"], 1)
            self.assertNotEqual(
                changed["resource_stream_generation"],
                first["resource_stream_generation"],
            )

    def test_live_host_bridge_detects_cached_resource_page_changes(self) -> None:
        runtime = XboxRuntimeShims()
        watchpoint = RenderWriteWatchpoint()
        memory = SparseMemory({0x80004000: b"TEXTURE"})

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bridge = LiveHostBridge(
                runtime,
                watchpoint,
                render_stream_path=root / "render.json",
                controller_state_path=root / "controller.json",
            )
            source_address = 0x80004000
            generations = (
                memory.page_generation(source_address & ~memory._PAGE_MASK),
            )
            bridge._resource_snapshot_cache[
                (0, 0x00004000, 0x00000600, 0)
            ] = (
                generations,
                {
                    "address": 0x00004000,
                    "source_address": source_address,
                    "byte_count": 7,
                },
            )

            self.assertFalse(bridge._cached_render_resource_changed(memory))
            memory.write(source_address, b"texture")
            self.assertTrue(bridge._cached_render_resource_changed(memory))

    def test_diagnostic_history_retains_startup_and_recent_tail(self) -> None:
        history = _BoundedDiagnosticHistory(first_limit=2, recent_limit=3)
        for index in range(10):
            history.append({"run": index})

        self.assertEqual(
            [record["run"] for record in history.records()],
            [0, 1, 7, 8, 9],
        )
        self.assertEqual(
            history.summary(),
            {
                "total_count": 10,
                "retained_count": 5,
                "dropped_count": 5,
                "first_limit": 2,
                "recent_limit": 3,
            },
        )

    def test_live_host_bridge_rotates_acknowledged_command_epochs(self) -> None:
        runtime = XboxRuntimeShims()
        watchpoint = RenderWriteWatchpoint(retain_diagnostic_writes=False)
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
                presentation_ack_path=root / "presented.bin",
                render_publish_interval_seconds=0.0,
                command_epoch_record_limit=3,
            )
            self.assertTrue(bridge.direct_command_transport_enabled)

            with patch.object(
                bridge,
                "_wait_for_presentation_ack",
                return_value=True,
            ):
                watchpoint.observe(0x80000000, flip_header.to_bytes(4, "little"))
                watchpoint.observe(0x80000004, (1).to_bytes(4, "little"))
                watchpoint.observe(0xFED00000, (1).to_bytes(4, "little"))
                # Native batching may cross a completed flip before yielding.
                # This next-frame write must seed, not invalidate, the next epoch.
                watchpoint.observe(0x80000008, (0xDEADBEEF).to_bytes(4, "little"))
                bridge.on_slice(CpuState(), memory, 100)

                first = json.loads(render_path.read_text(encoding="utf-8"))
                first_path = Path(first["command_snapshot_path"])
                self.assertEqual(first["command_snapshot_base_record_count"], 0)
                self.assertEqual(first["command_snapshot_record_count"], 4)
                self.assertEqual(first["presentable_command_record_count"], 3)
                self.assertEqual(first_path.read_bytes()[:8], b"B2SPAN01")
                self.assertEqual(first["command_snapshot_byte_count"], 80)
                self.assertEqual(first["presentable_command_byte_count"], 60)
                self.assertEqual(first["command_snapshot_span_count"], 4)
                self.assertEqual(first_path.stat().st_size, 88)

                watchpoint.observe(0x8000000C, flip_header.to_bytes(4, "little"))
                watchpoint.observe(0x80000010, (2).to_bytes(4, "little"))
                watchpoint.observe(0xFED00004, (1).to_bytes(4, "little"))
                bridge.on_slice(CpuState(), memory, 200)

                second = json.loads(render_path.read_text(encoding="utf-8"))
                second_path = Path(second["command_snapshot_path"])
                self.assertEqual(second["command_stream_generation"], first["command_stream_generation"])
                self.assertEqual(second["command_snapshot_base_record_count"], 3)
                self.assertEqual(second["command_snapshot_record_count"], 4)
                self.assertEqual(second["presentable_command_record_count"], 7)
                self.assertEqual(second_path.read_bytes()[:8], b"B2SPAN01")
                self.assertEqual(second["command_snapshot_base_byte_count"], 60)
                self.assertEqual(second["command_snapshot_byte_count"], 80)
                self.assertEqual(second["presentable_command_byte_count"], 140)
                self.assertEqual(second["command_snapshot_base_span_count"], 3)
                self.assertEqual(second["command_snapshot_span_count"], 4)
                self.assertEqual(second_path.stat().st_size, 88)
                self.assertFalse(first_path.exists())

            summary = bridge.summary()
            self.assertEqual(summary["command_epoch_rotation_count"], 2)
            self.assertEqual(summary["command_epoch_reuse_count"], 0)
            self.assertEqual(summary["command_epoch_record_limit"], 3)
            self.assertEqual(summary["command_epoch_peak_resident_record_count"], 4)
            self.assertEqual(summary["retired_snapshot_delete_count"], 1)
            self.assertEqual(summary["direct_command_receive_record_count"], 7)
            self.assertEqual(summary["direct_command_receive_span_count"], 7)
            self.assertEqual(summary["direct_command_receive_payload_byte_count"], 28)
            self.assertEqual(len(watchpoint.live_command_records), 0)

    def test_live_host_bridge_reuses_command_epoch_below_record_limit(self) -> None:
        runtime = XboxRuntimeShims()
        watchpoint = RenderWriteWatchpoint(retain_diagnostic_writes=False)
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
                presentation_ack_path=root / "presented.bin",
                render_publish_interval_seconds=0.0,
                command_epoch_record_limit=8,
            )

            with patch.object(
                bridge,
                "_wait_for_presentation_ack",
                return_value=True,
            ):
                watchpoint.observe(0x80000000, flip_header.to_bytes(4, "little"))
                watchpoint.observe(0x80000004, (1).to_bytes(4, "little"))
                watchpoint.observe(0xFED00000, (1).to_bytes(4, "little"))
                bridge.on_slice(CpuState(), memory, 100)
                first = json.loads(render_path.read_text(encoding="utf-8"))

                watchpoint.observe(0x80000008, flip_header.to_bytes(4, "little"))
                watchpoint.observe(0x8000000C, (2).to_bytes(4, "little"))
                watchpoint.observe(0xFED00004, (1).to_bytes(4, "little"))
                bridge.on_slice(CpuState(), memory, 200)
                second = json.loads(render_path.read_text(encoding="utf-8"))

            self.assertEqual(
                second["command_snapshot_path"],
                first["command_snapshot_path"],
            )
            self.assertEqual(second["command_snapshot_base_record_count"], 0)
            self.assertEqual(second["command_snapshot_record_count"], 6)
            self.assertEqual(second["presentable_command_record_count"], 6)
            self.assertEqual(second["command_epoch"], 0)
            self.assertEqual(second["command_epoch_record_limit"], 8)
            self.assertEqual(second["command_snapshot_byte_count"], 120)
            self.assertEqual(second["command_snapshot_span_count"], 6)
            self.assertEqual(
                Path(second["command_snapshot_path"]).stat().st_size,
                128,
            )

            summary = bridge.summary()
            self.assertEqual(summary["command_epoch_rotation_count"], 0)
            self.assertEqual(summary["command_epoch_reuse_count"], 2)
            self.assertEqual(summary["command_epoch_resident_record_count"], 6)
            self.assertEqual(summary["command_epoch_peak_resident_record_count"], 6)

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
                manifest = json.loads(_read_text_file_shared(render_path))
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
