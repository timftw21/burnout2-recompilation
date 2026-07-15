#!/usr/bin/env python3
"""Milestone 7 boot/playability probe with runtime shim ABI dispatch."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import inspect
import itertools
import json
import math
import os
import struct
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, BinaryIO

try:
    from runtime.xbox.shims import (
        ControllerState,
        RuntimeShim,
        XboxRuntimeConfig,
        XboxRuntimeError,
        XboxRuntimeShims,
        XboxStatus,
    )
    from tools.loader.xbe_loader import (
        ImportResolver,
        LoadedXbeImage,
        XbeMemoryAccessError,
        load_xbe_file,
    )
    from tools.playability.host_audio import (
        PcmClip,
        WindowsPcmOutput,
        parse_rws_pcm,
        parse_rws_xbox_adpcm,
    )
    from tools.recomp.native_executor import NativeExecutorError
    from tools.recomp.x86_lifter import (
        CpuState,
        ExecutionTrace,
        ExecutionResult,
        LiftedFunction,
        Operand,
        SparseMemory,
        X86Instruction,
        X86DecodeError,
        X86ExecutionError,
        execute_lifted_function,
        lift_x86_block,
        lift_x86_function,
    )
    from tools.render.d3d8_stream import (
        extract_render_streams_from_probe_summary,
        write_json,
    )
    from tools.xbe.xbe_info import parse_xbe_file
except ModuleNotFoundError:  # pragma: no cover - direct script execution fallback
    import sys

    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from runtime.xbox.shims import (
        ControllerState,
        RuntimeShim,
        XboxRuntimeConfig,
        XboxRuntimeError,
        XboxRuntimeShims,
        XboxStatus,
    )
    from tools.loader.xbe_loader import (
        ImportResolver,
        LoadedXbeImage,
        XbeMemoryAccessError,
        load_xbe_file,
    )
    from tools.playability.host_audio import (
        PcmClip,
        WindowsPcmOutput,
        parse_rws_pcm,
        parse_rws_xbox_adpcm,
    )
    from tools.recomp.native_executor import NativeExecutorError
    from tools.recomp.x86_lifter import (
        CpuState,
        ExecutionTrace,
        ExecutionResult,
        LiftedFunction,
        Operand,
        SparseMemory,
        X86Instruction,
        X86DecodeError,
        X86ExecutionError,
        execute_lifted_function,
        lift_x86_block,
        lift_x86_function,
    )
    from tools.render.d3d8_stream import (
        extract_render_streams_from_probe_summary,
        write_json,
    )
    from tools.xbe.xbe_info import parse_xbe_file


DEFAULT_ENTRY_BYTES = 0x200
DEFAULT_MAX_INSTRUCTIONS = 128
DEFAULT_MAX_BLOCK_INSTRUCTIONS = 1024
DEFAULT_MAX_STEPS = 256
DEFAULT_MAX_THREAD_STEPS = 327680
DEFAULT_INTERNAL_DEPTH = 15
DEFAULT_MAX_RECOVERED_BLOCKS = 1024
DEFAULT_MAX_DYNAMIC_BLOCKS = 65536
DEFAULT_MAX_GUEST_ARGUMENTS = 16
DEFAULT_MAX_GUEST_THREAD_EXECUTIONS = 8
DEFAULT_RENDER_STREAM_MAX_WRITES = 65536
DEFAULT_RUNTIME_ABI_HISTORY = 4096
DEFAULT_STACK_BASE = 0x70000000
DEFAULT_THREAD_STACK_BASE = 0x71000000
DEFAULT_FS_BASE = 0x72000000
THREAD_STACK_STRIDE = 0x00010000
THREAD_FS_SELF_POINTER_OFFSET = 0x20
THREAD_FS_CALLBACK_TABLE_OFFSET = 0x250
BOOT_PROBE_RETURN = 0xB2000000
BOOT_THREAD_RETURN_BASE = 0xB2100000
TITLE_SPIN_DELAY_ADDRESS = 0x0021AB00
TITLE_SPIN_DELAY_ITERATIONS = 400
TITLE_SPIN_DELAY_TSC_ADVANCE = TITLE_SPIN_DELAY_ITERATIONS
TITLE_STATIC_DRIVE_ARRAY_SETUP_ADDRESS = 0x000D9570
TITLE_STATIC_DRIVE_ARRAY_DESCRIPTOR_ADDRESS = 0x00295B14
TITLE_STATIC_DRIVE_ARRAY_OBJECT_ADDRESS = 0x00303C70
TITLE_STATIC_DRIVE_ARRAY_ELEMENT_BASE_ADDRESS = 0x00489F70
TITLE_STATIC_DRIVE_ARRAY_ELEMENT_SIZE = 0x50
TITLE_STATIC_DRIVE_ARRAY_REGISTRY_OBJECTS_ADDRESS = 0x00598234
TITLE_STATIC_DRIVE_ARRAY_REGISTRY_DESCRIPTORS_ADDRESS = 0x0059823C
TITLE_STATIC_DRIVE_ARRAY_REGISTRY_COUNT_ADDRESS = 0x00598248
TITLE_STATIC_DRIVE_ARRAY_MAX_ELEMENTS = 16
TITLE_STATIC_DRIVE_ARRAY_SETUP_STACK_CLEANUP = 16

SCHEDULER_PRODUCER_ANCHORS = {
    0x00127E9B: {
        "semantic": "initial_node_key_seed",
        "block_entry_hex": "0x00127E8C",
        "instruction_text": "mov [esi + 0x8], eax",
        "field": "work_item.key",
        "field_offset_hex": "0x00000008",
        "source": "0x005B8EDC",
        "hle_role": "seeds the observed scheduler node key field before runtime enqueue activity",
    },
    0x00127ED2: {
        "semantic": "initial_node_pointer_byte_seed",
        "block_entry_hex": "0x00127ECC",
        "instruction_text": "mov [eax + esi + 0x1C], cl",
        "field": "work_item.next_byte",
        "hle_role": "seeds a byte inside the watched scheduler node pointer area",
    },
    0x000F29F6: {
        "semantic": "queue_slot_replacement_writer",
        "block_entry_hex": "0x000F29DE",
        "instruction_text": "mov [esi + ebx*4], edi",
        "field": "watched scheduler slot",
        "hle_role": "latest observed scheduler slot replacement before wait",
    },
    0x000F2A63: {
        "semantic": "work_item_key_writer",
        "block_entry_hex": "0x000F2A59",
        "instruction_text": "mov [eax + 0x8], ecx",
        "field": "work_item.key",
        "field_offset_hex": "0x00000008",
        "source_instruction_address_hex": "0x000F2A5B",
        "source_instruction_text": "mov ecx, [esp + 0x24]",
        "source_stack_offset_hex": "0x00000024",
        "hle_role": "writes queued work-item key from scheduler argument",
    },
    0x000F2AAB: {
        "semantic": "work_item_next_clear",
        "block_entry_hex": "0x000F2AA0",
        "instruction_text": "mov [eax + 0x30], ebp",
        "field": "work_item.next",
        "field_offset_hex": "0x00000030",
        "hle_role": "initializes work-item next pointer before linking",
    },
    0x000F2AF9: {
        "semantic": "work_item_next_link",
        "block_entry_hex": "0x000F2AF6",
        "instruction_text": "mov [edx + 0x30], eax",
        "field": "work_item.next",
        "field_offset_hex": "0x00000030",
        "hle_role": "links the queued work item into the observed scheduler list",
    },
}

TITLE_HEAP_FREE_LIST_LOOP_ENTRY = 0x000E506F
TITLE_HEAP_FREE_LIST_LOOP_BRANCH = 0x000E5087
TITLE_GPU_IDLE_PUMP_LOOP_ENTRY = 0x0021DD80
TITLE_GPU_IDLE_PUMP_LOOP_BRANCH = 0x0021DDBC
TITLE_FRONTEND_RESOURCE_LIST_FIND_LOOP_ENTRY = 0x000EB4E5
TITLE_FRONTEND_RESOURCE_LIST_FIND_LOOP_BRANCH = 0x000EB4FE
TITLE_FRONTEND_GLOBAL_RESOURCE_LIST_ADDRESS = 0x00443EE8
TITLE_FRONTEND_GLOBAL_DIC_PATH_ADDRESS = 0x002C4BE4
TITLE_FRONTEND_GLOBAL_DIC_IMPACT2_KEY_ADDRESS = 0x002B3430
TITLE_FRONTEND_ASSET_INIT_ADDRESS = 0x000B9030
TITLE_FRONTEND_SYNTHETIC_ASSET_MANAGER_ADDRESS = 0x31F00000
TITLE_FRONTEND_ASSET_METHOD_TRAMPOLINE_ADDRESS = 0x000E8450
TITLE_FRONTEND_ASSET_SECOND_METHOD_TRAMPOLINE_ADDRESS = 0x000E8440
TITLE_FRONTEND_SYNTHETIC_ASSET_METHOD_TARGET_ADDRESS = 0x31F0E000
TITLE_FRONTEND_SYNTHETIC_BOOT_RESOURCE_HANDLE_ADDRESS = 0x31F01000
TITLE_FRONTEND_SYNTHETIC_GLOBAL_DIC_HANDLE_ADDRESS = 0x31F02000
TITLE_FRONTEND_SYNTHETIC_GLOBAL_DIC_NODE_ADDRESS = 0x31F03000
TITLE_FRONTEND_SYNTHETIC_GLOBAL_DIC_LIST_ADDRESS = (
    TITLE_FRONTEND_SYNTHETIC_GLOBAL_DIC_HANDLE_ADDRESS
)
TITLE_FRONTEND_SYNTHETIC_GLASSB_HANDLE_ADDRESS = 0x31F04000
TITLE_FRONTEND_SYNTHETIC_GLASS_HANDLE_ADDRESS = 0x31F05000
TITLE_ASSET_STREAM_OPEN_ADDRESS = 0x000D8680
TITLE_ASSET_STREAM_SYNTHETIC_OBJECT_ADDRESS = 0x31F10000
TITLE_ASSET_STREAM_SYNTHETIC_VTABLE_ADDRESS = 0x31F10100
TITLE_ASSET_STREAM_SYNTHETIC_ACTIVATE_TARGET_ADDRESS = 0x31F10200
TITLE_ASSET_STREAM_SYNTHETIC_STATUS_TARGET_ADDRESS = 0x31F10300
TITLE_ASSET_STREAM_SYNTHETIC_READ_TARGET_ADDRESS = 0x31F10700
TITLE_ASSET_STREAM_SYNTHETIC_SEEK_TARGET_ADDRESS = 0x31F10800
TITLE_ASSET_STREAM_SYNTHETIC_OBJECT_STRIDE = 0x1000
TITLE_FRONTEND_SPECIAL_AUDIO_CREATE_ADDRESS = 0x000CB690
TITLE_MUSIC_MODE_SET_ADDRESS = 0x000CC2F0
TITLE_MUSIC_SYNTHETIC_MANAGER_ADDRESS = 0x31FE0000
TITLE_MUSIC_MENU_MODE = 2
TITLE_MUSIC_MENU_PATH = Path("music0/trk07menust.rws")
TITLE_FRONTEND_SYNTHETIC_AUDIO_HANDLE_ADDRESS = 0x31F10400
TITLE_FRONTEND_OBJECT_CONSTRUCTOR_ADDRESS = 0x000CB420
TITLE_FRONTEND_SYNTHETIC_CHILD_OBJECT_BASE_ADDRESS = 0x31F06000
TITLE_FRONTEND_SYNTHETIC_CHILD_STATE_BASE_ADDRESS = 0x31F07000
TITLE_FRONTEND_SYNTHETIC_CHILD_DESCRIPTOR_BASE_ADDRESS = 0x31F08000
TITLE_FRONTEND_SYNTHETIC_CHILD_METHOD_TABLE_ADDRESS = 0x31F09000
TITLE_FRONTEND_SYNTHETIC_CHILD_METHOD_TARGET_ADDRESS = 0x31F0A000
TITLE_FRONTEND_SYNTHETIC_CHILD_STRIDE = 0x100
TITLE_FRONTEND_SYNTHETIC_CHILD_DISPATCH_INDEX = 2
TITLE_FRONTEND_SYNTHETIC_CHILD_DISPATCH_INDICES = (
    0,
    1,
    TITLE_FRONTEND_SYNTHETIC_CHILD_DISPATCH_INDEX,
    3,
    4,
    5,
    6,
    8,
    9,
    10,
)
TITLE_FRONTEND_SYNTHETIC_CHILD_OUT_POINTER_DISPATCH_INDICES = (4,)
TITLE_FIXED_WIDTH_COMPARE_ADDRESS = 0x00106BF0
TITLE_FIXED_WIDTH_COMPARE_BYTES = 0x10
TITLE_SUBSYSTEM_INITIALIZER_CALL_ADDRESS = 0x0010A221
TITLE_SUBSYSTEM_INITIALIZER_RESULT_ADDRESS = 0x0010A223
TITLE_SUBSYSTEM_STARTUP_ADDRESS = 0x000CB420
TITLE_SUBSYSTEM_INITIALIZER_DRIVER_ADDRESS = 0x0010A490
TITLE_SUBSYSTEM_INITIALIZER_TABLE_ADDRESS = 0x0010A210
TITLE_FRONTEND_REGISTRY_INITIALIZER_ADDRESS = 0x0010D8A0
TITLE_FRONTEND_REGISTRY_ROOT_PUBLISH_ADDRESS = 0x0010D91C
TITLE_FRONTEND_REGISTRY_LOOKUP_ADDRESS = 0x0010D480
TITLE_FRONTEND_REGISTRY_LOOKUP_RESULT_ADDRESS = 0x0010D4CA
TITLE_FRONTEND_REGISTRY_ATTACH_ADDRESS = 0x0010CF60
TITLE_FRONTEND_REGISTRY_ATTACH_RESULT_ADDRESS = 0x0010CF73
TITLE_FRONTEND_REGISTRY_ATTACH_FAILURE_ADDRESS = 0x0010CF7A
TITLE_FRONTEND_REGISTRY_ATTACH_SUCCESS_ADDRESS = 0x0010CF7D
TITLE_FRONTEND_REGISTRY_TARGET_CLASS_INIT_ADDRESS = 0x00108950
TITLE_FRONTEND_SUBSYSTEM_SHUTDOWN_ADDRESS = 0x0010A4F0
TITLE_FRONTEND_CONSTRUCTOR_RESULT_ADDRESS = 0x000CB534
TITLE_FRONTEND_CONSTRUCTOR_FRONTEND_OBJECT_RESULT_ADDRESS = 0x000CB498
TITLE_FRONTEND_CONSTRUCTOR_DESCRIPTOR_RESULT_ADDRESS = 0x000CB48A
TITLE_FRONTEND_CONSTRUCTOR_FRONTEND_STATE_RESULT_ADDRESS = 0x000CB4B7
TITLE_FRONTEND_CONSTRUCTOR_RESOURCE_OBJECT_RESULT_ADDRESS = 0x000CB505
TITLE_FRONTEND_CONSTRUCTOR_RESOURCE_FAILURE_ADDRESS = 0x000CB50F
TITLE_FRONTEND_CONSTRUCTOR_SUBSYSTEM_FAILURE_ADDRESS = 0x000CB51C
TITLE_FRONTEND_OBJECT_CREATE_FACTORY_RESULT_ADDRESS = 0x0010C6D9
TITLE_FRONTEND_OBJECT_CREATE_INTERFACE_RESULT_ADDRESS = 0x0010C70C
TITLE_FRONTEND_OBJECT_CREATE_FAILURE_ADDRESS = 0x0010C720
TITLE_FRONTEND_OBJECT_CREATE_SUCCESS_ADDRESS = 0x0010C731
TITLE_FRONTEND_DESCRIPTOR_ALLOCATE_ADDRESS = 0x0010CFE0
TITLE_FRONTEND_DEFAULT_FACTORY_ADDRESS = 0x00109910
TITLE_FRONTEND_FACTORY_RESULT_ADDRESS = 0x00109FD9
TITLE_FRONTEND_OBJECT_INITIALIZER_RESULT_ADDRESS = 0x0010A07F
TITLE_FRONTEND_OBJECT_INITIALIZER_FAILURE_ADDRESS = 0x0010A083
TITLE_FRONTEND_OBJECT_INITIALIZER_SUCCESS_ADDRESS = 0x0010A0BF
TITLE_FRONTEND_OBJECT_AUDIO_CREATE_RESULT_ADDRESS = 0x0010AB78
TITLE_FRONTEND_OBJECT_AUDIO_STATE_ALLOC_RESULT_ADDRESS = 0x0010AB93
TITLE_FRONTEND_OBJECT_AUDIO_INIT_RESULT_ADDRESS = 0x0010ABB9
TITLE_MAIN_LOOP_EXIT_FLAG_ADDRESS = 0x0034900C
TITLE_MAIN_LOOP_ENTRY_ADDRESS = 0x00011000
TITLE_MAIN_LOOP_FRAME_ADDRESS = 0x00011036
TITLE_MAIN_LOOP_UPDATE_CALL_ADDRESS = 0x0001115C
TITLE_MAIN_LOOP_CONDITION_ADDRESS = 0x00011161
TITLE_MAIN_LOOP_EXIT_ADDRESS = 0x0001116D
TITLE_MAIN_LOOP_RETURN_ADDRESS = 0x00011171
TITLE_MAIN_LOOP_AUDIT_ADDRESSES = {
    0x0001100C,
    0x00011011,
    0x00011016,
    0x0001101B,
    TITLE_MAIN_LOOP_ENTRY_ADDRESS,
    TITLE_MAIN_LOOP_FRAME_ADDRESS,
    TITLE_MAIN_LOOP_UPDATE_CALL_ADDRESS,
    TITLE_MAIN_LOOP_CONDITION_ADDRESS,
    TITLE_MAIN_LOOP_EXIT_ADDRESS,
    TITLE_MAIN_LOOP_RETURN_ADDRESS,
    0x00013590,
    0x00013793,
    0x00013798,
    0x0001379D,
    0x000137A2,
    0x000137A7,
    0x000137AC,
    0x000137B1,
    0x000137B6,
    0x000137BB,
    0x000137C0,
    0x000137C5,
    0x000137CA,
    0x000137CF,
    0x000137D1,
    0x000137D6,
    0x000137DB,
    0x000137E0,
    0x000137E5,
    0x000137EB,
    0x000137F0,
    0x000137F5,
    0x000137FB,
    0x00013800,
    0x00013805,
    0x0001380A,
    0x0001380F,
    0x00013814,
    0x00013829,
    0x0001382B,
    0x0001383E,
    0x00013843,
    0x00013848,
    0x0001384D,
    0x00013852,
    0x00013857,
    0x0001385C,
    0x00013861,
    0x00013866,
    0x0001386B,
    0x0001386E,
    0x00013873,
    0x00013879,
    0x0001387E,
    0x00013884,
    0x00013889,
    0x00013892,
    0x00013897,
    0x000138B2,
    0x000138B7,
    0x000138CA,
    0x000138CF,
    0x000138D4,
    0x000138D9,
    0x000138E9,
    0x000138EE,
    0x00013910,
    0x00013913,
    0x00013919,
    0x00013924,
    0x00013929,
    0x0001392E,
    0x00013935,
    0x0001393A,
    0x00013948,
    0x000B8760,
    0x000B899C,
    0x000B8A35,
    0x000B8A38,
    0x000B8A3B,
    0x000B9030,
    0x000B91AA,
    0x000B91BC,
    0x000B91C1,
    0x000B91D3,
    0x000B91D8,
    0x000B91E3,
    0x000B91E8,
    0x000B91EE,
    0x000B91F3,
    0x000B91F9,
    0x000B91FE,
    0x000B9204,
    0x000B9209,
    0x000B920E,
    0x000B9216,
    0x000B9219,
    0x000CC3E0,
    0x000CC40E,
    0x000CC413,
    0x000CC420,
    0x000CC425,
    0x000CC431,
    0x000CC436,
    0x000CC484,
    0x000CC489,
    0x000CC48E,
    0x000CC48F,
    0x000CC494,
    0x000CC495,
    0x000CC49A,
    0x000CC544,
    0x000CC549,
    0x000CC558,
    0x000CC55D,
    0x000CC560,
    0x000CC570,
    0x000CC574,
    0x000D4DE0,
    0x000D4DE8,
    0x000D4DED,
    0x000D4E56,
    0x000D4E5B,
    0x000D4E5C,
    0x000D4830,
    0x000D4847,
    0x000D484C,
    0x000D4853,
    0x000D4858,
    0x000D4879,
    0x000D487E,
    0x000D489F,
    0x000D48A4,
    0x000D48AD,
    0x000D48B2,
    0x000D48B7,
    0x000D48BC,
    0x000D494F,
    0x000D4954,
    0x000D4955,
    0x000D495A,
    0x000D4965,
    0x000D4968,
    0x000D496B,
    0x0010C510,
    0x0010C530,
    0x0010C532,
    0x0010C53D,
    0x0010C540,
    0x0010C55F,
    0x0010C561,
    0x0010C56B,
    0x0010C5A0,
    0x0010C5A9,
    0x0010C5D4,
    0x0010C5D9,
    0x0010C5F1,
    0x0010E480,
    0x0010E4B2,
    0x0010E4B7,
    0x0010E4BF,
}
TITLE_DSOUND_INIT_DEVICE_RESULT_ADDRESS = 0x0022FAEA
TITLE_DSOUND_INIT_CAPABILITY_RESULT_ADDRESS = 0x0022FB33
TITLE_DSOUND_INIT_MIXBIN_CREATE_RESULT_ADDRESS = 0x0022FB77
TITLE_DSOUND_INIT_MIXBIN_CONFIGURE_RESULT_ADDRESS = 0x0022FB89
TITLE_DSOUND_INIT_MIXBIN_FINALIZE_RESULT_ADDRESS = 0x0022FB99
TITLE_DSOUND_INIT_EXIT_ADDRESS = 0x0022FBA4
TITLE_DSOUND_DEVICE_CORE_RESULT_ADDRESS = 0x002316EA
TITLE_DSOUND_DEVICE_COMMAND_RESULT_ADDRESS = 0x00231701
TITLE_DSOUND_DEVICE_EVENT_RESULT_ADDRESS = 0x0023176D
TITLE_DSOUND_DEVICE_VOICE_RESULT_ADDRESS = 0x002317AB
TITLE_DSOUND_CORE_MEMORY_RESULT_ADDRESS = 0x00237001
TITLE_DSOUND_CORE_HARDWARE_RESULT_ADDRESS = 0x0023701A
TITLE_DSOUND_HARDWARE_VOICE0_CREATE_RESULT_ADDRESS = 0x0023680E
TITLE_DSOUND_HARDWARE_VOICE0_CONFIG_RESULT_ADDRESS = 0x00236823
TITLE_DSOUND_HARDWARE_VOICE0_START_RESULT_ADDRESS = 0x00236835
TITLE_DSOUND_HARDWARE_VOICE1_CONFIG_RESULT_ADDRESS = 0x00236853
TITLE_DSOUND_HARDWARE_VOICE1_START_RESULT_ADDRESS = 0x00236866
TITLE_FRONTEND_COMPARE_SEARCH_ADDRESS = 0x0010D150
TITLE_FRONTEND_COMPARE_SEARCH_GLOBAL_ROOT_ADDRESS = 0x005B8ECC
TITLE_FRONTEND_COMPARE_SEARCH_CHILD_LIST_OFFSET = 0x10
TITLE_FRONTEND_COMPARE_SEARCH_SYNTHETIC_ROOT_ADDRESS = 0x31F0D000
TITLE_FRONTEND_COMPARE_SEARCH_SYNTHETIC_KEY_ADDRESS = 0x31F0D100
TITLE_FRONTEND_RESOURCE_CACHE_SENTINEL_ADDRESS = 0x005A7058
TITLE_FRONTEND_REGISTRY_LIST_SENTINEL_ADDRESS = 0x005A70A4
TITLE_FRONTEND_POST_AUDIO_LIST_SENTINEL_ADDRESS = 0x005A70E0
TITLE_FRONTEND_INITIALIZER_LIST_SENTINEL_ADDRESS = 0x005A727C
TITLE_FRONTEND_RECORD_TABLE_SCAN_ADDRESS = 0x00112873
TITLE_FRONTEND_RECORD_TABLE_MAX_COUNT = 0x00010000
TITLE_FRONTEND_POST_AUDIO_LIST_ADVANCE_ADDRESS = 0x0010C8AD
TITLE_FRONTEND_STATIC_SINGLETON_USE_ADDRESS = 0x0002A5C0
TITLE_FRONTEND_STATIC_SINGLETON_OBJECT_ADDRESS = 0x004D9F90
TITLE_FRONTEND_STATIC_SINGLETON_VTABLE_ADDRESS = 0x002B4EA8
TITLE_FRONTEND_STATIC_SINGLETON_RECORDS_OFFSET = 0x11C
TITLE_FRONTEND_STATIC_SINGLETON_RECORD_COUNT = 5
TITLE_FRONTEND_STATIC_SINGLETON_RECORD_SIZE = 0x18
TITLE_FRONTEND_STATIC_SINGLETON_AUDIO_OBJECT_ADDRESS = 0x004DABD8
TITLE_FRONTEND_STATIC_SINGLETON_AUDIO_VTABLE_ADDRESS = 0x002B54E0
TITLE_FRONTEND_STATIC_SINGLETON_AUDIO_RECORDS_OFFSET = 0x50
TITLE_FRONTEND_STATIC_SINGLETON_AUDIO_RECORD_COUNT = 9
TITLE_FRONTEND_STATIC_SINGLETON_EFFECT_OBJECT_ADDRESS = 0x004DDB88
TITLE_FRONTEND_STATIC_SINGLETON_EFFECT_VTABLE_ADDRESS = 0x002B6B40
TITLE_FRONTEND_DYNAMIC_OBJECT_OWNER_ADDRESS = 0x004B9C50
TITLE_FRONTEND_DYNAMIC_OBJECT_POINTER_OFFSET = 0x8
TITLE_FRONTEND_DYNAMIC_OBJECT_RESET_USE_ADDRESS = 0x0008281D
TITLE_CRT_CONSTRUCTOR_TABLE_START_ADDRESS = 0x002CD670
TITLE_CRT_CONSTRUCTOR_TABLE_END_ADDRESS = 0x002E98F4
TITLE_FRONTEND_CRT_VTABLE_INITIALIZER_ADDRESS = 0x001392B0
TITLE_FRONTEND_CRT_VTABLE_INITIALIZER_TABLE_ENTRY_ADDRESS = 0x002CF664
TITLE_RUNTIME_OBJECT_TABLE_USE_ADDRESS = 0x0003F660
TITLE_RUNTIME_CALLBACK_DISPATCH_ADDRESSES = (0x00109AD0, 0x00109B40)
TITLE_RUNTIME_CALLBACK_GLOBAL_OBJECT_ADDRESS = 0x005A6EEC
TITLE_RUNTIME_CALLBACK_LIST_OFFSET = 0x0C
TITLE_RUNTIME_CALLBACK_SYNTHETIC_OBJECT_ADDRESS = 0x31F10900
TITLE_RUNTIME_CALLBACK_SYNTHETIC_VTABLE_ADDRESS = 0x31F10980
TITLE_RUNTIME_OBJECT_CONSTRUCTOR_SPECS = (
    (0x004E3610, 0x002B7E30, 0x1F0, 3),
    (0x004E3FB8, 0x002B8198, 0x0AC, 2),
    (0x004E3B10, 0x002B8034, 0x130, 4),
    (0x004E8C70, 0x002B9F74, 0x2DC, 3),
    (0x004EC318, 0x002BAD70, 0x648, 5),
    (0x004ECF08, 0x002BAEE8, 0x190, 5),
    (0x004ED460, 0x002BB054, 0x054, 5),
    (0x004EEAE0, 0x002BB8EC, 0x054, 5),
    (0x004E6710, 0x002B9D74, 0x200C, 5),
    (0x004E6018, 0x002B9BF0, 0x580, 5),
    (0x004EDC58, 0x002BB4A8, 0x244, 9),
    (0x004E9728, 0x002BA254, 0x1A8, 3),
    (0x004E9488, 0x002BA0D8, 0x0A0, 2),
)
TITLE_HEAP_ALLOC_TRAMPOLINE_ADDRESS = 0x000E8720
TITLE_HEAP_FREE_TRAMPOLINE_ADDRESS = 0x000E8730
TITLE_HEAP_FAST_PATH_BASE_ADDRESS = 0x18000000
TITLE_HEAP_FAST_PATH_ALIGNMENT = 0x10
TITLE_HEAP_FAST_PATH_MIN_OBJECT_SIZE = 0x60
TITLE_HEAP_FAST_PATH_MAX_DESCRIPTOR_SIZE = 0x01000000
TITLE_ALLOCATION_LIST_COUNT_ADDRESS = 0x00109BD0
TITLE_ALLOCATION_LIST_SENTINEL_ADDRESS = 0x005A6EBC
TITLE_ALLOCATION_LIST_OWNER_BACK_OFFSET = 0x04
TITLE_ALLOCATION_LIST_MAX_SCAN_NODES = 4096
TITLE_CLEANUP_LIST_SENTINEL_ADDRESS = 0x005A6E0C
TITLE_GLOBAL_LIST_REGISTER_ADDRESS = 0x000E90D0
TITLE_GLOBAL_LIST_CLEANUP_ADDRESS = 0x000F2620
TITLE_GLOBAL_LIST_HEAD_ADDRESS = 0x005ADD5C
TITLE_GLOBAL_LIST_TAIL_ADDRESS = TITLE_GLOBAL_LIST_HEAD_ADDRESS + 4
TITLE_GLOBAL_LIST_NODE_OFFSET = 0x08
TITLE_GLOBAL_LIST_OWNER_OFFSET = 0xA0
TITLE_GLOBAL_LIST_ACTIVE_FLAG = 0x03
TITLE_GLOBAL_LIST_OWNER_FLAG = 0x0C
NV2A_STATUS_POLL_ADDRESS = 0xFD100410
NV2A_STATUS_POLL_BUSY_BIT = 0x00010000
TITLE_GPU_SUBMISSION_BASE_ADDRESS = 0x002256C0
TITLE_GPU_SUBMISSION_LIMIT_ADDRESS = 0x002256C4
TITLE_GPU_COMPLETION_REGISTER_ADDRESS = 0x00800044
TITLE_GPU_COMPLETION_MASK = 0x0FFFFFFF
TITLE_GPU_COMMAND_KICK_ADDRESS = 0x80000000
TITLE_GPU_COMPLETION_DMA_POINTER_OFFSET = 0x17F4
TITLE_GPU_COMPLETION_DMA_STATUS_OFFSET = 0x44
TITLE_GPU_INTERRUPT_STATUS_ADDRESS = 0xFD400100
TITLE_GPU_PFIFO_INTERRUPT_STATUS_ADDRESS = 0xFD002100
TITLE_GPU_PFIFO_RUNOUT_STATUS_ADDRESS = 0xFD002400
TITLE_GPU_PFIFO_CACHE1_STATUS_ADDRESS = 0xFD003214
TITLE_GPU_PFIFO_IDLE_BIT = 0x00000010
TITLE_GPU_PROGRESS_COUNTER_ADDRESS = 0x21B8C000
TITLE_GPU_SOFTWARE_COMPLETION_FLAG_ADDRESS = 0x00100410
TITLE_GPU_SOFTWARE_COMPLETION_PENDING_BIT = 0x00010000
TITLE_MCPX_FRAME_COUNTER_ADDRESS = 0xFE820010
TITLE_MCPX_FRAME_COUNTER_INCREMENT = 0x00000004
TITLE_AUDIO_DSP_CONTROL_ADDRESS = 0xFEC0012C
TITLE_AUDIO_DSP_RESET_REQUEST_BIT = 0x00000002
TITLE_AUDIO_DSP_STATUS_ADDRESS = 0xFEC00130
TITLE_AUDIO_DSP_RESET_READY_BIT = 0x00000100
TITLE_AUDIO_DSP_VOICE_COMMAND_ADDRESSES = (0xFEC0011B, 0xFEC0017B)
TITLE_AUDIO_DSP_VOICE_COMMAND_PENDING_BIT = 0x02
TITLE_D3D_CONTEXT_GLOBAL_ADDRESS = 0x002256B8
TITLE_D3D_CONTEXT_SYNTHETIC_ADDRESS = 0x21B70000
TITLE_D3D_CONTEXT_DMA_STATE_ADDRESS = TITLE_D3D_CONTEXT_SYNTHETIC_ADDRESS + 0x2000
TITLE_D3D_CONTEXT_GET_POINTER_ADDRESS = TITLE_D3D_CONTEXT_SYNTHETIC_ADDRESS + 0x3000
TITLE_DYNAMIC_U32_READ_ADDRESSES = frozenset(
    {
        TITLE_AUDIO_DSP_STATUS_ADDRESS,
        TITLE_MCPX_FRAME_COUNTER_ADDRESS,
        TITLE_GPU_COMPLETION_REGISTER_ADDRESS,
        TITLE_GPU_PFIFO_RUNOUT_STATUS_ADDRESS,
        TITLE_GPU_PFIFO_CACHE1_STATUS_ADDRESS,
        TITLE_GPU_PROGRESS_COUNTER_ADDRESS,
        TITLE_GPU_SOFTWARE_COMPLETION_FLAG_ADDRESS,
        TITLE_GPU_SUBMISSION_BASE_ADDRESS,
        TITLE_GPU_SUBMISSION_LIMIT_ADDRESS,
        TITLE_D3D_CONTEXT_GLOBAL_ADDRESS,
        TITLE_D3D_CONTEXT_GET_POINTER_ADDRESS,
    }
)
TITLE_D3D_CONTEXT_MARKER_QUEUE_ADDRESS = TITLE_D3D_CONTEXT_SYNTHETIC_ADDRESS + 0x4000
TITLE_D3D_CONTEXT_LIST_NODE0_ADDRESS = TITLE_D3D_CONTEXT_SYNTHETIC_ADDRESS + 0x5000
TITLE_D3D_CONTEXT_LIST_NODE1_ADDRESS = TITLE_D3D_CONTEXT_SYNTHETIC_ADDRESS + 0x5020
TITLE_D3D_CONTEXT_SURFACE_STATE_ADDRESS = TITLE_D3D_CONTEXT_SYNTHETIC_ADDRESS + 0x6000
TITLE_D3D_CONTEXT_LIST_COUNT_OFFSET = 0x15F0
TITLE_D3D_CONTEXT_LIST_FIRST_OFFSET = 0x15F4
TITLE_D3D_CONTEXT_LIST_SECOND_OFFSET = 0x15F8
TITLE_D3D_CONTEXT_LIST_SEEDED_COUNT = 2
TITLE_D3D_STATE_DESCRIPTOR_BASE_ADDRESS = 0x00225220
TITLE_D3D_STATE_DESCRIPTOR_STRIDE = 0x80
TITLE_D3D_STATE_DESCRIPTOR_OBSERVED_INDEX = 1
TITLE_D3D_STATE_DESCRIPTOR_SWITCH_OFFSET = 0x30
TITLE_D3D_STATE_DESCRIPTOR_SWITCH_VALUE = 1
TITLE_D3D_PUSH_BUFFER_BASE_ADDRESS = 0x80000000
TITLE_D3D_PUSH_BUFFER_SIZE = 0x00010000
TITLE_D3D_PUSH_BUFFER_END_ADDRESS = (
    TITLE_D3D_PUSH_BUFFER_BASE_ADDRESS + TITLE_D3D_PUSH_BUFFER_SIZE
)
TITLE_D3D_FLUSH_ADDRESS = 0x0021AE80
TITLE_D3D_PACKET_ALLOC_ADDRESS = 0x0021AFD0
TITLE_D3D_PACKET_ALLOC_SIZE = 0x18
TITLE_D3D_RESERVE_ADDRESS = 0x0021B1C0
TITLE_D3D_RESERVE_STACK_CLEANUP = 8
TITLE_D3D_RESERVE_GUARD_BYTES = 0x4000
TITLE_D3D_RESERVE_LIMIT_MARGIN = 0x204
TITLE_D3D_PRIMITIVE_DRAW_ADDRESS = 0x0021CA20
TITLE_D3D_PRIMITIVE_DRAW_STACK_CLEANUP = 0x18
TITLE_D3D_PRIMITIVE_DRAW_ARGUMENT_COUNT = 6
TITLE_D3D_PRIMITIVE_DRAW_SAMPLE_LIMIT = 16
TITLE_IMMEDIATE_DRAW_ADDRESS = 0x000EE1B0
TITLE_IMMEDIATE_DRAW_ARGUMENT_COUNT = 4
TITLE_IMMEDIATE_DRAW_SAMPLE_LIMIT = 32
TITLE_IMMEDIATE_DRAW_MAX_VERTICES = 0x4000
TITLE_TEXT_DRAW_ADDRESS = 0x000BF6F0
TITLE_TEXT_DRAW_STACK_CLEANUP = 0x14
TITLE_TEXT_DRAW_MAX_SAMPLE_BYTES = 128
TITLE_DIRECTSOUND_BUFFER_SYNC_ADDRESS = 0x00230850
TITLE_DIRECTSOUND_EFFECT_IMAGE_ADDRESS = 0x00230ABD
TITLE_DIRECTSOUND_SYNTHETIC_WORKSPACE_ADDRESS = 0x31FF0000
TITLE_QUAD_SUBMIT_ADDRESS = 0x000C2280
TITLE_QUAD_SUBMIT_END_ADDRESS = 0x000C23ED
TITLE_VERTEX_APPEND_ADDRESS = 0x000C20E0
TITLE_VERTEX_APPEND_OBJECT_ADDRESS = 0x0057A448
TITLE_VERTEX_APPEND_COUNT_OFFSET = 0x1C00
TITLE_VERTEX_APPEND_DEFAULT_Z_OFFSET = 0x1C04
TITLE_VERTEX_APPEND_STACK_CLEANUP = 0x14
TITLE_VERTEX_APPEND_STRIDE = 0x1C
TITLE_VERTEX_APPEND_ANOMALY_SAMPLE_LIMIT = 64
TITLE_VERTEX_APPEND_STACK_SCAN_WORDS = 32
TITLE_VERTEX_APPEND_QUAD_PARENT_RETURN_STACK_OFFSET = 0x54
TITLE_VERTEX_APPEND_COUNT_ADDRESS = (
    TITLE_VERTEX_APPEND_OBJECT_ADDRESS + TITLE_VERTEX_APPEND_COUNT_OFFSET
)
TITLE_STARTUP_WORK_QUEUE_LOOP_ENTRY = 0x0010C100
TITLE_STARTUP_WORK_QUEUE_LOOP_BRANCH = 0x0010C112
TITLE_STARTUP_WORK_QUEUE_LINK_PAYLOAD_BACK_OFFSET = 0x14
SCHEDULER_LOOP_CONVERGENCE_ERROR = "scheduler loop convergence detected"
SCHEDULER_LOOP_CONVERGENCE_REPETITIONS = 32
TITLE_XGETDEVICES_ADDRESS = 0x0028D282
TITLE_XINPUT_OPEN_ADDRESS = 0x0028CF40
TITLE_XINPUT_GET_CAPABILITIES_ADDRESS = 0x0028CFA2
TITLE_XINPUT_GET_STATE_ADDRESS = 0x0028D180
TITLE_INPUT_CONSUMER_ADDRESS = 0x000DA080
TITLE_INPUT_CONSUMER_END_ADDRESS = 0x000DA3C0
TITLE_INPUT_PORT_BASE_ADDRESS = 0x00598260
TITLE_INPUT_PORT_STRIDE = 0xC4
TITLE_INPUT_A_VALUE_OFFSET = 0x7C
TITLE_XINPUT_HANDLE_BASE = 0xB2401000
TITLE_XINPUT_ERROR_DEVICE_NOT_CONNECTED = 0x48F

GUEST_ARGUMENT_COUNT_OVERRIDES = {
    "AvSendTVEncoderOption": 4,
    "HalReadWritePCISpace": 6,
    "KeQuerySystemTime": 1,
    "KfLowerIrql": 0,
    "NtAllocateVirtualMemory": 5,
    "NtCreateSemaphore": 4,
    "NtCreateFile": 11,
    "NtDeviceIoControlFile": 10,
    "NtOpenFile": 6,
    "NtOpenSymbolicLinkObject": 2,
    "NtQueryInformationFile": 5,
    "NtQueryDirectoryFile": 10,
    "NtQuerySymbolicLinkObject": 3,
    "NtQueryVolumeInformationFile": 5,
    "NtReadFile": 8,
    "NtReleaseSemaphore": 3,
    "NtSetInformationFile": 5,
    "NtWriteFile": 8,
    "ObReferenceObjectByHandle": 3,
    "ObfDereferenceObject": 0,
    "PsCreateSystemThreadEx": 10,
    "RtlCompareMemoryUlong": 3,
}

FILE_APPEND_DATA = 0x00000004
FILE_WRITE_DATA = 0x00000002
GENERIC_WRITE = 0x40000000
FILE_SUPERSEDE = 0
FILE_CREATE = 2
FILE_OPEN_IF = 3
FILE_OVERWRITE = 4
FILE_OVERWRITE_IF = 5
MEM_COMMIT = 0x00001000


class RuntimeAbiBridgeError(RuntimeError):
    """Raised when a recovered guest call cannot be adapted to a shim handler."""


class BootProbeStop(RuntimeError):
    """Internal sentinel used to stop cleanly at unrecovered code handoffs."""

    def __init__(
        self,
        *,
        target: int,
        stack_args: tuple[int, ...],
        state: CpuState,
        trace: ExecutionTrace,
    ) -> None:
        super().__init__(f"stopped at unrecovered internal call 0x{target:08X}")
        self.target = target
        self.stack_args = stack_args
        self.state = state
        self.trace = trace


class RenderWatchpointStop(RuntimeError):
    """Internal sentinel used to stop after a requested render-write count."""

    def __init__(self, stream: dict[str, Any]) -> None:
        super().__init__("render write watchpoint limit reached")
        self.stream = stream


class TitleSpinDelayFastPath:
    """Fast-path the observed title spin-delay loop without spending step budget."""

    def __init__(
        self,
        *,
        address: int = TITLE_SPIN_DELAY_ADDRESS,
        iterations: int = TITLE_SPIN_DELAY_ITERATIONS,
        tsc_advance: int = TITLE_SPIN_DELAY_TSC_ADVANCE,
    ) -> None:
        self.address = address
        self.iterations = iterations
        self.tsc_advance = tsc_advance
        self.invocation_count = 0

    def call_handlers(
        self,
    ) -> dict[int, Callable[[CpuState, SparseMemory, int, ExecutionTrace], None]]:
        return {self.address: self.call_handler}

    def call_handler(
        self,
        cpu: CpuState,
        _memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
    ) -> None:
        self.invocation_count += 1
        cpu.set_register("eax", 0)
        cpu.timestamp_counter = (
            cpu.timestamp_counter + self.tsc_advance
        ) & 0xFFFFFFFFFFFFFFFF
        cpu.flags.zf = True
        cpu.flags.sf = False
        cpu.flags.pf = True
        cpu.flags.af = False
        cpu.flags.of = False
        trace.add(
            target,
            "title_spin_delay_fast_path",
            iterations=self.iterations,
            invocation_count=self.invocation_count,
            timestamp_counter=cpu.timestamp_counter,
            timestamp_counter_hex=f"0x{cpu.timestamp_counter:016X}",
        )

    def summary(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "address_hex": _hex32(self.address),
            "iterations": self.iterations,
            "timestamp_counter_advance_per_call": self.tsc_advance,
            "invocation_count": self.invocation_count,
        }


class TitleXInputFastPath:
    """Bind the title's XAPILIB 5344 input calls to the runtime input shim."""

    def __init__(self, input_shim: Any) -> None:
        self.input = input_shim
        self.get_devices_count = 0
        self.open_count = 0
        self.capabilities_count = 0
        self.get_state_count = 0
        self.successful_get_state_count = 0
        self.observed_button_mask = 0
        self.a_pressed_poll_count = 0
        self._last_states: dict[int, ControllerState] = {}
        self._packets = [0, 0, 0, 0]

    def call_handlers(
        self,
    ) -> dict[int, Callable[[CpuState, SparseMemory, int, ExecutionTrace], None]]:
        return {
            TITLE_XGETDEVICES_ADDRESS: self.get_devices_handler,
            TITLE_XINPUT_OPEN_ADDRESS: self.open_handler,
            TITLE_XINPUT_GET_CAPABILITIES_ADDRESS: self.capabilities_handler,
            TITLE_XINPUT_GET_STATE_ADDRESS: self.get_state_handler,
        }

    @staticmethod
    def _handle_for_port(port: int) -> int:
        return TITLE_XINPUT_HANDLE_BASE + port

    @staticmethod
    def _port_from_handle(handle: int) -> int | None:
        port = handle - TITLE_XINPUT_HANDLE_BASE
        return port if 0 <= port < 4 else None

    def get_devices_handler(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
    ) -> None:
        states = self.input.poll_all()
        mask = sum(1 << port for port, state in states.items() if state.connected)
        cpu.set_register("eax", mask)
        _prepare_stdcall_return(cpu, memory, 4)
        self.get_devices_count += 1
        trace.add(target, "title_xgetdevices_fast_path", connected_mask=mask)

    def open_handler(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
    ) -> None:
        _device_type, port, _slot, _polling = _read_stack_arguments(
            memory, cpu.get_register("esp"), 4
        )
        state = self.input.poll_controller(port) if port < 4 else ControllerState()
        handle = self._handle_for_port(port) if state.connected else 0
        cpu.set_register("eax", handle)
        _prepare_stdcall_return(cpu, memory, 16)
        self.open_count += 1
        trace.add(
            target,
            "title_xinput_open_fast_path",
            port=port,
            connected=state.connected,
            handle=handle,
            handle_hex=_hex32(handle),
        )

    def capabilities_handler(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
    ) -> None:
        handle, capabilities_address = _read_stack_arguments(
            memory, cpu.get_register("esp"), 2
        )
        port = self._port_from_handle(handle)
        connected = port is not None and self.input.poll_controller(port).connected
        if connected and capabilities_address:
            # The reached title only consumes the standard gamepad capability
            # type/subtype and zeroed optional feedback fields.
            memory.write(capabilities_address, bytes((1, 1)) + bytes(18))
        status = 0 if connected else TITLE_XINPUT_ERROR_DEVICE_NOT_CONNECTED
        cpu.set_register("eax", status)
        _prepare_stdcall_return(cpu, memory, 8)
        self.capabilities_count += 1
        trace.add(
            target,
            "title_xinput_get_capabilities_fast_path",
            port=port,
            status=status,
        )

    @staticmethod
    def _xbox_gamepad_payload(state: ControllerState) -> bytes:
        digital_buttons = state.buttons & 0x00FF
        analog = bytes(
            (
                0xFF if state.buttons & 0x1000 else 0,
                0xFF if state.buttons & 0x2000 else 0,
                0xFF if state.buttons & 0x4000 else 0,
                0xFF if state.buttons & 0x8000 else 0,
                0,
                0,
                state.left_trigger,
                state.right_trigger,
            )
        )
        return struct.pack(
            "<H8Bhhhh",
            digital_buttons,
            *analog,
            state.thumb_lx,
            state.thumb_ly,
            state.thumb_rx,
            state.thumb_ry,
        )

    def get_state_handler(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
    ) -> None:
        handle, state_address = _read_stack_arguments(
            memory, cpu.get_register("esp"), 2
        )
        port = self._port_from_handle(handle)
        state = self.input.poll_controller(port) if port is not None else ControllerState()
        status = 0 if state.connected else TITLE_XINPUT_ERROR_DEVICE_NOT_CONNECTED
        if status == 0 and state_address:
            self.observed_button_mask |= state.buttons
            if state.buttons & 0x1000:
                self.a_pressed_poll_count += 1
            if self._last_states.get(port) != state:
                self._packets[port] = _u32(self._packets[port] + 1)
                self._last_states[port] = state
            memory.write_u32(state_address, self._packets[port])
            memory.write(state_address + 4, self._xbox_gamepad_payload(state))
            self.successful_get_state_count += 1
        cpu.set_register("eax", status)
        _prepare_stdcall_return(cpu, memory, 8)
        self.get_state_count += 1
        trace.add(
            target,
            "title_xinput_get_state_fast_path",
            port=port,
            status=status,
            state_address=state_address,
            state_address_hex=_hex32(state_address),
            buttons=state.buttons,
            packet=self._packets[port] if port is not None else 0,
        )

    def summary(self, memory: SparseMemory | None = None) -> dict[str, Any]:
        port_snapshots = []
        if memory is not None:
            for port in range(4):
                a_address = (
                    TITLE_INPUT_PORT_BASE_ADDRESS
                    + port * TITLE_INPUT_PORT_STRIDE
                    + TITLE_INPUT_A_VALUE_OFFSET
                )
                a_bits = memory.read_u32(a_address)
                port_snapshots.append(
                    {
                        "port": port,
                        "a_value_address_hex": _hex32(a_address),
                        "a_value_bits_hex": _hex32(a_bits),
                        "a_value": struct.unpack("<f", struct.pack("<I", a_bits))[0],
                    }
                )
        return {
            "xgetdevices_address_hex": _hex32(TITLE_XGETDEVICES_ADDRESS),
            "xinput_open_address_hex": _hex32(TITLE_XINPUT_OPEN_ADDRESS),
            "xinput_get_state_address_hex": _hex32(TITLE_XINPUT_GET_STATE_ADDRESS),
            "consumer_address_hex": _hex32(TITLE_INPUT_CONSUMER_ADDRESS),
            "get_devices_count": self.get_devices_count,
            "open_count": self.open_count,
            "capabilities_count": self.capabilities_count,
            "get_state_count": self.get_state_count,
            "successful_get_state_count": self.successful_get_state_count,
            "observed_button_mask_hex": _hex32(self.observed_button_mask),
            "a_pressed_poll_count": self.a_pressed_poll_count,
            "packets": list(self._packets),
            "guest_port_snapshots": port_snapshots,
        }


class TitleGuestHeapFastPath:
    """Model the observed title heap trampolines with unique guest allocations."""

    def __init__(
        self,
        *,
        allocation_address: int = TITLE_HEAP_ALLOC_TRAMPOLINE_ADDRESS,
        free_address: int = TITLE_HEAP_FREE_TRAMPOLINE_ADDRESS,
        base_address: int = TITLE_HEAP_FAST_PATH_BASE_ADDRESS,
        alignment: int = TITLE_HEAP_FAST_PATH_ALIGNMENT,
        min_object_size: int = TITLE_HEAP_FAST_PATH_MIN_OBJECT_SIZE,
        max_descriptor_size: int = TITLE_HEAP_FAST_PATH_MAX_DESCRIPTOR_SIZE,
    ) -> None:
        self.allocation_address = allocation_address
        self.free_address = free_address
        self.base_address = base_address
        self.alignment = alignment
        self.min_object_size = min_object_size
        self.max_descriptor_size = max_descriptor_size
        self._next_address = base_address
        self.allocation_count = 0
        self.free_count = 0
        self.bytes_allocated = 0
        self.allocations: list[dict[str, int | str]] = []
        self.freed_addresses: list[int] = []

    def call_handlers(
        self,
    ) -> dict[int, Callable[[CpuState, SparseMemory, int, ExecutionTrace], None]]:
        return {
            self.allocation_address: self.allocate_handler,
            self.free_address: self.free_handler,
        }

    def allocate_handler(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
    ) -> None:
        esp = cpu.get_register("esp")
        return_address = memory.read_u32(esp)
        descriptor_address = memory.read_u32(esp + 4)
        raw_requested_size = (
            memory.read_u32(descriptor_address) if descriptor_address else 0
        )
        requested_size = (
            raw_requested_size
            if 0 < raw_requested_size <= self.max_descriptor_size
            else 0
        )
        normalized_size = (
            requested_size if requested_size > 0 else max(self.min_object_size, 1)
        )
        allocated_size = max(
            self.alignment,
            _align_up_u32(normalized_size, self.alignment),
        )
        address = _align_up_u32(self._next_address, self.alignment)
        self._next_address = _align_up_u32(address + allocated_size, self.alignment)
        self.allocation_count += 1
        self.bytes_allocated += allocated_size
        allocation = {
            "address": address,
            "address_hex": _hex32(address),
            "return_address": return_address,
            "return_address_hex": _hex32(return_address),
            "descriptor_address": descriptor_address,
            "descriptor_address_hex": _hex32(descriptor_address),
            "requested_size": requested_size,
            "raw_requested_size": raw_requested_size,
            "allocated_size": allocated_size,
        }
        self.allocations.append(allocation)
        cpu.set_register("eax", address)
        trace.add(
            target,
            "title_heap_fast_path_allocate",
            invocation_count=self.allocation_count,
            allocation_address=address,
            allocation_address_hex=_hex32(address),
            return_address=return_address,
            return_address_hex=_hex32(return_address),
            descriptor_address=descriptor_address,
            descriptor_address_hex=_hex32(descriptor_address),
            requested_size=requested_size,
            raw_requested_size=raw_requested_size,
            allocated_size=allocated_size,
        )

    def free_handler(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
    ) -> None:
        esp = cpu.get_register("esp")
        address = memory.read_u32(esp + 8)
        self.free_count += 1
        self.freed_addresses.append(address)
        cpu.set_register("eax", address)
        trace.add(
            target,
            "title_heap_fast_path_free",
            invocation_count=self.free_count,
            allocation_address=address,
            allocation_address_hex=_hex32(address),
        )

    def summary(self) -> dict[str, Any]:
        return {
            "allocation_address": self.allocation_address,
            "allocation_address_hex": _hex32(self.allocation_address),
            "free_address": self.free_address,
            "free_address_hex": _hex32(self.free_address),
            "base_address": self.base_address,
            "base_address_hex": _hex32(self.base_address),
            "next_address": self._next_address,
            "next_address_hex": _hex32(self._next_address),
            "alignment": self.alignment,
            "min_object_size": self.min_object_size,
            "max_descriptor_size": self.max_descriptor_size,
            "allocation_count": self.allocation_count,
            "free_count": self.free_count,
            "bytes_allocated": self.bytes_allocated,
            "recent_allocations": self.allocations[-16:],
            "recent_freed_addresses": [
                _hex32(address) for address in self.freed_addresses[-16:]
            ],
        }


class TitleAllocationListCountFastPath:
    """Fast-count the observed allocation owner list without guest list-walk cost."""

    def __init__(
        self,
        *,
        count_address: int = TITLE_ALLOCATION_LIST_COUNT_ADDRESS,
        sentinel_address: int = TITLE_ALLOCATION_LIST_SENTINEL_ADDRESS,
        owner_back_offset: int = TITLE_ALLOCATION_LIST_OWNER_BACK_OFFSET,
        max_scan_nodes: int = TITLE_ALLOCATION_LIST_MAX_SCAN_NODES,
    ) -> None:
        self.count_address = count_address
        self.sentinel_address = sentinel_address
        self.owner_back_offset = owner_back_offset
        self.max_scan_nodes = max_scan_nodes
        self.invocation_count = 0
        self.total_nodes_scanned = 0
        self.cycle_count = 0
        self.max_node_count = 0
        self.last_summary: dict[str, Any] | None = None

    def call_handlers(
        self,
    ) -> dict[int, Callable[[CpuState, SparseMemory, int, ExecutionTrace], None]]:
        return {self.count_address: self.call_handler}

    def call_handler(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
    ) -> None:
        owner_address = memory.read_u32(_u32(cpu.get_register("esp") + 4))
        node = memory.read_u32(self.sentinel_address)
        visited: set[int] = set()
        visited_order: list[int] = []
        match_count = 0
        terminated_at = "max_scan_nodes"
        while len(visited_order) < self.max_scan_nodes:
            if node == self.sentinel_address:
                terminated_at = "sentinel"
                break
            if node == 0:
                terminated_at = "null"
                break
            if node in visited:
                terminated_at = "cycle"
                self.cycle_count += 1
                break
            visited.add(node)
            visited_order.append(node)
            node_owner = memory.read_u32(_u32(node - self.owner_back_offset))
            if node_owner == owner_address:
                match_count += 1
            node = memory.read_u32(node)

        self.invocation_count += 1
        self.total_nodes_scanned += len(visited_order)
        self.max_node_count = max(self.max_node_count, len(visited_order))
        self.last_summary = {
            "owner_address": owner_address,
            "owner_address_hex": _hex32(owner_address),
            "match_count": match_count,
            "node_count": len(visited_order),
            "terminated_at": terminated_at,
            "last_node_hex": _hex32(node),
            "sampled_nodes_hex": [_hex32(address) for address in visited_order[:16]],
        }
        cpu.set_register("eax", match_count)
        trace.add(
            target,
            "title_allocation_list_count_fast_path",
            invocation_count=self.invocation_count,
            owner_address=owner_address,
            owner_address_hex=_hex32(owner_address),
            match_count=match_count,
            node_count=len(visited_order),
            terminated_at=terminated_at,
            last_node=node,
            last_node_hex=_hex32(node),
            sampled_nodes_hex=[_hex32(address) for address in visited_order[:16]],
        )

    def summary(self) -> dict[str, Any]:
        return {
            "count_address": self.count_address,
            "count_address_hex": _hex32(self.count_address),
            "sentinel_address": self.sentinel_address,
            "sentinel_address_hex": _hex32(self.sentinel_address),
            "owner_back_offset": self.owner_back_offset,
            "owner_back_offset_hex": _hex32(self.owner_back_offset),
            "max_scan_nodes": self.max_scan_nodes,
            "invocation_count": self.invocation_count,
            "total_nodes_scanned": self.total_nodes_scanned,
            "max_node_count": self.max_node_count,
            "cycle_count": self.cycle_count,
            "last_summary": self.last_summary,
        }


class TitleGlobalListRegistrationFastPath:
    """Model the observed title intrusive-list registration edge."""

    def __init__(
        self,
        *,
        register_address: int = TITLE_GLOBAL_LIST_REGISTER_ADDRESS,
        head_address: int = TITLE_GLOBAL_LIST_HEAD_ADDRESS,
        tail_address: int = TITLE_GLOBAL_LIST_TAIL_ADDRESS,
        owner_offset: int = TITLE_GLOBAL_LIST_OWNER_OFFSET,
        node_offset: int = TITLE_GLOBAL_LIST_NODE_OFFSET,
    ) -> None:
        self.register_address = register_address
        self.cleanup_address = TITLE_GLOBAL_LIST_CLEANUP_ADDRESS
        self.head_address = head_address
        self.tail_address = tail_address
        self.owner_offset = owner_offset
        self.node_offset = node_offset
        self.invocation_count = 0
        self.lazy_seed_count = 0
        self.insert_count = 0
        self.skip_count = 0
        self.cleanup_count = 0
        self.cleanup_node_count = 0
        self.registrations: list[dict[str, int | str | bool]] = []
        self.cleanups: list[dict[str, int | str | bool]] = []

    def call_handlers(
        self,
    ) -> dict[int, Callable[[CpuState, SparseMemory, int, ExecutionTrace], None]]:
        return {
            self.register_address: self.register_handler,
            self.cleanup_address: self.cleanup_handler,
        }

    def register_handler(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
    ) -> None:
        esp = cpu.get_register("esp")
        object_address = memory.read_u32(esp + 4)
        owner_address = memory.read_u32(_u32(object_address + self.owner_offset))
        flags = self._read_u8(memory, _u32(owner_address + 3)) if owner_address else 0
        seeded = self._seed_list_if_empty(memory)
        inserted = False
        node_address = _u32(owner_address + self.node_offset) if owner_address else 0
        if owner_address and not flags & TITLE_GLOBAL_LIST_ACTIVE_FLAG:
            previous_head = memory.read_u32(self.head_address)
            memory.write_u32(node_address, previous_head)
            memory.write_u32(_u32(node_address + 4), self.head_address)
            memory.write_u32(_u32(previous_head + 4), node_address)
            memory.write_u32(self.head_address, node_address)
            self._write_u8(
                memory,
                _u32(owner_address + 3),
                flags | TITLE_GLOBAL_LIST_ACTIVE_FLAG,
            )
            self.insert_count += 1
            inserted = True
        elif owner_address:
            self.skip_count += 1

        object_flags = (
            self._read_u8(memory, _u32(object_address + 3)) if object_address else 0
        )
        if object_address:
            self._write_u8(
                memory,
                _u32(object_address + 3),
                object_flags | TITLE_GLOBAL_LIST_OWNER_FLAG,
            )
        cpu.set_register("eax", object_address)
        self.invocation_count += 1
        registration = {
            "target": target,
            "target_hex": _hex32(target),
            "object_address": object_address,
            "object_address_hex": _hex32(object_address),
            "owner_address": owner_address,
            "owner_address_hex": _hex32(owner_address),
            "node_address": node_address,
            "node_address_hex": _hex32(node_address),
            "seeded": seeded,
            "inserted": inserted,
        }
        self.registrations.append(registration)
        trace.add(
            target,
            "title_global_list_register",
            invocation_count=self.invocation_count,
            object_address=object_address,
            object_address_hex=_hex32(object_address),
            owner_address=owner_address,
            owner_address_hex=_hex32(owner_address),
            node_address=node_address,
            node_address_hex=_hex32(node_address),
            seeded=seeded,
            inserted=inserted,
        )

    def cleanup_handler(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
    ) -> None:
        seeded = self._seed_list_if_empty(memory)
        node_count = self._count_list_nodes(memory, limit=64)
        memory.write_u32(self.head_address, self.head_address)
        memory.write_u32(self.tail_address, self.head_address)
        cpu.set_register("eax", 1)
        self.cleanup_count += 1
        self.cleanup_node_count += node_count
        cleanup = {
            "target": target,
            "target_hex": _hex32(target),
            "node_count": node_count,
            "seeded": seeded,
        }
        self.cleanups.append(cleanup)
        trace.add(
            target,
            "title_global_list_cleanup",
            invocation_count=self.cleanup_count,
            node_count=node_count,
            seeded=seeded,
        )

    def _seed_list_if_empty(self, memory: SparseMemory) -> bool:
        if memory.read_u32(self.head_address) != 0:
            return False
        memory.write_u32(self.head_address, self.head_address)
        memory.write_u32(self.tail_address, self.head_address)
        self.lazy_seed_count += 1
        return True

    def _count_list_nodes(self, memory: SparseMemory, *, limit: int) -> int:
        node = memory.read_u32(self.head_address)
        count = 0
        seen: set[int] = set()
        while node not in {0, self.head_address} and count < limit and node not in seen:
            seen.add(node)
            count += 1
            node = memory.read_u32(node)
        return count

    @staticmethod
    def _read_u8(memory: SparseMemory, address: int) -> int:
        return memory.read(address, 1)[0]

    @staticmethod
    def _write_u8(memory: SparseMemory, address: int, value: int) -> None:
        memory.write(address, bytes([value & 0xFF]))

    def summary(self) -> dict[str, Any]:
        return {
            "register_address": self.register_address,
            "register_address_hex": _hex32(self.register_address),
            "cleanup_address": self.cleanup_address,
            "cleanup_address_hex": _hex32(self.cleanup_address),
            "head_address": self.head_address,
            "head_address_hex": _hex32(self.head_address),
            "tail_address": self.tail_address,
            "tail_address_hex": _hex32(self.tail_address),
            "owner_offset": self.owner_offset,
            "node_offset": self.node_offset,
            "invocation_count": self.invocation_count,
            "lazy_seed_count": self.lazy_seed_count,
            "insert_count": self.insert_count,
            "skip_count": self.skip_count,
            "cleanup_count": self.cleanup_count,
            "cleanup_node_count": self.cleanup_node_count,
            "recent_registrations": self.registrations[-16:],
            "recent_cleanups": self.cleanups[-16:],
        }


class TitleStaticDriveArrayFastPath:
    """Seed the observed static drive descriptor array before virtual dispatch."""

    def __init__(
        self,
        *,
        setup_address: int = TITLE_STATIC_DRIVE_ARRAY_SETUP_ADDRESS,
        element_size: int = TITLE_STATIC_DRIVE_ARRAY_ELEMENT_SIZE,
        max_elements: int = TITLE_STATIC_DRIVE_ARRAY_MAX_ELEMENTS,
    ) -> None:
        self.setup_address = setup_address
        self.element_size = element_size
        self.max_elements = max_elements
        self.invocation_count = 0
        self.seeded_element_count = 0
        self.invocations: list[dict[str, Any]] = []

    def call_handlers(
        self,
    ) -> dict[int, Callable[[CpuState, SparseMemory, int, ExecutionTrace], None]]:
        return {self.setup_address: self.setup_handler}

    def setup_handler(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
    ) -> None:
        esp = cpu.get_register("esp")
        return_address = memory.read_u32(esp)
        count = memory.read_u32(_u32(esp + 4))
        element_base = memory.read_u32(_u32(esp + 8))
        path_address = memory.read_u32(_u32(esp + 12))
        descriptor_address = memory.read_u32(_u32(esp + 16))
        object_address = cpu.get_register("ecx")
        bounded_count = max(0, min(count, self.max_elements))

        memory.write_u32(_u32(object_address + 0x04), 1)
        memory.write_u32(_u32(object_address + 0x08), 0)
        memory.write_u32(_u32(object_address + 0x0C), count)
        memory.write_u32(_u32(object_address + 0x10), element_base)

        registry_index = memory.read_u32(TITLE_STATIC_DRIVE_ARRAY_REGISTRY_COUNT_ADDRESS)
        registered = False
        descriptor_tag = 0
        if descriptor_address:
            descriptor_tag = memory.read_u32(descriptor_address)
        if registry_index < 2:
            memory.write_u32(
                _u32(
                    TITLE_STATIC_DRIVE_ARRAY_REGISTRY_OBJECTS_ADDRESS
                    + registry_index * 4
                ),
                object_address,
            )
            memory.write_u32(
                _u32(
                    TITLE_STATIC_DRIVE_ARRAY_REGISTRY_DESCRIPTORS_ADDRESS
                    + registry_index * 5
                ),
                descriptor_tag,
            )
            memory.write_u32(
                TITLE_STATIC_DRIVE_ARRAY_REGISTRY_COUNT_ADDRESS,
                _u32(registry_index + 1),
            )
            registered = True

        for index in range(bounded_count):
            element_address = _u32(element_base + index * self.element_size)
            memory.write_u32(element_address, descriptor_address)
            memory.write_u32(_u32(element_address + 0x20), 0)

        self.invocation_count += 1
        self.seeded_element_count += bounded_count
        invocation = {
            "target": target,
            "target_hex": _hex32(target),
            "return_address": return_address,
            "return_address_hex": _hex32(return_address),
            "object_address": object_address,
            "object_address_hex": _hex32(object_address),
            "element_base": element_base,
            "element_base_hex": _hex32(element_base),
            "element_size": self.element_size,
            "requested_count": count,
            "seeded_count": bounded_count,
            "descriptor_address": descriptor_address,
            "descriptor_address_hex": _hex32(descriptor_address),
            "descriptor_tag_hex": _hex32(descriptor_tag),
            "path_address": path_address,
            "path_address_hex": _hex32(path_address),
            "registry_index": registry_index,
            "registered": registered,
        }
        self.invocations.append(invocation)
        cpu.set_register("eax", 1)
        _prepare_stdcall_return(
            cpu,
            memory,
            TITLE_STATIC_DRIVE_ARRAY_SETUP_STACK_CLEANUP,
        )
        trace.add(
            target,
            "title_static_drive_array_fast_path",
            invocation_count=self.invocation_count,
            **invocation,
        )

    def summary(self) -> dict[str, Any]:
        return {
            "setup_address_hex": _hex32(self.setup_address),
            "element_size": self.element_size,
            "max_elements": self.max_elements,
            "invocation_count": self.invocation_count,
            "seeded_element_count": self.seeded_element_count,
            "recent_invocations": self.invocations[-8:],
        }


class TitleFrontendAssetInitFastPath:
    """Seed the frontend global dictionary resources past the dirty-disc panel."""

    def __init__(
        self,
        *,
        init_address: int = TITLE_FRONTEND_ASSET_INIT_ADDRESS,
        manager_address: int = TITLE_FRONTEND_SYNTHETIC_ASSET_MANAGER_ADDRESS,
        boot_resource_handle: int = TITLE_FRONTEND_SYNTHETIC_BOOT_RESOURCE_HANDLE_ADDRESS,
        global_dic_handle: int = TITLE_FRONTEND_SYNTHETIC_GLOBAL_DIC_HANDLE_ADDRESS,
        global_dic_node: int = TITLE_FRONTEND_SYNTHETIC_GLOBAL_DIC_NODE_ADDRESS,
        global_dic_list: int = TITLE_FRONTEND_SYNTHETIC_GLOBAL_DIC_LIST_ADDRESS,
        glassb_handle: int = TITLE_FRONTEND_SYNTHETIC_GLASSB_HANDLE_ADDRESS,
        glass_handle: int = TITLE_FRONTEND_SYNTHETIC_GLASS_HANDLE_ADDRESS,
    ) -> None:
        self.init_address = init_address
        self.manager_address = manager_address
        self.boot_resource_handle = boot_resource_handle
        self.global_dic_handle = global_dic_handle
        self.global_dic_node = global_dic_node
        self.global_dic_list = global_dic_list
        self.glassb_handle = glassb_handle
        self.glass_handle = glass_handle
        self.invocation_count = 0
        self.method_invocation_count = 0
        self.last_object_address: int | None = None

    def call_handlers(
        self,
    ) -> dict[int, Callable[[CpuState, SparseMemory, int, ExecutionTrace], None]]:
        return {
            TITLE_FRONTEND_ASSET_METHOD_TRAMPOLINE_ADDRESS: self.method_handler,
            TITLE_FRONTEND_ASSET_SECOND_METHOD_TRAMPOLINE_ADDRESS: self.method_handler,
        }

    def init_handler(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
    ) -> None:
        object_address = cpu.get_register("ecx")
        self._seed_object_fields(memory, object_address)
        self._seed_global_dictionary_list(memory)

        cpu.set_register("eax", 1)
        _prepare_stdcall_return(cpu, memory, 0)
        self.invocation_count += 1
        self.last_object_address = object_address
        trace.add(
            target,
            "title_frontend_asset_init_fast_path",
            invocation_count=self.invocation_count,
            object_address=object_address,
            object_address_hex=_hex32(object_address),
            manager_address_hex=_hex32(self.manager_address),
            global_resource_list_address_hex=_hex32(
                TITLE_FRONTEND_GLOBAL_RESOURCE_LIST_ADDRESS
            ),
            global_dic_node_hex=_hex32(self.global_dic_node),
            resource_path="Frontend/global.dic",
            lookup_key_hint="impact2",
        )

    def _seed_object_fields(self, memory: SparseMemory, object_address: int) -> None:
        memory.write_u32(_u32(object_address + 0x6B950), self.manager_address)
        memory.write_u32(_u32(object_address + 0x6B978), self.boot_resource_handle)
        memory.write_u32(_u32(object_address + 0x6B97C), self.global_dic_handle)
        memory.write_u32(_u32(object_address + 0x6B980), 0)
        memory.write_u32(_u32(object_address + 0x6B984), self.glassb_handle)
        memory.write_u32(_u32(object_address + 0x6B988), self.glass_handle)
        memory.write_u32(_u32(self.manager_address + 0x60), 0)
        memory.write_u32(_u32(self.manager_address + 0x64), 0)
        memory.write_u32(
            _u32(self.manager_address + 0x18),
            TITLE_FRONTEND_SYNTHETIC_ASSET_METHOD_TARGET_ADDRESS,
        )
        memory.write_u32(
            _u32(self.manager_address + 0x1C),
            TITLE_FRONTEND_SYNTHETIC_ASSET_METHOD_TARGET_ADDRESS,
        )

    def method_handler(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
    ) -> None:
        esp = cpu.get_register("esp")
        manager_address = memory.read_u32(_u32(esp + 4))
        dispatch_offset = (
            0x1C
            if target == TITLE_FRONTEND_ASSET_SECOND_METHOD_TRAMPOLINE_ADDRESS
            else 0x18
        )
        method_target = memory.read_u32(_u32(manager_address + dispatch_offset))
        self.method_invocation_count += 1
        cpu.set_register("eax", 1)
        trace.add(
            target,
            "title_frontend_asset_method_fast_path",
            invocation_count=self.method_invocation_count,
            manager_address_hex=_hex32(manager_address),
            dispatch_offset=dispatch_offset,
            method_target_hex=_hex32(method_target),
        )

    def _seed_global_dictionary_list(self, memory: SparseMemory) -> None:
        list_base = self.global_dic_list
        sentinel = _u32(list_base + 8)
        node = self.global_dic_node
        link = _u32(node + 8)
        memory.write_u32(TITLE_FRONTEND_GLOBAL_RESOURCE_LIST_ADDRESS, list_base)
        memory.write_u32(list_base, 0)
        memory.write_u32(_u32(list_base + 4), 0)
        memory.write_u32(sentinel, link)
        memory.write_u32(_u32(sentinel + 4), link)
        memory.write_u32(link, sentinel)
        memory.write_u32(_u32(link + 4), sentinel)
        memory.write(_u32(node + 0x10), b"impact2\x00")

    def summary(self) -> dict[str, Any]:
        return {
            "init_address": self.init_address,
            "init_address_hex": _hex32(self.init_address),
            "invocation_count": self.invocation_count,
            "method_invocation_count": self.method_invocation_count,
            "last_object_address_hex": _hex32(self.last_object_address or 0)
            if self.last_object_address is not None
            else None,
            "manager_address_hex": _hex32(self.manager_address),
            "method_trampoline_address_hex": _hex32(
                TITLE_FRONTEND_ASSET_METHOD_TRAMPOLINE_ADDRESS
            ),
            "second_method_trampoline_address_hex": _hex32(
                TITLE_FRONTEND_ASSET_SECOND_METHOD_TRAMPOLINE_ADDRESS
            ),
            "method_target_address_hex": _hex32(
                TITLE_FRONTEND_SYNTHETIC_ASSET_METHOD_TARGET_ADDRESS
            ),
            "boot_resource_handle_hex": _hex32(self.boot_resource_handle),
            "global_dic_handle_hex": _hex32(self.global_dic_handle),
            "global_dic_node_hex": _hex32(self.global_dic_node),
            "global_dic_list_hex": _hex32(self.global_dic_list),
            "glassb_handle_hex": _hex32(self.glassb_handle),
            "glass_handle_hex": _hex32(self.glass_handle),
            "resource_path": "Frontend/global.dic",
            "lookup_key_hint": "impact2",
        }


class TitleAssetStreamOpenFastPath:
    """Open observed title asset streams against the configured disc root."""

    def __init__(
        self,
        runtime: XboxRuntimeShims,
        *,
        open_address: int = TITLE_ASSET_STREAM_OPEN_ADDRESS,
        object_address: int = TITLE_ASSET_STREAM_SYNTHETIC_OBJECT_ADDRESS,
        vtable_address: int = TITLE_ASSET_STREAM_SYNTHETIC_VTABLE_ADDRESS,
        activate_target: int = TITLE_ASSET_STREAM_SYNTHETIC_ACTIVATE_TARGET_ADDRESS,
        status_target: int = TITLE_ASSET_STREAM_SYNTHETIC_STATUS_TARGET_ADDRESS,
        read_target: int = TITLE_ASSET_STREAM_SYNTHETIC_READ_TARGET_ADDRESS,
        seek_target: int = TITLE_ASSET_STREAM_SYNTHETIC_SEEK_TARGET_ADDRESS,
    ) -> None:
        self.runtime = runtime
        self.open_address = open_address
        self.object_address = object_address
        self.vtable_address = vtable_address
        self.activate_target = activate_target
        self.status_target = status_target
        self.read_target = read_target
        self.seek_target = seek_target
        self.invocations: list[dict[str, Any]] = []
        self.activation_count = 0
        self.status_poll_count = 0
        self.read_count = 0
        self.bytes_read = 0
        self.seek_count = 0
        self._states: dict[int, dict[str, Any]] = {}

    def call_handlers(
        self,
    ) -> dict[int, Callable[[CpuState, SparseMemory, int, ExecutionTrace], None]]:
        return {
            self.open_address: self.open_handler,
            self.activate_target: self.activate_handler,
            self.status_target: self.status_handler,
            self.read_target: self.read_handler,
            self.seek_target: self.seek_handler,
        }

    def open_handler(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
    ) -> None:
        esp = cpu.get_register("esp")
        title_path_address = memory.read_u32(_u32(esp + 4))
        flags = memory.read_u32(_u32(esp + 8))
        title_path_bytes = _read_guest_c_string(memory, title_path_address, max_bytes=260)
        title_path = title_path_bytes.decode("cp1252", errors="replace")
        guest_path = title_path.replace("/", "\\")
        if not any(guest_path.casefold().startswith(prefix.casefold()) for prefix in ("d:\\", "\\device\\")):
            guest_path = "D:\\" + guest_path.lstrip("\\")
        opened = self.runtime.filesystem.open_file(guest_path, "rb")
        status = int(opened.get("status", XboxStatus.NO_SUCH_FILE))
        size = 0
        result_address = 0
        payload = b""
        if status == XboxStatus.SUCCESS and not bool(opened.get("is_directory", False)):
            handle = opened.get("handle")
            if isinstance(handle, int):
                information = self.runtime.filesystem.query_file_handle_information(handle)
                size = int(information.get("size", 0))
                read_result = self.runtime.filesystem.read_file(handle, size, offset=0)
                read_payload = read_result.get("data", b"")
                payload = bytes(read_payload) if isinstance(read_payload, bytes) else b""
                self.runtime.nt_close(handle)
            result_address = _u32(
                self.object_address
                + len(self._states) * TITLE_ASSET_STREAM_SYNTHETIC_OBJECT_STRIDE
            )
            self._states[result_address] = {
                "payload": payload,
                "position": 0,
                "title_path": title_path.replace("\\", "/").casefold(),
            }
            memory.write_u32(result_address, self.vtable_address)
            memory.write_u32(_u32(result_address + 0x10), size)
            memory.write_u32(_u32(result_address + 0x2C), 2)
            memory.write_u32(_u32(self.vtable_address + 4), self.activate_target)
            memory.write_u32(_u32(self.vtable_address + 8), self.read_target)
            memory.write_u32(_u32(self.vtable_address + 0x10), self.seek_target)
            memory.write_u32(_u32(self.vtable_address + 0x1C), self.status_target)
        cpu.set_register("eax", result_address)
        _prepare_stdcall_return(cpu, memory, 8)
        invocation = {
            "invocation_count": len(self.invocations) + 1,
            "title_path": title_path,
            "guest_path": guest_path,
            "flags_hex": _hex32(flags),
            "status": status,
            "status_hex": _hex32(status),
            "size": size,
            "result_address_hex": _hex32(result_address),
        }
        self.invocations.append(invocation)
        trace.add(target, "title_asset_stream_open_fast_path", **invocation)

    def payload_for_title(self, title_path: str) -> bytes | None:
        normalized = title_path.replace("\\", "/").casefold()
        for state in reversed(list(self._states.values())):
            if state.get("title_path") == normalized:
                return bytes(state.get("payload", b""))
        return None

    def activate_handler(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
    ) -> None:
        self.activation_count += 1
        cpu.set_register("eax", 1)
        _prepare_stdcall_return(cpu, memory, 0)
        trace.add(
            target,
            "title_asset_stream_activate_fast_path",
            activation_count=self.activation_count,
            object_address_hex=_hex32(cpu.get_register("ecx")),
        )

    def read_handler(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
    ) -> None:
        esp = cpu.get_register("esp")
        destination = memory.read_u32(_u32(esp + 4))
        requested = memory.read_u32(_u32(esp + 8))
        object_address = cpu.get_register("ecx")
        state = self._states.get(object_address, {"payload": b"", "position": 0})
        payload = state["payload"]
        position = int(state["position"])
        chunk = payload[position : position + requested]
        if destination and chunk:
            memory.write(destination, chunk)
        position += len(chunk)
        state["position"] = position
        self.read_count += 1
        self.bytes_read += len(chunk)
        cpu.set_register("eax", len(chunk))
        _prepare_stdcall_return(cpu, memory, 8)
        trace.add(
            target,
            "title_asset_stream_read_fast_path",
            read_count=self.read_count,
            destination_hex=_hex32(destination),
            requested=requested,
            bytes_read=len(chunk),
            object_address_hex=_hex32(object_address),
            next_position=position,
        )

    def status_handler(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
    ) -> None:
        object_address = cpu.get_register("ecx")
        object_mode = memory.read_u32(_u32(object_address + 0x2C))
        # The title's stream scheduler treats 2 as pending and 3 as failed.
        # A validated synchronous host read is immediately ready (1).
        status = 1 if object_address in self._states else 3
        self.status_poll_count += 1
        cpu.set_register("eax", status)
        _prepare_stdcall_return(cpu, memory, 0)
        trace.add(
            target,
            "title_asset_stream_status_fast_path",
            object_address_hex=_hex32(object_address),
            status=status,
            status_hex=_hex32(status),
            object_mode=object_mode,
            object_mode_hex=_hex32(object_mode),
        )

    def seek_handler(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
    ) -> None:
        esp = cpu.get_register("esp")
        offset_low = memory.read_u32(_u32(esp + 4))
        offset_high = memory.read_u32(_u32(esp + 8))
        origin = memory.read_u32(_u32(esp + 0xC))
        object_address = cpu.get_register("ecx")
        state = self._states.get(object_address, {"payload": b"", "position": 0})
        payload = state["payload"]
        position = int(state["position"])
        signed_offset = (offset_high << 32) | offset_low
        if signed_offset & (1 << 63):
            signed_offset -= 1 << 64
        base = 0 if origin == 0 else position if origin == 1 else len(payload)
        position = max(0, min(len(payload), base + signed_offset))
        state["position"] = position
        self.seek_count += 1
        cpu.set_register("eax", position)
        _prepare_stdcall_return(cpu, memory, 12)
        trace.add(
            target,
            "title_asset_stream_seek_fast_path",
            seek_count=self.seek_count,
            signed_offset=signed_offset,
            origin=origin,
            object_address_hex=_hex32(object_address),
            position=position,
        )

    def summary(self) -> dict[str, Any]:
        return {
            "open_address_hex": _hex32(self.open_address),
            "invocation_count": len(self.invocations),
            "successful_open_count": sum(
                1 for invocation in self.invocations if invocation["status"] == XboxStatus.SUCCESS
            ),
            "activation_count": self.activation_count,
            "status_poll_count": self.status_poll_count,
            "read_count": self.read_count,
            "bytes_read": self.bytes_read,
            "seek_count": self.seek_count,
            "active_stream_count": len(self._states),
            "recent_invocations": self.invocations[-16:],
        }


class TitleFrontendSpecialAudioFastPath:
    """Create the frontend sound-bank handle and submit its requested SFX."""

    def __init__(
        self,
        runtime: XboxRuntimeShims,
        asset_streams: TitleAssetStreamOpenFastPath,
        output: WindowsPcmOutput | None = None,
        *,
        create_address: int = TITLE_FRONTEND_SPECIAL_AUDIO_CREATE_ADDRESS,
        handle_address: int = TITLE_FRONTEND_SYNTHETIC_AUDIO_HANDLE_ADDRESS,
    ) -> None:
        self.create_address = create_address
        self.handle_address = handle_address
        self.runtime = runtime
        self.asset_streams = asset_streams
        self.output = output
        self.invocations: list[dict[str, Any]] = []
        self.audio_handles: dict[int, int] = {}
        self.decode_error_count = 0
        self.submitted_clip_count = 0

    def call_handlers(
        self,
    ) -> dict[int, Callable[[CpuState, SparseMemory, int, ExecutionTrace], None]]:
        return {self.create_address: self.create_handler}

    def create_handler(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
    ) -> None:
        esp = cpu.get_register("esp")
        sample_index = memory.read_u32(_u32(esp + 4))
        frontend_audio_object = cpu.get_register("ecx")
        decoded_clip_count = 0
        playback_started = False
        payload = self.asset_streams.payload_for_title("audio/special.rws")
        if payload:
            try:
                clips = parse_rws_pcm(payload)
                decoded_clip_count = len(clips)
                if self.output is not None and clips:
                    clip = clips[sample_index % len(clips)]
                    playback_started = self.output.submit_pcm(
                        clip.payload,
                        sample_rate=clip.sample_rate,
                        channels=clip.channels,
                        bits_per_sample=clip.bits_per_sample,
                    )
                    if playback_started:
                        self.submitted_clip_count += 1
            except ValueError:
                self.decode_error_count += 1
        memory.write_u32(self.handle_address, 1)
        list_sentinel = _u32(self.handle_address + 0x0C)
        memory.write_u32(_u32(self.handle_address + 0x10), list_sentinel)
        cpu.set_register("eax", self.handle_address)
        _prepare_stdcall_return(cpu, memory, 4)
        invocation = {
            "invocation_count": len(self.invocations) + 1,
            "frontend_audio_object_hex": _hex32(frontend_audio_object),
            "sample_index": sample_index,
            "handle_address_hex": _hex32(self.handle_address),
            "list_sentinel_hex": _hex32(list_sentinel),
            "decoded_clip_count": decoded_clip_count,
            "playback_started": playback_started,
        }
        self.invocations.append(invocation)
        trace.add(target, "title_frontend_special_audio_fast_path", **invocation)

    def summary(self) -> dict[str, Any]:
        return {
            "create_address_hex": _hex32(self.create_address),
            "invocation_count": len(self.invocations),
            "handle_address_hex": _hex32(self.handle_address),
            "list_sentinel_hex": _hex32(_u32(self.handle_address + 0x0C)),
            "submitted_clip_count": self.submitted_clip_count,
            "decode_error_count": self.decode_error_count,
            "recent_invocations": self.invocations[-8:],
        }


class TitleMusicModeFastPath:
    """Model the title music-mode transition and submit its streamed menu track."""

    _PCM_CACHE_MAGIC = b"B2RPCM1\0"
    _PCM_CACHE_HEADER = struct.Struct("<8sIHHQ32s")

    def __init__(
        self,
        runtime: XboxRuntimeShims,
        output: WindowsPcmOutput | None,
        *,
        set_mode_address: int = TITLE_MUSIC_MODE_SET_ADDRESS,
        cache_dir: Path | None = None,
    ) -> None:
        self.runtime = runtime
        self.output = output
        self.set_mode_address = set_mode_address
        self.cache_dir = cache_dir
        self.invocations: list[dict[str, Any]] = []
        self.decode_error_count = 0
        self.decoded_track_count = 0
        self.submitted_track_count = 0
        self.cache_hit_count = 0
        self.cache_miss_count = 0
        self._menu_clip = None

    def call_handlers(
        self,
    ) -> dict[int, Callable[[CpuState, SparseMemory, int, ExecutionTrace], None]]:
        return {self.set_mode_address: self.set_mode_handler}

    def set_mode_handler(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
    ) -> None:
        esp = cpu.get_register("esp")
        object_holder_address = cpu.get_register("ecx")
        object_address = (
            memory.read_u32(object_holder_address) if object_holder_address else 0
        )
        if object_address == 0 and object_holder_address:
            object_address = TITLE_MUSIC_SYNTHETIC_MANAGER_ADDRESS
            memory.write_u32(object_holder_address, object_address)
        mode = memory.read_u32(_u32(esp + 4))
        previous_mode = memory.read_u32(_u32(object_address + 0x38)) if object_address else 0
        if object_address:
            memory.write_u32(_u32(object_address + 0x38), mode)

        decoded_bytes = 0
        playback_started = False
        if mode == TITLE_MUSIC_MENU_MODE and previous_mode != mode:
            try:
                clip = self._load_menu_clip()
                decoded_bytes = len(clip.payload)
                if self.output is not None:
                    playback_started = self.output.submit_pcm(
                        clip.payload,
                        sample_rate=clip.sample_rate,
                        channels=clip.channels,
                        bits_per_sample=clip.bits_per_sample,
                        loop=True,
                    )
                    if playback_started:
                        self.submitted_track_count += 1
            except (OSError, ValueError):
                self.decode_error_count += 1
        elif previous_mode == TITLE_MUSIC_MENU_MODE and mode != previous_mode:
            stop = getattr(self.output, "stop", None)
            if callable(stop):
                stop()

        cpu.set_register("eax", 1)
        _prepare_stdcall_return(cpu, memory, 4)
        invocation = {
            "invocation_count": len(self.invocations) + 1,
            "object_holder_address_hex": _hex32(object_holder_address),
            "object_address_hex": _hex32(object_address),
            "previous_mode": previous_mode,
            "mode": mode,
            "menu_track": TITLE_MUSIC_MENU_PATH.as_posix(),
            "decoded_bytes": decoded_bytes,
            "playback_started": playback_started,
        }
        self.invocations.append(invocation)
        trace.add(target, "title_music_mode_fast_path", **invocation)

    def _load_menu_clip(self):
        if self._menu_clip is None:
            disc_root = self.runtime.config.extracted_disc_root
            if disc_root is None:
                raise OSError("no extracted disc root configured")
            encoded = (disc_root / TITLE_MUSIC_MENU_PATH).read_bytes()
            cache_path = self._menu_cache_path(encoded)
            self._menu_clip = self._read_cached_menu_clip(cache_path)
            if self._menu_clip is None:
                self.cache_miss_count += 1
                self._menu_clip = parse_rws_xbox_adpcm(encoded)
                self._write_cached_menu_clip(cache_path, self._menu_clip)
            else:
                self.cache_hit_count += 1
            self.decoded_track_count += 1
        return self._menu_clip

    def _menu_cache_path(self, encoded: bytes) -> Path | None:
        if self.cache_dir is None:
            return None
        digest = hashlib.sha256(encoded).hexdigest()
        return self.cache_dir / f"{digest}-xbox-adpcm-v1-stereo.pcm"

    def _read_cached_menu_clip(self, path: Path | None) -> PcmClip | None:
        if path is None or not path.is_file():
            return None
        cached = path.read_bytes()
        if len(cached) < self._PCM_CACHE_HEADER.size:
            return None
        magic, sample_rate, channels, bits_per_sample, size, digest = (
            self._PCM_CACHE_HEADER.unpack_from(cached)
        )
        payload = cached[self._PCM_CACHE_HEADER.size :]
        if (
            magic != self._PCM_CACHE_MAGIC
            or size != len(payload)
            or hashlib.sha256(payload).digest() != digest
        ):
            return None
        return PcmClip(sample_rate, channels, bits_per_sample, payload)

    def _write_cached_menu_clip(self, path: Path | None, clip: PcmClip) -> None:
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        header = self._PCM_CACHE_HEADER.pack(
            self._PCM_CACHE_MAGIC,
            clip.sample_rate,
            clip.channels,
            clip.bits_per_sample,
            len(clip.payload),
            hashlib.sha256(clip.payload).digest(),
        )
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(header + clip.payload)
        temporary.replace(path)

    def summary(self) -> dict[str, Any]:
        return {
            "set_mode_address_hex": _hex32(self.set_mode_address),
            "invocation_count": len(self.invocations),
            "menu_mode": TITLE_MUSIC_MENU_MODE,
            "menu_track": TITLE_MUSIC_MENU_PATH.as_posix(),
            "decode_error_count": self.decode_error_count,
            "decoded_track_count": self.decoded_track_count,
            "submitted_track_count": self.submitted_track_count,
            "cache_hit_count": self.cache_hit_count,
            "cache_miss_count": self.cache_miss_count,
            "recent_invocations": self.invocations[-8:],
        }


class TitleFrontendCrtInitializerAudit:
    """Audit the frontend objects that should be initialized by CRT routine 0x1392B0."""

    def __init__(self) -> None:
        self.initializer_execution_count = 0
        self.observation_count = 0
        self.null_object_count = 0
        self.null_vtable_count = 0
        self.observations: list[dict[str, Any]] = []

    def call_handlers(
        self,
    ) -> dict[int, Callable[[CpuState, SparseMemory, int, ExecutionTrace], None]]:
        return {}

    def observer(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        trace: ExecutionTrace,
        steps: int,
    ) -> None:
        if cpu.eip == TITLE_FRONTEND_CRT_VTABLE_INITIALIZER_ADDRESS:
            self.initializer_execution_count += 1
            initializer_table_target = memory.read_u32(
                TITLE_FRONTEND_CRT_VTABLE_INITIALIZER_TABLE_ENTRY_ADDRESS
            )
            trace.add(
                TITLE_FRONTEND_CRT_VTABLE_INITIALIZER_ADDRESS,
                "title_frontend_crt_initializer_execution",
                steps=steps,
                execution_count=self.initializer_execution_count,
                initializer_table_entry_address_hex=_hex32(
                    TITLE_FRONTEND_CRT_VTABLE_INITIALIZER_TABLE_ENTRY_ADDRESS
                ),
                initializer_table_target_hex=_hex32(initializer_table_target),
                initializer_table_entry_matches=initializer_table_target
                == TITLE_FRONTEND_CRT_VTABLE_INITIALIZER_ADDRESS,
            )
            return
        if cpu.eip != TITLE_FRONTEND_DYNAMIC_OBJECT_RESET_USE_ADDRESS:
            return
        self.observation_count += 1
        owner_address = cpu.get_register("esi")
        object_address = memory.read_u32(
            owner_address + TITLE_FRONTEND_DYNAMIC_OBJECT_POINTER_OFFSET
        )
        vtable_address = memory.read_u32(object_address) if object_address else 0
        method0_target = memory.read_u32(vtable_address) if vtable_address else 0
        initializer_table_target = memory.read_u32(
            TITLE_FRONTEND_CRT_VTABLE_INITIALIZER_TABLE_ENTRY_ADDRESS
        )
        if object_address == 0:
            self.null_object_count += 1
        if object_address != 0 and vtable_address == 0:
            self.null_vtable_count += 1
        observation = {
            "steps": steps,
            "owner_address_hex": _hex32(owner_address),
            "object_address_hex": _hex32(object_address),
            "vtable_address_hex": _hex32(vtable_address),
            "method0_target_hex": _hex32(method0_target),
            "initializer_table_target_hex": _hex32(initializer_table_target),
            "initializer_table_entry_matches": initializer_table_target
            == TITLE_FRONTEND_CRT_VTABLE_INITIALIZER_ADDRESS,
        }
        self.observations.append(observation)
        trace.add(
            TITLE_FRONTEND_DYNAMIC_OBJECT_RESET_USE_ADDRESS,
            "title_frontend_crt_initializer_audit",
            observation_count=self.observation_count,
            null_object_count=self.null_object_count,
            null_vtable_count=self.null_vtable_count,
            **observation,
        )

    def summary(self) -> dict[str, Any]:
        return {
            "use_address_hex": _hex32(TITLE_FRONTEND_DYNAMIC_OBJECT_RESET_USE_ADDRESS),
            "initializer_address_hex": _hex32(
                TITLE_FRONTEND_CRT_VTABLE_INITIALIZER_ADDRESS
            ),
            "initializer_table_entry_address_hex": _hex32(
                TITLE_FRONTEND_CRT_VTABLE_INITIALIZER_TABLE_ENTRY_ADDRESS
            ),
            "crt_constructor_table_start_address_hex": _hex32(
                TITLE_CRT_CONSTRUCTOR_TABLE_START_ADDRESS
            ),
            "crt_constructor_table_end_address_hex": _hex32(
                TITLE_CRT_CONSTRUCTOR_TABLE_END_ADDRESS
            ),
            "initializer_execution_count": self.initializer_execution_count,
            "observation_count": self.observation_count,
            "null_object_count": self.null_object_count,
            "null_vtable_count": self.null_vtable_count,
            "recent_observations": self.observations[-12:],
        }


class TitleFrontendObjectConstructorFastPath:
    """Legacy synthetic child model retained only for focused diagnostics."""

    def __init__(
        self,
        *,
        constructor_address: int = TITLE_FRONTEND_OBJECT_CONSTRUCTOR_ADDRESS,
        child_object_base: int = TITLE_FRONTEND_SYNTHETIC_CHILD_OBJECT_BASE_ADDRESS,
        child_state_base: int = TITLE_FRONTEND_SYNTHETIC_CHILD_STATE_BASE_ADDRESS,
        child_descriptor_base: int = (
            TITLE_FRONTEND_SYNTHETIC_CHILD_DESCRIPTOR_BASE_ADDRESS
        ),
        method_table_address: int = TITLE_FRONTEND_SYNTHETIC_CHILD_METHOD_TABLE_ADDRESS,
        method_target_address: int = (
            TITLE_FRONTEND_SYNTHETIC_CHILD_METHOD_TARGET_ADDRESS
        ),
        child_stride: int = TITLE_FRONTEND_SYNTHETIC_CHILD_STRIDE,
    ) -> None:
        self.constructor_address = constructor_address
        self.child_object_base = child_object_base
        self.child_state_base = child_state_base
        self.child_descriptor_base = child_descriptor_base
        self.method_table_address = method_table_address
        self.method_target_address = method_target_address
        self.child_stride = child_stride
        self.invocation_count = 0
        self.method_invocation_count = 0
        self.invocations: list[dict[str, Any]] = []
        self.method_invocations: list[dict[str, Any]] = []

    def call_handlers(
        self,
        *,
        include_constructor_bypass: bool = False,
    ) -> dict[int, Callable[[CpuState, SparseMemory, int, ExecutionTrace], None]]:
        handlers = {self.method_target_address: self.method_handler}
        if include_constructor_bypass:
            handlers[self.constructor_address] = self.constructor_handler
        return handlers

    def constructor_handler(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
    ) -> None:
        owner_address = cpu.get_register("ecx")
        slot = self.invocation_count
        child_object = self.child_object_base + slot * self.child_stride
        child_state = self.child_state_base + slot * self.child_stride
        child_descriptor = self.child_descriptor_base + slot * self.child_stride
        self._seed_child(memory, owner_address, child_object, child_state, child_descriptor)

        self.invocation_count += 1
        invocation = {
            "owner_address": owner_address,
            "owner_address_hex": _hex32(owner_address),
            "child_object_hex": _hex32(child_object),
            "child_state_hex": _hex32(child_state),
            "child_descriptor_hex": _hex32(child_descriptor),
            "method_table_hex": _hex32(self.method_table_address),
            "method_target_hex": _hex32(self.method_target_address),
            "dispatch_indices": list(TITLE_FRONTEND_SYNTHETIC_CHILD_DISPATCH_INDICES),
        }
        self.invocations.append(invocation)
        cpu.set_register("eax", 1)
        _prepare_stdcall_return(cpu, memory, 0)
        trace.add(
            target,
            "title_frontend_object_constructor_fast_path",
            invocation_count=self.invocation_count,
            **invocation,
        )

    def method_handler(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
    ) -> None:
        esp = cpu.get_register("esp")
        dispatch_index = (
            _u32(cpu.get_register("eax") - self.method_table_address) // 8
        )
        payload_address = memory.read_u32(esp + 12)
        output_object_address = None
        if (
            dispatch_index
            in TITLE_FRONTEND_SYNTHETIC_CHILD_OUT_POINTER_DISPATCH_INDICES
            and payload_address
        ):
            output_object_address = self.child_object_base
            memory.write_u32(payload_address, output_object_address)
        invocation = {
            "context_hex": _hex32(memory.read_u32(esp + 4)),
            "dispatch_index": dispatch_index,
            "selector": memory.read_u32(esp + 8),
            "selector_hex": _hex32(memory.read_u32(esp + 8)),
            "payload_hex": _hex32(payload_address),
            "output_object_hex": (
                _hex32(output_object_address)
                if output_object_address is not None
                else None
            ),
        }
        self.method_invocation_count += 1
        self.method_invocations.append(invocation)
        cpu.set_register("eax", 1)
        _prepare_stdcall_return(cpu, memory, 0)
        trace.add(
            target,
            "title_frontend_child_method_fast_path",
            invocation_count=self.method_invocation_count,
            **invocation,
        )

    def _seed_child(
        self,
        memory: SparseMemory,
        owner_address: int,
        child_object: int,
        child_state: int,
        child_descriptor: int,
    ) -> None:
        memory.write_u32(_u32(owner_address + 0x58), child_object)
        memory.write_u32(_u32(owner_address + 0x5C), child_state)
        memory.write_u32(child_object, owner_address)
        memory.write_u32(_u32(child_object + 4), child_descriptor)
        memory.write_u32(child_descriptor, child_object)
        memory.write_u32(_u32(child_descriptor + 4), self.method_table_address)
        memory.write_u32(_u32(child_descriptor + 8), self.method_table_address)
        for dispatch_index in TITLE_FRONTEND_SYNTHETIC_CHILD_DISPATCH_INDICES:
            method_entry = self.method_table_address + dispatch_index * 8
            memory.write_u32(method_entry, self.method_target_address)
            memory.write(_u32(method_entry + 4), b"\x00\x00")
        memory.write_u32(_u32(child_state + 0x64), 0)
        memory.write_u32(_u32(child_state + 0x68), 0)
        memory.write_u32(_u32(child_state + 0x70), 0)

    def summary(self) -> dict[str, Any]:
        return {
            "constructor_address": self.constructor_address,
            "constructor_address_hex": _hex32(self.constructor_address),
            "constructor_bypass_enabled": False,
            "invocation_count": self.invocation_count,
            "method_invocation_count": self.method_invocation_count,
            "child_object_base_hex": _hex32(self.child_object_base),
            "child_state_base_hex": _hex32(self.child_state_base),
            "child_descriptor_base_hex": _hex32(self.child_descriptor_base),
            "method_table_hex": _hex32(self.method_table_address),
            "method_target_hex": _hex32(self.method_target_address),
            "dispatch_index": TITLE_FRONTEND_SYNTHETIC_CHILD_DISPATCH_INDEX,
            "dispatch_indices": list(TITLE_FRONTEND_SYNTHETIC_CHILD_DISPATCH_INDICES),
            "recent_invocations": self.invocations[-8:],
            "recent_method_invocations": self.method_invocations[-8:],
        }


class TitleFixedWidthCompareFastPath:
    """Legacy synthetic compare retained only for focused diagnostics."""

    def __init__(
        self,
        *,
        compare_address: int = TITLE_FIXED_WIDTH_COMPARE_ADDRESS,
        compare_size: int = TITLE_FIXED_WIDTH_COMPARE_BYTES,
        sample_limit: int = 16,
    ) -> None:
        self.compare_address = compare_address
        self.compare_size = compare_size
        self.sample_limit = sample_limit
        self.invocation_count = 0
        self.equal_count = 0
        self.less_count = 0
        self.greater_count = 0
        self.null_argument_count = 0
        self.sampled_invocations: list[dict[str, Any]] = []

    def call_handlers(
        self,
        *,
        include_compare_bypass: bool = False,
    ) -> dict[int, Callable[[CpuState, SparseMemory, int, ExecutionTrace], None]]:
        if not include_compare_bypass:
            return {}
        return {self.compare_address: self.compare_handler}

    def compare_handler(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
    ) -> None:
        esp = cpu.get_register("esp")
        left_address = memory.read_u32(_u32(esp + 4))
        right_address = memory.read_u32(_u32(esp + 8))
        left = memory.read(left_address, self.compare_size)
        right = memory.read(right_address, self.compare_size)
        mismatch_index: int | None = None
        result = 0
        for index, (left_byte, right_byte) in enumerate(zip(left, right, strict=True)):
            if left_byte == right_byte:
                continue
            mismatch_index = index
            result = 0xFFFFFFFF if left_byte < right_byte else 1
            break

        self.invocation_count += 1
        if left_address == 0 or right_address == 0:
            self.null_argument_count += 1
        if result == 0:
            self.equal_count += 1
        elif result == 0xFFFFFFFF:
            self.less_count += 1
        else:
            self.greater_count += 1
        cpu.set_register("eax", result)
        sample = {
            "invocation_count": self.invocation_count,
            "left_address_hex": _hex32(left_address),
            "right_address_hex": _hex32(right_address),
            "result_hex": _hex32(result),
            "mismatch_index": mismatch_index,
            "left_sample_hex": left[: self.compare_size].hex().upper(),
            "right_sample_hex": right[: self.compare_size].hex().upper(),
        }
        if len(self.sampled_invocations) < self.sample_limit:
            self.sampled_invocations.append(sample)
        trace.add(
            target,
            "title_fixed_width_compare_fast_path",
            compare_size=self.compare_size,
            null_argument=left_address == 0 or right_address == 0,
            **sample,
        )

    def summary(self) -> dict[str, Any]:
        return {
            "compare_address": self.compare_address,
            "compare_address_hex": _hex32(self.compare_address),
            "compare_size": self.compare_size,
            "compare_bypass_enabled": False,
            "invocation_count": self.invocation_count,
            "equal_count": self.equal_count,
            "less_count": self.less_count,
            "greater_count": self.greater_count,
            "null_argument_count": self.null_argument_count,
            "sampled_invocations": self.sampled_invocations,
        }


class TitleFrontendCompareSearchSentinelRepair:
    """Repair missing child-list sentinels before the recovered tree search."""

    def __init__(self, *, sample_limit: int = 16) -> None:
        self.sample_limit = sample_limit
        self.invocation_count = 0
        self.null_root_count = 0
        self.repair_count = 0
        self.synthetic_root_seed_count = 0
        self.repaired_next_count = 0
        self.repaired_previous_count = 0
        self.samples: list[dict[str, Any]] = []
        self.call_site_counts: Counter[int] = Counter()
        self.signature_counts: Counter[tuple[int, int, int, int, int, int]] = Counter()

    def observer(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        trace: ExecutionTrace,
        steps: int,
    ) -> None:
        if cpu.eip != TITLE_FRONTEND_COMPARE_SEARCH_ADDRESS:
            return

        self.invocation_count += 1
        esp = cpu.get_register("esp")
        return_address = memory.read_u32(esp)
        supplied_root = memory.read_u32(_u32(esp + 4))
        root = supplied_root or memory.read_u32(
            TITLE_FRONTEND_COMPARE_SEARCH_GLOBAL_ROOT_ADDRESS
        )
        out_pointer = memory.read_u32(_u32(esp + 8))
        comparator = memory.read_u32(_u32(esp + 0x0C))
        key = memory.read_u32(_u32(esp + 0x10))
        mode = memory.read_u32(_u32(esp + 0x14))
        self.call_site_counts[return_address] += 1
        signature = (return_address, supplied_root, root, out_pointer, key, mode)
        self.signature_counts[signature] += 1
        if supplied_root == 0 and root == 0:
            root = TITLE_FRONTEND_COMPARE_SEARCH_SYNTHETIC_ROOT_ADDRESS
            sentinel = _u32(root + TITLE_FRONTEND_COMPARE_SEARCH_CHILD_LIST_OFFSET)
            memory.write_u32(TITLE_FRONTEND_COMPARE_SEARCH_GLOBAL_ROOT_ADDRESS, root)
            memory.write_u32(root, TITLE_FRONTEND_COMPARE_SEARCH_SYNTHETIC_KEY_ADDRESS)
            memory.write(
                TITLE_FRONTEND_COMPARE_SEARCH_SYNTHETIC_KEY_ADDRESS,
                bytes(TITLE_FIXED_WIDTH_COMPARE_BYTES),
            )
            memory.write_u32(sentinel, sentinel)
            memory.write_u32(_u32(sentinel + 4), sentinel)
            self.synthetic_root_seed_count += 1
            trace.add(
                TITLE_FRONTEND_COMPARE_SEARCH_ADDRESS,
                "title_frontend_compare_search_root_seed",
                steps=steps,
                return_address_hex=_hex32(return_address),
                root_hex=_hex32(root),
                sentinel_hex=_hex32(sentinel),
                key_storage_hex=_hex32(
                    TITLE_FRONTEND_COMPARE_SEARCH_SYNTHETIC_KEY_ADDRESS
                ),
            )
        valid_root = 0x00010000 <= root < 0x80000000
        if not valid_root:
            self.null_root_count += 1
            sample = {
                "steps": steps,
                "return_address_hex": _hex32(return_address),
                "supplied_root_hex": _hex32(supplied_root),
                "root_hex": _hex32(root),
                "valid_root": False,
                "out_pointer_hex": _hex32(out_pointer),
                "comparator_hex": _hex32(comparator),
                "key_hex": _hex32(key),
                "mode_hex": _hex32(mode),
                "repair": False,
            }
            if (
                len(self.samples) < self.sample_limit
                and self.signature_counts[signature] == 1
            ):
                self.samples.append(sample)
            return

        sentinel = _u32(root + TITLE_FRONTEND_COMPARE_SEARCH_CHILD_LIST_OFFSET)
        next_link = memory.read_u32(sentinel)
        previous_link = memory.read_u32(_u32(sentinel + 4))
        repaired_next = next_link == 0
        repaired_previous = previous_link == 0
        if repaired_next:
            memory.write_u32(sentinel, sentinel)
            self.repaired_next_count += 1
        if repaired_previous:
            memory.write_u32(_u32(sentinel + 4), sentinel)
            self.repaired_previous_count += 1
        repaired = repaired_next or repaired_previous
        if repaired:
            self.repair_count += 1

        sample = {
            "steps": steps,
            "return_address_hex": _hex32(return_address),
            "supplied_root_hex": _hex32(supplied_root),
            "root_hex": _hex32(root),
            "valid_root": True,
            "out_pointer_hex": _hex32(out_pointer),
            "comparator_hex": _hex32(comparator),
            "key_hex": _hex32(key),
            "mode_hex": _hex32(mode),
            "sentinel_hex": _hex32(sentinel),
            "next_link_before_hex": _hex32(next_link),
            "previous_link_before_hex": _hex32(previous_link),
            "repair": repaired,
        }
        if (
            len(self.samples) < self.sample_limit
            and self.signature_counts[signature] == 1
        ):
            self.samples.append(sample)
        if repaired:
            trace.add(
                TITLE_FRONTEND_COMPARE_SEARCH_ADDRESS,
                "title_frontend_compare_search_sentinel_repair",
                **sample,
            )

    def summary(self) -> dict[str, Any]:
        return {
            "search_address_hex": _hex32(TITLE_FRONTEND_COMPARE_SEARCH_ADDRESS),
            "global_root_address_hex": _hex32(
                TITLE_FRONTEND_COMPARE_SEARCH_GLOBAL_ROOT_ADDRESS
            ),
            "child_list_offset": TITLE_FRONTEND_COMPARE_SEARCH_CHILD_LIST_OFFSET,
            "invocation_count": self.invocation_count,
            "null_root_count": self.null_root_count,
            "repair_count": self.repair_count,
            "synthetic_root_seed_count": self.synthetic_root_seed_count,
            "repaired_next_count": self.repaired_next_count,
            "repaired_previous_count": self.repaired_previous_count,
            "call_site_counts": {
                _hex32(address): count
                for address, count in sorted(self.call_site_counts.items())
            },
            "unique_signature_count": len(self.signature_counts),
            "samples": self.samples,
        }


class TitleSubsystemInitializerAudit:
    """Read-only audit of the title's ordered subsystem initializer table."""

    observer_addresses = {
        TITLE_SUBSYSTEM_STARTUP_ADDRESS,
        TITLE_SUBSYSTEM_INITIALIZER_DRIVER_ADDRESS,
        TITLE_SUBSYSTEM_INITIALIZER_TABLE_ADDRESS,
        TITLE_SUBSYSTEM_INITIALIZER_CALL_ADDRESS,
        TITLE_SUBSYSTEM_INITIALIZER_RESULT_ADDRESS,
        TITLE_FRONTEND_REGISTRY_INITIALIZER_ADDRESS,
        TITLE_FRONTEND_REGISTRY_ROOT_PUBLISH_ADDRESS,
    }

    def __init__(self, *, sample_limit: int = 64) -> None:
        self.sample_limit = sample_limit
        self.attempt_count = 0
        self.result_count = 0
        self.stage_counts: Counter[int] = Counter()
        self.registry_root_candidates: list[dict[str, Any]] = []
        self._pending: list[dict[str, Any]] = []
        self.records: list[dict[str, Any]] = []

    def observer(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        _trace: ExecutionTrace,
        steps: int,
    ) -> None:
        if cpu.eip in {
            TITLE_SUBSYSTEM_STARTUP_ADDRESS,
            TITLE_SUBSYSTEM_INITIALIZER_DRIVER_ADDRESS,
            TITLE_SUBSYSTEM_INITIALIZER_TABLE_ADDRESS,
            TITLE_FRONTEND_REGISTRY_INITIALIZER_ADDRESS,
            TITLE_FRONTEND_REGISTRY_ROOT_PUBLISH_ADDRESS,
        }:
            self.stage_counts[cpu.eip] += 1
            if cpu.eip == TITLE_FRONTEND_REGISTRY_ROOT_PUBLISH_ADDRESS:
                self.registry_root_candidates.append(
                    {
                        "steps": steps,
                        "root_candidate_hex": _hex32(cpu.get_register("eax")),
                    }
                )
            return
        if cpu.eip == TITLE_SUBSYSTEM_INITIALIZER_CALL_ADDRESS:
            table_entry = cpu.get_register("edi")
            record = {
                "index": cpu.get_register("esi"),
                "table_entry_hex": _hex32(table_entry),
                "initializer_hex": _hex32(memory.read_u32(table_entry)),
                "call_steps": steps,
                "result": None,
            }
            self.attempt_count += 1
            self._pending.append(record)
            if len(self.records) < self.sample_limit:
                self.records.append(record)
            return
        if cpu.eip != TITLE_SUBSYSTEM_INITIALIZER_RESULT_ADDRESS:
            return
        self.result_count += 1
        if not self._pending:
            return
        record = self._pending.pop()
        record["result"] = cpu.get_register("eax")
        record["result_hex"] = _hex32(cpu.get_register("eax"))
        record["result_steps"] = steps

    def summary(self) -> dict[str, Any]:
        return {
            "call_address_hex": _hex32(TITLE_SUBSYSTEM_INITIALIZER_CALL_ADDRESS),
            "result_address_hex": _hex32(
                TITLE_SUBSYSTEM_INITIALIZER_RESULT_ADDRESS
            ),
            "attempt_count": self.attempt_count,
            "result_count": self.result_count,
            "stage_counts": {
                _hex32(address): self.stage_counts[address]
                for address in sorted(self.observer_addresses)
            },
            "registry_root_candidates": self.registry_root_candidates,
            "failed_initializers": [
                record for record in self.records if record.get("result") == 0
            ],
            "records": self.records,
        }


class TitleFrontendRegistryAudit:
    """Read-only audit of frontend class attachment and GUID lookup results."""

    observer_addresses = {
        TITLE_FRONTEND_REGISTRY_LOOKUP_ADDRESS,
        TITLE_FRONTEND_REGISTRY_LOOKUP_RESULT_ADDRESS,
        TITLE_FRONTEND_REGISTRY_ATTACH_ADDRESS,
        TITLE_FRONTEND_REGISTRY_ATTACH_RESULT_ADDRESS,
        TITLE_FRONTEND_REGISTRY_ATTACH_FAILURE_ADDRESS,
        TITLE_FRONTEND_REGISTRY_ATTACH_SUCCESS_ADDRESS,
        TITLE_FRONTEND_REGISTRY_TARGET_CLASS_INIT_ADDRESS,
        TITLE_FRONTEND_SUBSYSTEM_SHUTDOWN_ADDRESS,
        TITLE_FRONTEND_CONSTRUCTOR_RESULT_ADDRESS,
        TITLE_FRONTEND_CONSTRUCTOR_FRONTEND_OBJECT_RESULT_ADDRESS,
        TITLE_FRONTEND_CONSTRUCTOR_DESCRIPTOR_RESULT_ADDRESS,
        TITLE_FRONTEND_CONSTRUCTOR_FRONTEND_STATE_RESULT_ADDRESS,
        TITLE_FRONTEND_CONSTRUCTOR_RESOURCE_OBJECT_RESULT_ADDRESS,
        TITLE_FRONTEND_CONSTRUCTOR_RESOURCE_FAILURE_ADDRESS,
        TITLE_FRONTEND_CONSTRUCTOR_SUBSYSTEM_FAILURE_ADDRESS,
        TITLE_FRONTEND_OBJECT_CREATE_FACTORY_RESULT_ADDRESS,
        TITLE_FRONTEND_OBJECT_CREATE_INTERFACE_RESULT_ADDRESS,
        TITLE_FRONTEND_OBJECT_CREATE_FAILURE_ADDRESS,
        TITLE_FRONTEND_OBJECT_CREATE_SUCCESS_ADDRESS,
        TITLE_FRONTEND_DESCRIPTOR_ALLOCATE_ADDRESS,
        TITLE_FRONTEND_DEFAULT_FACTORY_ADDRESS,
        TITLE_FRONTEND_FACTORY_RESULT_ADDRESS,
        TITLE_FRONTEND_OBJECT_INITIALIZER_RESULT_ADDRESS,
        TITLE_FRONTEND_OBJECT_INITIALIZER_FAILURE_ADDRESS,
        TITLE_FRONTEND_OBJECT_INITIALIZER_SUCCESS_ADDRESS,
        TITLE_FRONTEND_OBJECT_AUDIO_CREATE_RESULT_ADDRESS,
        TITLE_FRONTEND_OBJECT_AUDIO_STATE_ALLOC_RESULT_ADDRESS,
        TITLE_FRONTEND_OBJECT_AUDIO_INIT_RESULT_ADDRESS,
        TITLE_DSOUND_INIT_DEVICE_RESULT_ADDRESS,
        TITLE_DSOUND_INIT_CAPABILITY_RESULT_ADDRESS,
        TITLE_DSOUND_INIT_MIXBIN_CREATE_RESULT_ADDRESS,
        TITLE_DSOUND_INIT_MIXBIN_CONFIGURE_RESULT_ADDRESS,
        TITLE_DSOUND_INIT_MIXBIN_FINALIZE_RESULT_ADDRESS,
        TITLE_DSOUND_INIT_EXIT_ADDRESS,
        TITLE_DSOUND_DEVICE_CORE_RESULT_ADDRESS,
        TITLE_DSOUND_DEVICE_COMMAND_RESULT_ADDRESS,
        TITLE_DSOUND_DEVICE_EVENT_RESULT_ADDRESS,
        TITLE_DSOUND_DEVICE_VOICE_RESULT_ADDRESS,
        TITLE_DSOUND_CORE_MEMORY_RESULT_ADDRESS,
        TITLE_DSOUND_CORE_HARDWARE_RESULT_ADDRESS,
        TITLE_DSOUND_HARDWARE_VOICE0_CREATE_RESULT_ADDRESS,
        TITLE_DSOUND_HARDWARE_VOICE0_CONFIG_RESULT_ADDRESS,
        TITLE_DSOUND_HARDWARE_VOICE0_START_RESULT_ADDRESS,
        TITLE_DSOUND_HARDWARE_VOICE1_CONFIG_RESULT_ADDRESS,
        TITLE_DSOUND_HARDWARE_VOICE1_START_RESULT_ADDRESS,
    }

    def __init__(self, *, sample_limit: int = 64) -> None:
        self.sample_limit = sample_limit
        self.stage_counts: Counter[int] = Counter()
        self.lookup_count = 0
        self.attach_count = 0
        self.lookup_records: list[dict[str, Any]] = []
        self.attach_records: list[dict[str, Any]] = []
        self.constructor_checkpoints: list[dict[str, Any]] = []
        self.object_create_checkpoints: list[dict[str, Any]] = []
        self.descriptor_allocations: list[dict[str, Any]] = []
        self.dsound_init_checkpoints: list[dict[str, Any]] = []
        self.constructor_results: list[dict[str, Any]] = []
        self._pending_lookups: list[dict[str, Any]] = []
        self._pending_attaches: list[dict[str, Any]] = []

    @staticmethod
    def _key_fields(memory: SparseMemory, object_address: int) -> dict[str, Any]:
        key_address = memory.read_u32(object_address) if object_address else 0
        key_sample = (
            memory.read(key_address, TITLE_FIXED_WIDTH_COMPARE_BYTES)
            if 0x00010000 <= key_address < 0x80000000
            else bytes()
        )
        return {
            "key_address_hex": _hex32(key_address),
            "key_sample_hex": key_sample.hex().upper(),
        }

    def observer(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        _trace: ExecutionTrace,
        steps: int,
    ) -> None:
        address = cpu.eip
        if address not in self.observer_addresses:
            return
        self.stage_counts[address] += 1
        esp = cpu.get_register("esp")
        if address in {
            TITLE_DSOUND_INIT_DEVICE_RESULT_ADDRESS,
            TITLE_DSOUND_INIT_CAPABILITY_RESULT_ADDRESS,
            TITLE_DSOUND_INIT_MIXBIN_CREATE_RESULT_ADDRESS,
            TITLE_DSOUND_INIT_MIXBIN_CONFIGURE_RESULT_ADDRESS,
            TITLE_DSOUND_INIT_MIXBIN_FINALIZE_RESULT_ADDRESS,
            TITLE_DSOUND_INIT_EXIT_ADDRESS,
            TITLE_DSOUND_DEVICE_CORE_RESULT_ADDRESS,
            TITLE_DSOUND_DEVICE_COMMAND_RESULT_ADDRESS,
            TITLE_DSOUND_DEVICE_EVENT_RESULT_ADDRESS,
            TITLE_DSOUND_DEVICE_VOICE_RESULT_ADDRESS,
            TITLE_DSOUND_CORE_MEMORY_RESULT_ADDRESS,
            TITLE_DSOUND_CORE_HARDWARE_RESULT_ADDRESS,
            TITLE_DSOUND_HARDWARE_VOICE0_CREATE_RESULT_ADDRESS,
            TITLE_DSOUND_HARDWARE_VOICE0_CONFIG_RESULT_ADDRESS,
            TITLE_DSOUND_HARDWARE_VOICE0_START_RESULT_ADDRESS,
            TITLE_DSOUND_HARDWARE_VOICE1_CONFIG_RESULT_ADDRESS,
            TITLE_DSOUND_HARDWARE_VOICE1_START_RESULT_ADDRESS,
        }:
            ebp = cpu.get_register("ebp")
            self.dsound_init_checkpoints.append(
                {
                    "steps": steps,
                    "address_hex": _hex32(address),
                    "eax_hex": _hex32(cpu.get_register("eax")),
                    "mixbin_index": memory.read_u32(_u32(ebp + 8)),
                }
            )
            return
        if address in {
            TITLE_FRONTEND_DESCRIPTOR_ALLOCATE_ADDRESS,
            TITLE_FRONTEND_DEFAULT_FACTORY_ADDRESS,
        }:
            descriptor = memory.read_u32(_u32(esp + 4))
            self.descriptor_allocations.append(
                {
                    "steps": steps,
                    "address_hex": _hex32(address),
                    "descriptor_hex": _hex32(descriptor),
                    "flags_hex": _hex32(memory.read_u32(_u32(descriptor + 0x40))),
                    "element_size": int.from_bytes(
                        memory.read(_u32(descriptor + 0x44), 2), "little"
                    ),
                    "element_count": int.from_bytes(
                        memory.read(_u32(descriptor + 0x46), 2), "little"
                    ),
                    "allocation_hex": _hex32(
                        memory.read_u32(_u32(descriptor + 0x48))
                    ),
                }
            )
            return
        if address in {
            TITLE_FRONTEND_OBJECT_CREATE_FACTORY_RESULT_ADDRESS,
            TITLE_FRONTEND_OBJECT_CREATE_INTERFACE_RESULT_ADDRESS,
            TITLE_FRONTEND_OBJECT_CREATE_FAILURE_ADDRESS,
            TITLE_FRONTEND_OBJECT_CREATE_SUCCESS_ADDRESS,
        }:
            temporary = cpu.get_register("esi")
            self.object_create_checkpoints.append(
                {
                    "steps": steps,
                    "address_hex": _hex32(address),
                    "eax_hex": _hex32(cpu.get_register("eax")),
                    "ecx_hex": _hex32(cpu.get_register("ecx")),
                    "esi_hex": _hex32(temporary),
                    "temporary_object_hex": _hex32(
                        memory.read_u32(temporary) if temporary else 0
                    ),
                    "temporary_interface_hex": _hex32(
                        memory.read_u32(_u32(temporary + 4)) if temporary else 0
                    ),
                }
            )
            return
        if address in {
            TITLE_FRONTEND_FACTORY_RESULT_ADDRESS,
            TITLE_FRONTEND_OBJECT_INITIALIZER_RESULT_ADDRESS,
            TITLE_FRONTEND_OBJECT_INITIALIZER_FAILURE_ADDRESS,
            TITLE_FRONTEND_OBJECT_INITIALIZER_SUCCESS_ADDRESS,
        }:
            constructed = (
                cpu.get_register("eax")
                if address == TITLE_FRONTEND_FACTORY_RESULT_ADDRESS
                else cpu.get_register("esi")
            )
            descriptor = memory.read_u32(constructed) if constructed else 0
            self.object_create_checkpoints.append(
                {
                    "steps": steps,
                    "address_hex": _hex32(address),
                    "eax_hex": _hex32(cpu.get_register("eax")),
                    "constructed_hex": _hex32(constructed),
                    "descriptor_hex": _hex32(descriptor),
                    "initializer_hex": _hex32(
                        memory.read_u32(_u32(descriptor + 0x28))
                        if descriptor
                        else 0
                    ),
                }
            )
            return
        if address in {
            TITLE_FRONTEND_OBJECT_AUDIO_CREATE_RESULT_ADDRESS,
            TITLE_FRONTEND_OBJECT_AUDIO_STATE_ALLOC_RESULT_ADDRESS,
            TITLE_FRONTEND_OBJECT_AUDIO_INIT_RESULT_ADDRESS,
        }:
            self.object_create_checkpoints.append(
                {
                    "steps": steps,
                    "address_hex": _hex32(address),
                    "eax_hex": _hex32(cpu.get_register("eax")),
                    "esi_hex": _hex32(cpu.get_register("esi")),
                    "edi_hex": _hex32(cpu.get_register("edi")),
                }
            )
            return
        if address in {
            TITLE_FRONTEND_CONSTRUCTOR_FRONTEND_OBJECT_RESULT_ADDRESS,
            TITLE_FRONTEND_CONSTRUCTOR_DESCRIPTOR_RESULT_ADDRESS,
            TITLE_FRONTEND_CONSTRUCTOR_FRONTEND_STATE_RESULT_ADDRESS,
            TITLE_FRONTEND_CONSTRUCTOR_RESOURCE_OBJECT_RESULT_ADDRESS,
            TITLE_FRONTEND_CONSTRUCTOR_RESOURCE_FAILURE_ADDRESS,
            TITLE_FRONTEND_CONSTRUCTOR_SUBSYSTEM_FAILURE_ADDRESS,
        }:
            owner = cpu.get_register("esi")
            self.constructor_checkpoints.append(
                {
                    "steps": steps,
                    "address_hex": _hex32(address),
                    "eax_hex": _hex32(cpu.get_register("eax")),
                    "edi_hex": _hex32(cpu.get_register("edi")),
                    "owner_hex": _hex32(owner),
                    "frontend_object_hex": _hex32(
                        memory.read_u32(_u32(owner + 0x58))
                    ),
                    "frontend_state_hex": _hex32(
                        memory.read_u32(_u32(owner + 0x5C))
                    ),
                    "resource_object_hex": _hex32(
                        memory.read_u32(_u32(owner + 0x124))
                    ),
                }
            )
            return
        if address == TITLE_FRONTEND_CONSTRUCTOR_RESULT_ADDRESS:
            owner = cpu.get_register("esi")
            self.constructor_results.append(
                {
                    "steps": steps,
                    "owner_hex": _hex32(owner),
                    "result_hex": _hex32(cpu.get_register("edi")),
                    "frontend_object_hex": _hex32(
                        memory.read_u32(_u32(owner + 0x58))
                    ),
                    "frontend_state_hex": _hex32(
                        memory.read_u32(_u32(owner + 0x5C))
                    ),
                    "resource_object_hex": _hex32(
                        memory.read_u32(_u32(owner + 0x124))
                    ),
                }
            )
            return
        if address == TITLE_FRONTEND_REGISTRY_LOOKUP_ADDRESS:
            key_address = memory.read_u32(_u32(esp + 8))
            record = {
                "steps": steps,
                "supplied_root_hex": _hex32(memory.read_u32(_u32(esp + 4))),
                "global_root_hex": _hex32(
                    memory.read_u32(TITLE_FRONTEND_COMPARE_SEARCH_GLOBAL_ROOT_ADDRESS)
                ),
                "key_address_hex": _hex32(key_address),
                "key_sample_hex": memory.read(
                    key_address, TITLE_FIXED_WIDTH_COMPARE_BYTES
                ).hex().upper(),
                "out_pointer_hex": _hex32(memory.read_u32(_u32(esp + 0x0C))),
                "result": None,
            }
            self.lookup_count += 1
            self._pending_lookups.append(record)
            if len(self.lookup_records) < self.sample_limit:
                self.lookup_records.append(record)
            return
        if address == TITLE_FRONTEND_REGISTRY_LOOKUP_RESULT_ADDRESS:
            if self._pending_lookups:
                record = self._pending_lookups.pop()
                record["result"] = cpu.get_register("eax")
                record["result_hex"] = _hex32(cpu.get_register("eax"))
                record["result_steps"] = steps
            return
        if address == TITLE_FRONTEND_REGISTRY_ATTACH_ADDRESS:
            parent = memory.read_u32(_u32(esp + 4))
            attached = memory.read_u32(_u32(esp + 8))
            record = {
                "steps": steps,
                "parent_hex": _hex32(parent),
                "attached_hex": _hex32(attached),
                "parent_key": self._key_fields(memory, parent),
                "attached_key": self._key_fields(memory, attached),
                "compatibility_result": None,
                "outcome": None,
            }
            self.attach_count += 1
            self._pending_attaches.append(record)
            if len(self.attach_records) < self.sample_limit:
                self.attach_records.append(record)
            return
        if address == TITLE_FRONTEND_REGISTRY_ATTACH_RESULT_ADDRESS:
            if self._pending_attaches:
                record = self._pending_attaches[-1]
                record["compatibility_result"] = cpu.get_register("eax")
                record["compatibility_result_hex"] = _hex32(
                    cpu.get_register("eax")
                )
                record["result_steps"] = steps
            return
        if address in {
            TITLE_FRONTEND_REGISTRY_ATTACH_FAILURE_ADDRESS,
            TITLE_FRONTEND_REGISTRY_ATTACH_SUCCESS_ADDRESS,
        } and self._pending_attaches:
            record = self._pending_attaches.pop()
            record["outcome"] = (
                "success"
                if address == TITLE_FRONTEND_REGISTRY_ATTACH_SUCCESS_ADDRESS
                else "incompatible"
            )
            record["outcome_steps"] = steps

    def summary(self) -> dict[str, Any]:
        return {
            "lookup_count": self.lookup_count,
            "lookup_miss_count": sum(
                record.get("result") == 0 for record in self.lookup_records
            ),
            "attach_count": self.attach_count,
            "incompatible_attach_count": sum(
                record.get("outcome") == "incompatible"
                for record in self.attach_records
            ),
            "target_class_init_count": self.stage_counts[
                TITLE_FRONTEND_REGISTRY_TARGET_CLASS_INIT_ADDRESS
            ],
            "subsystem_shutdown_count": self.stage_counts[
                TITLE_FRONTEND_SUBSYSTEM_SHUTDOWN_ADDRESS
            ],
            "stage_counts": {
                _hex32(address): self.stage_counts[address]
                for address in sorted(self.observer_addresses)
            },
            "lookup_records": self.lookup_records,
            "attach_records": self.attach_records,
            "constructor_checkpoints": self.constructor_checkpoints,
            "object_create_checkpoints": self.object_create_checkpoints,
            "descriptor_allocations": self.descriptor_allocations,
            "dsound_init_checkpoints": self.dsound_init_checkpoints,
            "constructor_results": self.constructor_results,
        }


class TitleFrontendRecordTableCountRepair:
    """Clamp the observed uninitialized frontend record-table count."""

    def __init__(self) -> None:
        self.observation_count = 0
        self.repair_count = 0
        self.last_repair: dict[str, Any] | None = None

    def observer(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        trace: ExecutionTrace,
        steps: int,
    ) -> None:
        if cpu.eip != TITLE_FRONTEND_RECORD_TABLE_SCAN_ADDRESS:
            return
        self.observation_count += 1
        table_address = cpu.get_register("edi")
        if not table_address:
            return
        count_address = _u32(table_address + 4)
        count = memory.read_u32(count_address)
        if count <= TITLE_FRONTEND_RECORD_TABLE_MAX_COUNT:
            return
        memory.write_u32(count_address, 0)
        self.repair_count += 1
        self.last_repair = {
            "steps": steps,
            "table_address_hex": _hex32(table_address),
            "count_address_hex": _hex32(count_address),
            "count_before_hex": _hex32(count),
            "count_after": 0,
        }
        trace.add(
            TITLE_FRONTEND_RECORD_TABLE_SCAN_ADDRESS,
            "title_frontend_record_table_count_repair",
            **self.last_repair,
        )

    def summary(self) -> dict[str, Any]:
        return {
            "scan_address_hex": _hex32(TITLE_FRONTEND_RECORD_TABLE_SCAN_ADDRESS),
            "observation_count": self.observation_count,
            "repair_count": self.repair_count,
            "last_repair": self.last_repair,
        }


class TitleFrontendPostAudioListRepair:
    """Close a synthetic frontend list node whose next link was left null."""

    def __init__(self) -> None:
        self.observation_count = 0
        self.repair_count = 0
        self.last_repair: dict[str, Any] | None = None

    def observer(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        trace: ExecutionTrace,
        steps: int,
    ) -> None:
        if cpu.eip != TITLE_FRONTEND_POST_AUDIO_LIST_ADVANCE_ADDRESS:
            return
        self.observation_count += 1
        current_link = cpu.get_register("esi")
        if current_link in {0, TITLE_FRONTEND_POST_AUDIO_LIST_SENTINEL_ADDRESS}:
            return
        next_link = memory.read_u32(current_link)
        if next_link != 0:
            return
        memory.write_u32(current_link, TITLE_FRONTEND_POST_AUDIO_LIST_SENTINEL_ADDRESS)
        self.repair_count += 1
        self.last_repair = {
            "steps": steps,
            "current_link_hex": _hex32(current_link),
            "next_link_before_hex": _hex32(next_link),
            "sentinel_hex": _hex32(TITLE_FRONTEND_POST_AUDIO_LIST_SENTINEL_ADDRESS),
        }
        trace.add(
            TITLE_FRONTEND_POST_AUDIO_LIST_ADVANCE_ADDRESS,
            "title_frontend_post_audio_list_repair",
            repair_count=self.repair_count,
            **self.last_repair,
        )

    def summary(self) -> dict[str, Any]:
        return {
            "advance_address_hex": _hex32(TITLE_FRONTEND_POST_AUDIO_LIST_ADVANCE_ADDRESS),
            "sentinel_address_hex": _hex32(TITLE_FRONTEND_POST_AUDIO_LIST_SENTINEL_ADDRESS),
            "observation_count": self.observation_count,
            "repair_count": self.repair_count,
            "last_repair": self.last_repair,
        }


class TitleFrontendStaticSingletonRepair:
    """Materialize the three title singletons whose CRT constructors were missed."""

    def __init__(self) -> None:
        self.observation_count = 0
        self.repair_count = 0
        self.repairs: list[dict[str, Any]] = []
        self.dynamic_object_transitions: list[dict[str, Any]] = []
        self._last_dynamic_pointer: int | None = None
        self._last_dynamic_vtable: int | None = None

    def observer(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        trace: ExecutionTrace,
        steps: int,
    ) -> None:
        dynamic_pointer = memory.read_u32(
            TITLE_FRONTEND_DYNAMIC_OBJECT_OWNER_ADDRESS
            + TITLE_FRONTEND_DYNAMIC_OBJECT_POINTER_OFFSET
        )
        dynamic_vtable = memory.read_u32(dynamic_pointer) if dynamic_pointer else 0
        if (
            dynamic_pointer != self._last_dynamic_pointer
            or dynamic_vtable != self._last_dynamic_vtable
        ):
            if len(self.dynamic_object_transitions) < 16:
                self.dynamic_object_transitions.append(
                    {
                        "steps": steps,
                        "next_instruction_hex": _hex32(cpu.eip),
                        "object_address_hex": _hex32(dynamic_pointer),
                        "vtable_address_hex": _hex32(dynamic_vtable),
                    }
                )
            self._last_dynamic_pointer = dynamic_pointer
            self._last_dynamic_vtable = dynamic_vtable
        if cpu.eip != TITLE_FRONTEND_STATIC_SINGLETON_USE_ADDRESS:
            return
        self.observation_count += 1
        repairs = (
            self._repair_record_singleton(
                memory,
                TITLE_FRONTEND_STATIC_SINGLETON_OBJECT_ADDRESS,
                TITLE_FRONTEND_STATIC_SINGLETON_VTABLE_ADDRESS,
                zero_offsets=(0x10C, 0x110, 0x114, 0x118),
                records_offset=TITLE_FRONTEND_STATIC_SINGLETON_RECORDS_OFFSET,
                record_count=TITLE_FRONTEND_STATIC_SINGLETON_RECORD_COUNT,
            ),
            self._repair_record_singleton(
                memory,
                TITLE_FRONTEND_STATIC_SINGLETON_AUDIO_OBJECT_ADDRESS,
                TITLE_FRONTEND_STATIC_SINGLETON_AUDIO_VTABLE_ADDRESS,
                zero_offsets=(0x40, 0x44, 0x48, 0x4C),
                records_offset=TITLE_FRONTEND_STATIC_SINGLETON_AUDIO_RECORDS_OFFSET,
                record_count=TITLE_FRONTEND_STATIC_SINGLETON_AUDIO_RECORD_COUNT,
            ),
            self._repair_effect_singleton(memory),
        )
        for repair in repairs:
            if repair is None:
                continue
            repair["steps"] = steps
            self.repair_count += 1
            self.repairs.append(repair)
            trace.add(
                TITLE_FRONTEND_STATIC_SINGLETON_USE_ADDRESS,
                "title_frontend_static_singleton_repair",
                repair_count=self.repair_count,
                **repair,
            )

    @staticmethod
    def _repair_record_singleton(
        memory: SparseMemory,
        object_address: int,
        vtable_address: int,
        *,
        zero_offsets: tuple[int, ...],
        records_offset: int,
        record_count: int,
    ) -> dict[str, Any] | None:
        vtable_before = memory.read_u32(object_address)
        if vtable_before != 0:
            return None
        memory.write_u32(object_address, vtable_address)
        for offset in zero_offsets:
            memory.write_u32(_u32(object_address + offset), 0)
        for index in range(record_count):
            record = _u32(
                object_address
                + records_offset
                + index * TITLE_FRONTEND_STATIC_SINGLETON_RECORD_SIZE
            )
            memory.write_u32(record, 1)
            for field_offset in range(4, TITLE_FRONTEND_STATIC_SINGLETON_RECORD_SIZE, 4):
                memory.write_u32(_u32(record + field_offset), 0)
        return {
            "object_address_hex": _hex32(object_address),
            "vtable_before_hex": _hex32(vtable_before),
            "vtable_after_hex": _hex32(vtable_address),
            "record_count": record_count,
        }

    @staticmethod
    def _repair_effect_singleton(memory: SparseMemory) -> dict[str, Any] | None:
        object_address = TITLE_FRONTEND_STATIC_SINGLETON_EFFECT_OBJECT_ADDRESS
        vtable_before = memory.read_u32(object_address)
        if vtable_before != 0:
            return None
        memory.write_u32(object_address, TITLE_FRONTEND_STATIC_SINGLETON_EFFECT_VTABLE_ADDRESS)
        for index in range(0x6A):
            memory.write_u32(_u32(object_address + 0x368 + index * 0x3C), 0x002B6B30)
            memory.write_u32(_u32(object_address + 0x1C40 + index * 0x48), 0x002B6B38)
        memory.write_u32(_u32(object_address + 0x3A10), 0x002B6B3C)
        return {
            "object_address_hex": _hex32(object_address),
            "vtable_before_hex": _hex32(vtable_before),
            "vtable_after_hex": _hex32(
                TITLE_FRONTEND_STATIC_SINGLETON_EFFECT_VTABLE_ADDRESS
            ),
            "record_count": 0x6A * 2 + 1,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "use_address_hex": _hex32(TITLE_FRONTEND_STATIC_SINGLETON_USE_ADDRESS),
            "object_address_hex": _hex32(
                TITLE_FRONTEND_STATIC_SINGLETON_OBJECT_ADDRESS
            ),
            "vtable_address_hex": _hex32(
                TITLE_FRONTEND_STATIC_SINGLETON_VTABLE_ADDRESS
            ),
            "observation_count": self.observation_count,
            "repair_count": self.repair_count,
            "repairs": self.repairs,
            "dynamic_object_transitions": self.dynamic_object_transitions,
        }


class TitleRuntimeObjectTableConstructorRepair:
    """Restore the missed CRT constructors for the runtime object table."""

    def __init__(self) -> None:
        self.observation_count = 0
        self.repair_count = 0
        self.repairs: list[dict[str, Any]] = []

    def observer(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        trace: ExecutionTrace,
        steps: int,
    ) -> None:
        if cpu.eip != TITLE_RUNTIME_OBJECT_TABLE_USE_ADDRESS:
            return
        self.observation_count += 1
        for object_address, vtable_address, records_offset, record_count in (
            TITLE_RUNTIME_OBJECT_CONSTRUCTOR_SPECS
        ):
            if memory.read_u32(object_address) != 0:
                continue
            memory.write_u32(object_address, vtable_address)
            for index in range(record_count):
                record = _u32(
                    object_address
                    + records_offset
                    + index * TITLE_FRONTEND_STATIC_SINGLETON_RECORD_SIZE
                )
                memory.write_u32(record, 1)
                for offset in range(4, TITLE_FRONTEND_STATIC_SINGLETON_RECORD_SIZE, 4):
                    memory.write_u32(_u32(record + offset), 0)
            if object_address == 0x004EC318:
                memory.write_u32(object_address + 0x5D8, 1)
                memory.write_u32(object_address + 0x5F0, 0xFFFFFFFF)
            elif object_address == 0x004ECF08:
                memory.write_u32(object_address + 0x2AC, 1)
                memory.write_u32(object_address + 0x2C4, 0xFFFFFFFF)
            repair = {
                "steps": steps,
                "object_address_hex": _hex32(object_address),
                "vtable_address_hex": _hex32(vtable_address),
                "records_offset_hex": _hex32(records_offset),
                "record_count": record_count,
            }
            self.repair_count += 1
            self.repairs.append(repair)
            trace.add(
                TITLE_RUNTIME_OBJECT_TABLE_USE_ADDRESS,
                "title_runtime_object_table_constructor_repair",
                repair_count=self.repair_count,
                **repair,
            )

    def summary(self) -> dict[str, Any]:
        return {
            "use_address_hex": _hex32(TITLE_RUNTIME_OBJECT_TABLE_USE_ADDRESS),
            "specification_count": len(TITLE_RUNTIME_OBJECT_CONSTRUCTOR_SPECS),
            "observation_count": self.observation_count,
            "repair_count": self.repair_count,
            "repairs": self.repairs,
        }


class TitleRuntimeCallbackListRepair:
    """Close the runtime callback list when its parsed owner has no valid nodes."""

    def __init__(self) -> None:
        self.observation_count = 0
        self.repair_count = 0
        self.repairs: list[dict[str, Any]] = []

    def observer(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        trace: ExecutionTrace,
        steps: int,
    ) -> None:
        if cpu.eip not in TITLE_RUNTIME_CALLBACK_DISPATCH_ADDRESSES:
            return
        self.observation_count += 1
        esp = cpu.get_register("esp")
        object_address = memory.read_u32(_u32(esp + 4))
        global_object = memory.read_u32(TITLE_RUNTIME_CALLBACK_GLOBAL_OBJECT_ADDRESS)
        synthesized = object_address == 0
        if synthesized:
            object_address = TITLE_RUNTIME_CALLBACK_SYNTHETIC_OBJECT_ADDRESS
            memory.write_u32(_u32(esp + 4), object_address)
            memory.write_u32(TITLE_RUNTIME_CALLBACK_GLOBAL_OBJECT_ADDRESS, object_address)
            memory.write_u32(
                object_address,
                TITLE_RUNTIME_CALLBACK_SYNTHETIC_VTABLE_ADDRESS,
            )
            memory.write_u32(
                TITLE_RUNTIME_CALLBACK_SYNTHETIC_VTABLE_ADDRESS + 0x2C,
                0,
            )
            memory.write_u32(
                TITLE_RUNTIME_CALLBACK_SYNTHETIC_VTABLE_ADDRESS + 0x30,
                0,
            )
            global_object = object_address
        if object_address != global_object:
            return
        sentinel = _u32(object_address + TITLE_RUNTIME_CALLBACK_LIST_OFFSET)
        next_link = memory.read_u32(sentinel)
        previous_link = memory.read_u32(_u32(sentinel + 4))
        if next_link == sentinel and previous_link == sentinel:
            return
        memory.write_u32(sentinel, sentinel)
        memory.write_u32(_u32(sentinel + 4), sentinel)
        repair = {
            "steps": steps,
            "dispatch_address_hex": _hex32(cpu.eip),
            "object_address_hex": _hex32(object_address),
            "sentinel_address_hex": _hex32(sentinel),
            "next_link_before_hex": _hex32(next_link),
            "previous_link_before_hex": _hex32(previous_link),
            "synthesized_owner": synthesized,
        }
        self.repair_count += 1
        self.repairs.append(repair)
        trace.add(
            cpu.eip,
            "title_runtime_callback_list_repair",
            repair_count=self.repair_count,
            **repair,
        )

    def summary(self) -> dict[str, Any]:
        return {
            "dispatch_addresses_hex": [
                _hex32(address) for address in TITLE_RUNTIME_CALLBACK_DISPATCH_ADDRESSES
            ],
            "global_object_address_hex": _hex32(
                TITLE_RUNTIME_CALLBACK_GLOBAL_OBJECT_ADDRESS
            ),
            "observation_count": self.observation_count,
            "repair_count": self.repair_count,
            "repairs": self.repairs,
        }


class TitleD3DFlushFastPath:
    """Fast-path the observed D3D push-buffer flush/progress helper."""

    def __init__(self, *, flush_address: int = TITLE_D3D_FLUSH_ADDRESS) -> None:
        self.flush_address = flush_address
        self.invocation_count = 0
        self.progress_update_count = 0
        self.last_context_address: int | None = None
        self.last_progress_value: int | None = None

    def call_handlers(
        self,
    ) -> dict[int, Callable[[CpuState, SparseMemory, int, ExecutionTrace], None]]:
        return {self.flush_address: self.flush_handler}

    def flush_handler(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
    ) -> None:
        context_address = cpu.get_register("ecx")
        current_push_address = memory.read_u32(context_address) if context_address else 0
        progress_value = current_push_address & TITLE_GPU_COMPLETION_MASK
        dma_state_address = (
            memory.read_u32(_u32(context_address + 0x17F4))
            if context_address
            else 0
        )
        if dma_state_address:
            memory.write_u32(_u32(dma_state_address + 0x40), progress_value)
            self.progress_update_count += 1

        cpu.set_register("eax", 0)
        cpu.set_register("ecx", context_address)
        self.invocation_count += 1
        self.last_context_address = context_address
        self.last_progress_value = progress_value
        trace.add(
            target,
            "title_d3d_flush_fast_path",
            invocation_count=self.invocation_count,
            context_address=context_address,
            context_address_hex=_hex32(context_address),
            current_push_address=current_push_address,
            current_push_address_hex=_hex32(current_push_address),
            progress_value=progress_value,
            progress_value_hex=_hex32(progress_value),
            dma_state_address=dma_state_address,
            dma_state_address_hex=_hex32(dma_state_address),
        )

    def summary(self) -> dict[str, Any]:
        return {
            "flush_address": self.flush_address,
            "flush_address_hex": _hex32(self.flush_address),
            "invocation_count": self.invocation_count,
            "progress_update_count": self.progress_update_count,
            "last_context_address_hex": _hex32(self.last_context_address or 0)
            if self.last_context_address is not None
            else None,
            "last_progress_value_hex": _hex32(self.last_progress_value or 0)
            if self.last_progress_value is not None
            else None,
        }


def _emit_title_d3d_marker_packet(
    memory: SparseMemory,
    *,
    context_address: int,
    packet_size: int,
) -> dict[str, Any]:
    packet_address = memory.read_u32(context_address) if context_address else 0
    packet_end_address = (
        memory.read_u32(_u32(context_address + 4)) if context_address else 0
    )
    ring_base = (
        memory.read_u32(_u32(context_address + 0x24)) if context_address else 0
    )
    put_value = (
        memory.read_u32(_u32(context_address + 0x2C)) if context_address else 0
    )

    wrapped = False
    if (
        not packet_address
        or not packet_end_address
        or _u32(packet_address + packet_size) > packet_end_address
    ):
        packet_address = ring_base or TITLE_D3D_PUSH_BUFFER_BASE_ADDRESS
        wrapped = True

    memory.write_u32(packet_address, 0x00041D70)
    memory.write_u32(_u32(packet_address + 0x04), put_value)
    memory.write_u32(_u32(packet_address + 0x08), 0x00041D90)
    memory.write_u32(_u32(packet_address + 0x0C), 0)
    memory.write_u32(_u32(packet_address + 0x10), 0x00041D90)
    memory.write_u32(_u32(packet_address + 0x14), 0)
    if context_address:
        memory.write_u32(
            _u32(context_address + 0x00),
            _u32(packet_address + packet_size),
        )
        memory.write_u32(_u32(context_address + 0x2C), _u32(put_value + 2))

    return {
        "packet_address": packet_address,
        "put_value": put_value,
        "wrapped": wrapped,
    }


class TitleD3DPacketAllocFastPath:
    """Fast-path the observed D3D packet allocation/emission helper."""

    def __init__(
        self,
        *,
        allocation_address: int = TITLE_D3D_PACKET_ALLOC_ADDRESS,
        packet_size: int = TITLE_D3D_PACKET_ALLOC_SIZE,
    ) -> None:
        self.allocation_address = allocation_address
        self.packet_size = packet_size
        self.invocation_count = 0
        self.wrap_count = 0
        self.flush_requested_count = 0
        self.last_context_address: int | None = None
        self.last_packet_address: int | None = None
        self.last_put_value: int | None = None

    def call_handlers(
        self,
    ) -> dict[int, Callable[[CpuState, SparseMemory, int, ExecutionTrace], None]]:
        return {self.allocation_address: self.allocation_handler}

    def allocation_handler(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
    ) -> None:
        esp = cpu.get_register("esp")
        flags = memory.read_u32(_u32(esp + 4))
        context_address = memory.read_u32(TITLE_D3D_CONTEXT_GLOBAL_ADDRESS)
        packet = _emit_title_d3d_marker_packet(
            memory,
            context_address=context_address,
            packet_size=self.packet_size,
        )
        packet_address = int(packet["packet_address"])
        put_value = int(packet["put_value"])
        wrapped = bool(packet["wrapped"])
        if wrapped:
            self.wrap_count += 1
        if flags & 0x2 and context_address:
            dma_state_address = memory.read_u32(_u32(context_address + 0x17F4))
            if dma_state_address:
                memory.write_u32(
                    _u32(dma_state_address + 0x40),
                    put_value & TITLE_GPU_COMPLETION_MASK,
                )
            self.flush_requested_count += 1

        cpu.set_register("eax", put_value)
        _prepare_stdcall_return(cpu, memory, 4)
        self.invocation_count += 1
        self.last_context_address = context_address
        self.last_packet_address = packet_address
        self.last_put_value = put_value
        trace.add(
            target,
            "title_d3d_packet_alloc_fast_path",
            invocation_count=self.invocation_count,
            context_address=context_address,
            context_address_hex=_hex32(context_address),
            packet_address=packet_address,
            packet_address_hex=_hex32(packet_address),
            put_value=put_value,
            put_value_hex=_hex32(put_value),
            flags=flags,
            flags_hex=_hex32(flags),
            wrapped=wrapped,
        )

    def summary(self) -> dict[str, Any]:
        return {
            "allocation_address": self.allocation_address,
            "allocation_address_hex": _hex32(self.allocation_address),
            "packet_size": self.packet_size,
            "invocation_count": self.invocation_count,
            "wrap_count": self.wrap_count,
            "flush_requested_count": self.flush_requested_count,
            "last_context_address_hex": _hex32(self.last_context_address or 0)
            if self.last_context_address is not None
            else None,
            "last_packet_address_hex": _hex32(self.last_packet_address or 0)
            if self.last_packet_address is not None
            else None,
            "last_put_value_hex": _hex32(self.last_put_value or 0)
            if self.last_put_value is not None
            else None,
        }


class TitleD3DReserveFastPath:
    """Fast-path the observed D3D push-buffer reserve helper."""

    def __init__(
        self,
        *,
        reserve_address: int = TITLE_D3D_RESERVE_ADDRESS,
        packet_size: int = TITLE_D3D_PACKET_ALLOC_SIZE,
    ) -> None:
        self.reserve_address = reserve_address
        self.packet_size = packet_size
        self.invocation_count = 0
        self.limit_update_count = 0
        self.marker_packet_count = 0
        self.wrap_count = 0
        self.flush_progress_update_count = 0
        self.last_context_address: int | None = None
        self.last_reserved_start_address: int | None = None
        self.last_reserved_limit_address: int | None = None
        self.last_return_address: int | None = None

    def call_handlers(
        self,
    ) -> dict[int, Callable[[CpuState, SparseMemory, int, ExecutionTrace], None]]:
        return {self.reserve_address: self.reserve_handler}

    def reserve_handler(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
    ) -> None:
        esp = cpu.get_register("esp")
        half_reserve_bytes = memory.read_u32(_u32(esp + 4))
        full_reserve_bytes = memory.read_u32(_u32(esp + 8))
        context_address = memory.read_u32(TITLE_D3D_CONTEXT_GLOBAL_ADDRESS)
        current_address = memory.read_u32(context_address) if context_address else 0
        ring_base = (
            memory.read_u32(_u32(context_address + 0x24)) if context_address else 0
        )
        ring_end = (
            memory.read_u32(_u32(context_address + 0x28)) if context_address else 0
        )
        if not ring_base:
            ring_base = TITLE_D3D_PUSH_BUFFER_BASE_ADDRESS
        if not ring_end:
            ring_end = TITLE_D3D_PUSH_BUFFER_END_ADDRESS

        reserved_start = current_address or ring_base
        wrapped = False
        if reserved_start < ring_base or reserved_start >= ring_end:
            reserved_start = ring_base
            wrapped = True

        guarded_end = _u32(
            reserved_start + full_reserve_bytes + TITLE_D3D_RESERVE_GUARD_BYTES
        )
        if guarded_end > ring_end:
            half_end = _u32(reserved_start + half_reserve_bytes)
            if half_end > ring_end:
                reserved_start = ring_base
                reserved_end = min(
                    _u32(reserved_start + full_reserve_bytes),
                    ring_end,
                )
                wrapped = True
            else:
                reserved_end = ring_end
        else:
            reserved_end = _u32(reserved_start + full_reserve_bytes)
            if reserved_end > ring_end:
                reserved_end = ring_end

        limit_address = reserved_end
        if limit_address > _u32(reserved_start + TITLE_D3D_RESERVE_LIMIT_MARGIN):
            limit_address = _u32(limit_address - TITLE_D3D_RESERVE_LIMIT_MARGIN)
        minimum_limit = _u32(reserved_start + self.packet_size)
        if limit_address < minimum_limit:
            limit_address = min(minimum_limit, ring_end)

        if context_address:
            memory.write_u32(_u32(context_address + 0x00), reserved_start)
            memory.write_u32(_u32(context_address + 0x04), limit_address)
            get_pointer_address = memory.read_u32(_u32(context_address + 0x30))
            put_value = memory.read_u32(_u32(context_address + 0x2C))
            if get_pointer_address:
                memory.write_u32(get_pointer_address, put_value)
            self.limit_update_count += 1

        packet = _emit_title_d3d_marker_packet(
            memory,
            context_address=context_address,
            packet_size=self.packet_size,
        )
        packet_address = int(packet["packet_address"])
        packet_wrapped = bool(packet["wrapped"])
        if packet_wrapped and not wrapped:
            wrapped = True
        if wrapped:
            self.wrap_count += 1
        self.marker_packet_count += 1

        return_address = (
            memory.read_u32(context_address) if context_address else packet_address
        )
        if context_address:
            memory.write_u32(TITLE_GPU_SUBMISSION_BASE_ADDRESS, return_address)
            memory.write_u32(TITLE_GPU_SUBMISSION_LIMIT_ADDRESS, limit_address)
        if context_address:
            dma_state_address = memory.read_u32(_u32(context_address + 0x17F4))
            if dma_state_address:
                memory.write_u32(
                    _u32(dma_state_address + 0x40),
                    return_address & TITLE_GPU_COMPLETION_MASK,
                )
                self.flush_progress_update_count += 1

        cpu.set_register("eax", return_address)
        _prepare_stdcall_return(cpu, memory, TITLE_D3D_RESERVE_STACK_CLEANUP)
        self.invocation_count += 1
        self.last_context_address = context_address
        self.last_reserved_start_address = reserved_start
        self.last_reserved_limit_address = limit_address
        self.last_return_address = return_address
        trace.add(
            target,
            "title_d3d_reserve_fast_path",
            invocation_count=self.invocation_count,
            context_address=context_address,
            context_address_hex=_hex32(context_address),
            current_address=current_address,
            current_address_hex=_hex32(current_address),
            half_reserve_bytes=half_reserve_bytes,
            full_reserve_bytes=full_reserve_bytes,
            reserved_start_address=reserved_start,
            reserved_start_address_hex=_hex32(reserved_start),
            reserved_limit_address=limit_address,
            reserved_limit_address_hex=_hex32(limit_address),
            return_address=return_address,
            return_address_hex=_hex32(return_address),
            packet_address=packet_address,
            packet_address_hex=_hex32(packet_address),
            wrapped=wrapped,
        )

    def summary(self) -> dict[str, Any]:
        return {
            "reserve_address": self.reserve_address,
            "reserve_address_hex": _hex32(self.reserve_address),
            "packet_size": self.packet_size,
            "invocation_count": self.invocation_count,
            "limit_update_count": self.limit_update_count,
            "marker_packet_count": self.marker_packet_count,
            "wrap_count": self.wrap_count,
            "flush_progress_update_count": self.flush_progress_update_count,
            "last_context_address_hex": _hex32(self.last_context_address or 0)
            if self.last_context_address is not None
            else None,
            "last_reserved_start_address_hex": _hex32(
                self.last_reserved_start_address or 0
            )
            if self.last_reserved_start_address is not None
            else None,
            "last_reserved_limit_address_hex": _hex32(
                self.last_reserved_limit_address or 0
            )
            if self.last_reserved_limit_address is not None
            else None,
            "last_return_address_hex": _hex32(self.last_return_address or 0)
            if self.last_return_address is not None
            else None,
        }


class TitleD3DPrimitiveDrawFastPath:
    """Audit the recovered D3D Clear entry; retain the legacy schema name."""

    def __init__(
        self,
        *,
        draw_address: int = TITLE_D3D_PRIMITIVE_DRAW_ADDRESS,
        stack_cleanup: int = TITLE_D3D_PRIMITIVE_DRAW_STACK_CLEANUP,
        argument_count: int = TITLE_D3D_PRIMITIVE_DRAW_ARGUMENT_COUNT,
        sample_limit: int = TITLE_D3D_PRIMITIVE_DRAW_SAMPLE_LIMIT,
        render_watchpoint: RenderWriteWatchpoint | None = None,
        execute_fast_path: bool = False,
    ) -> None:
        self.draw_address = draw_address
        self.stack_cleanup = stack_cleanup
        self.argument_count = argument_count
        self.sample_limit = sample_limit
        self.render_watchpoint = render_watchpoint
        self.execute_fast_path = execute_fast_path
        self.invocation_count = 0
        self.last_arguments: tuple[int, ...] = ()
        self.last_context_address: int | None = None
        self.last_put_address: int | None = None
        self.sampled_invocations: list[dict[str, Any]] = []
        self.latest_invocations: deque[dict[str, Any]] = deque(maxlen=sample_limit)
        self.caller_return_counts: Counter[int] = Counter()

    def call_handlers(
        self,
    ) -> dict[int, Callable[[CpuState, SparseMemory, int, ExecutionTrace], None]]:
        return {self.draw_address: self.draw_handler} if self.execute_fast_path else {}

    @property
    def observer_addresses(self) -> set[int]:
        return {self.draw_address} if not self.execute_fast_path else set()

    def observer(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        trace: ExecutionTrace,
        _steps: int,
    ) -> None:
        if self.execute_fast_path or cpu.eip != self.draw_address:
            return
        self._record_invocation(cpu, memory, self.draw_address, trace, mutate=False)

    def draw_handler(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
    ) -> None:
        self._record_invocation(cpu, memory, target, trace, mutate=True)

    def _record_invocation(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
        *,
        mutate: bool,
    ) -> None:
        esp = cpu.get_register("esp")
        caller_return_address = memory.read_u32(esp)
        arguments = tuple(
            memory.read_u32(_u32(esp + 4 + index * 4))
            for index in range(self.argument_count)
        )
        context_address = memory.read_u32(TITLE_D3D_CONTEXT_GLOBAL_ADDRESS)
        put_address = memory.read_u32(context_address) if context_address else 0

        if mutate:
            cpu.set_register("eax", put_address)
            _prepare_stdcall_return(cpu, memory, self.stack_cleanup)
        self.invocation_count += 1
        self.last_arguments = arguments
        self.last_context_address = context_address
        self.last_put_address = put_address
        self.caller_return_counts[caller_return_address] += 1
        sample = {
            "invocation_count": self.invocation_count,
            "arguments": [_hex32(argument) for argument in arguments],
            "context_address_hex": _hex32(context_address),
            "put_address_hex": _hex32(put_address),
            "caller_return_address_hex": _hex32(caller_return_address),
            "render_write_count": (
                self.render_watchpoint.write_count
                if self.render_watchpoint is not None
                else None
            ),
            "guest_flip_count": (
                self.render_watchpoint.flip_count
                if self.render_watchpoint is not None
                else None
            ),
        }
        if len(self.sampled_invocations) < self.sample_limit:
            self.sampled_invocations.append(sample)
        self.latest_invocations.append(sample)
        if trace.enabled:
            trace.add(
                target,
                (
                    "title_d3d_primitive_draw_fast_path"
                    if mutate
                    else "title_d3d_primitive_draw_recovered_entry"
                ),
                invocation_count=self.invocation_count,
                arguments=sample["arguments"],
                context_address=context_address,
                context_address_hex=sample["context_address_hex"],
                put_address=put_address,
                put_address_hex=sample["put_address_hex"],
                caller_return_address=caller_return_address,
                caller_return_address_hex=sample["caller_return_address_hex"],
                render_write_count=sample["render_write_count"],
                guest_flip_count=sample["guest_flip_count"],
                stack_cleanup=self.stack_cleanup,
            )

    def summary(self) -> dict[str, Any]:
        samples_by_invocation = {
            int(sample["invocation_count"]): sample
            for sample in [*self.sampled_invocations, *self.latest_invocations]
        }
        return {
            "draw_address": self.draw_address,
            "draw_address_hex": _hex32(self.draw_address),
            "stack_cleanup": self.stack_cleanup,
            "argument_count": self.argument_count,
            "execution_mode": (
                "return_only_fast_path"
                if self.execute_fast_path
                else "recovered_guest_code"
            ),
            "operation": "clear",
            "invocation_count": self.invocation_count,
            "last_context_address_hex": _hex32(self.last_context_address or 0)
            if self.last_context_address is not None
            else None,
            "last_put_address_hex": _hex32(self.last_put_address or 0)
            if self.last_put_address is not None
            else None,
            "last_arguments": [_hex32(argument) for argument in self.last_arguments],
            "sampled_invocations": [
                sample for _, sample in sorted(samples_by_invocation.items())
            ],
            "caller_return_counts": [
                {
                    "caller_return_address_hex": _hex32(address),
                    "invocation_count": count,
                }
                for address, count in self.caller_return_counts.most_common()
            ],
        }


class TitleImmediateDrawAudit:
    """Observe immediate-array draw submissions without changing guest execution."""

    def __init__(
        self,
        *,
        draw_address: int = TITLE_IMMEDIATE_DRAW_ADDRESS,
        sample_limit: int = TITLE_IMMEDIATE_DRAW_SAMPLE_LIMIT,
        max_vertices: int = TITLE_IMMEDIATE_DRAW_MAX_VERTICES,
        render_watchpoint: RenderWriteWatchpoint | None = None,
    ) -> None:
        self.draw_address = draw_address
        self.sample_limit = max(2, sample_limit)
        self.max_vertices = max_vertices
        self.render_watchpoint = render_watchpoint
        self.invocation_count = 0
        self.invalid_submission_count = 0
        self.caller_return_counts: Counter[int] = Counter()
        self.first_invocations: list[dict[str, Any]] = []
        self.latest_invocations: deque[dict[str, Any]] = deque(
            maxlen=self.sample_limit // 2
        )
        self.flip_geometry_stats: dict[int, dict[str, Any]] = {}

    @property
    def observer_addresses(self) -> set[int]:
        return {self.draw_address}

    def observer(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        trace: ExecutionTrace,
        _steps: int,
    ) -> None:
        if cpu.eip != self.draw_address:
            return
        esp = cpu.get_register("esp")
        caller_return_address = memory.read_u32(esp)
        primitive_type = memory.read_u32(_u32(esp + 4))
        vertex_count = memory.read_u32(_u32(esp + 8))
        vertex_data_address = memory.read_u32(_u32(esp + 12))
        stride = memory.read_u32(_u32(esp + 16))
        guest_flip_count = (
            self.render_watchpoint.flip_count
            if self.render_watchpoint is not None
            else None
        )
        render_write_count = (
            self.render_watchpoint.write_count
            if self.render_watchpoint is not None
            else None
        )
        invalid_reason = None
        if vertex_count == 0:
            invalid_reason = "zero_vertex_count"
        elif vertex_count > self.max_vertices:
            invalid_reason = "vertex_count_exceeds_audit_limit"
        elif vertex_data_address == 0:
            invalid_reason = "null_vertex_data"
        elif stride < 8 or stride > 0x400:
            invalid_reason = "invalid_vertex_stride"

        finite_vertex_count = 0
        non_finite_vertex_count = 0
        min_x: float | None = None
        max_x: float | None = None
        min_y: float | None = None
        max_y: float | None = None
        if invalid_reason is None:
            for vertex_index in range(vertex_count):
                address = _u32(vertex_data_address + vertex_index * stride)
                x = struct.unpack("<f", struct.pack("<I", memory.read_u32(address)))[0]
                y = struct.unpack(
                    "<f", struct.pack("<I", memory.read_u32(_u32(address + 4)))
                )[0]
                if not math.isfinite(x) or not math.isfinite(y):
                    non_finite_vertex_count += 1
                    continue
                finite_vertex_count += 1
                min_x = x if min_x is None else min(min_x, x)
                max_x = x if max_x is None else max(max_x, x)
                min_y = y if min_y is None else min(min_y, y)
                max_y = y if max_y is None else max(max_y, y)

        self.invocation_count += 1
        self.caller_return_counts[caller_return_address] += 1
        if invalid_reason is not None:
            self.invalid_submission_count += 1
        sample = {
            "invocation_count": self.invocation_count,
            "caller_return_address_hex": _hex32(caller_return_address),
            "primitive_type": primitive_type,
            "vertex_count": vertex_count,
            "vertex_data_address_hex": _hex32(vertex_data_address),
            "stride": stride,
            "finite_vertex_count": finite_vertex_count,
            "non_finite_vertex_count": non_finite_vertex_count,
            "min_x": min_x,
            "max_x": max_x,
            "min_y": min_y,
            "max_y": max_y,
            "invalid_reason": invalid_reason,
            "render_write_count": render_write_count,
            "guest_flip_count": guest_flip_count,
        }
        if len(self.first_invocations) < self.sample_limit // 2:
            self.first_invocations.append(sample)
        self.latest_invocations.append(sample)

        if guest_flip_count is not None:
            flip = self.flip_geometry_stats.setdefault(
                guest_flip_count,
                {
                    "guest_flip_count": guest_flip_count,
                    "draw_call_count": 0,
                    "vertex_count": 0,
                    "finite_vertex_count": 0,
                    "non_finite_vertex_count": 0,
                    "invalid_submission_count": 0,
                    "primitive_type_counts": Counter(),
                    "min_x": None,
                    "max_x": None,
                    "min_y": None,
                    "max_y": None,
                },
            )
            flip["draw_call_count"] += 1
            flip["vertex_count"] += vertex_count
            flip["finite_vertex_count"] += finite_vertex_count
            flip["non_finite_vertex_count"] += non_finite_vertex_count
            flip["primitive_type_counts"][primitive_type] += 1
            if invalid_reason is not None:
                flip["invalid_submission_count"] += 1
            for key, value, reducer in (
                ("min_x", min_x, min),
                ("max_x", max_x, max),
                ("min_y", min_y, min),
                ("max_y", max_y, max),
            ):
                if value is not None:
                    flip[key] = value if flip[key] is None else reducer(flip[key], value)

        if trace.enabled:
            trace.add(
                self.draw_address,
                "title_immediate_draw_submission",
                **sample,
            )

    def summary(self) -> dict[str, Any]:
        samples_by_invocation = {
            int(sample["invocation_count"]): sample
            for sample in [*self.first_invocations, *self.latest_invocations]
        }
        geometry_by_guest_flip = []
        for flip_count, stats in sorted(self.flip_geometry_stats.items()):
            geometry_by_guest_flip.append(
                {
                    **{
                        key: value
                        for key, value in stats.items()
                        if key != "primitive_type_counts"
                    },
                    "primitive_type_counts": [
                        {
                            "primitive_type": primitive_type,
                            "draw_call_count": count,
                        }
                        for primitive_type, count in stats[
                            "primitive_type_counts"
                        ].most_common()
                    ],
                }
            )
        return {
            "draw_address": self.draw_address,
            "draw_address_hex": _hex32(self.draw_address),
            "execution_mode": "observer_only_recovered_guest_code",
            "argument_count": TITLE_IMMEDIATE_DRAW_ARGUMENT_COUNT,
            "max_vertices_per_submission": self.max_vertices,
            "invocation_count": self.invocation_count,
            "invalid_submission_count": self.invalid_submission_count,
            "caller_return_counts": [
                {
                    "caller_return_address_hex": _hex32(address),
                    "invocation_count": count,
                }
                for address, count in self.caller_return_counts.most_common()
            ],
            "sampled_invocations": [
                sample for _, sample in sorted(samples_by_invocation.items())
            ],
            "geometry_by_guest_flip": geometry_by_guest_flip,
        }


class TitleVertexAppendFastPath:
    """Fast-path the observed immediate vertex-list append helper."""

    def __init__(
        self,
        *,
        append_address: int = TITLE_VERTEX_APPEND_ADDRESS,
        count_offset: int = TITLE_VERTEX_APPEND_COUNT_OFFSET,
        default_z_offset: int = TITLE_VERTEX_APPEND_DEFAULT_Z_OFFSET,
        stride: int = TITLE_VERTEX_APPEND_STRIDE,
        render_watchpoint: RenderWriteWatchpoint | None = None,
        anomaly_sample_limit: int = TITLE_VERTEX_APPEND_ANOMALY_SAMPLE_LIMIT,
    ) -> None:
        self.append_address = append_address
        self.count_offset = count_offset
        self.default_z_offset = default_z_offset
        self.stride = stride
        self.render_watchpoint = render_watchpoint
        self.anomaly_sample_limit = max(2, anomaly_sample_limit)
        self.invocation_count = 0
        self.last_object_address: int | None = None
        self.last_record_address: int | None = None
        self.last_vertex_index: int | None = None
        self.max_vertex_index = 0
        self.caller_stats: dict[int, dict[str, Any]] = {}
        self.upstream_caller_stats: dict[int, dict[str, Any]] = {}
        self.anomaly_counts: Counter[str] = Counter()
        self.first_anomaly_samples: list[dict[str, Any]] = []
        self.latest_anomaly_samples: deque[dict[str, Any]] = deque(
            maxlen=self.anomaly_sample_limit // 2
        )
        self.sampled_anomaly_flips: set[int] = set()
        self.recent_vertices: deque[tuple[Any, ...]] = deque(maxlen=8)
        self.flip_geometry_stats: dict[int, dict[str, Any]] = {}
        self.finite_min_x: float | None = None
        self.finite_max_x: float | None = None
        self.finite_min_y: float | None = None
        self.finite_max_y: float | None = None

    def call_handlers(
        self,
    ) -> dict[int, Callable[[CpuState, SparseMemory, int, ExecutionTrace], None]]:
        return {self.append_address: self.append_handler}

    def append_handler(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
    ) -> None:
        object_address = cpu.get_register("ecx")
        esp = cpu.get_register("esp")
        caller_return_address = memory.read_u32(esp)
        upstream_return_address = (
            memory.read_u32(
                _u32(esp + TITLE_VERTEX_APPEND_QUAD_PARENT_RETURN_STACK_OFFSET)
            )
            if TITLE_QUAD_SUBMIT_ADDRESS
            <= caller_return_address
            <= TITLE_QUAD_SUBMIT_END_ADDRESS
            else 0
        )
        entry_eax = cpu.get_register("eax")
        x_bits = memory.read_u32(_u32(esp + 4))
        y_bits = memory.read_u32(_u32(esp + 8))
        color_vector_address = memory.read_u32(_u32(esp + 12))
        tail0_bits = memory.read_u32(_u32(esp + 16))
        tail1_bits = memory.read_u32(_u32(esp + 20))
        vertex_index = memory.read_u32(_u32(object_address + self.count_offset))
        record_address = _u32(object_address + vertex_index * self.stride)
        default_z_bits = memory.read_u32(_u32(object_address + self.default_z_offset))
        packed_color = _pack_title_vertex_color(memory, color_vector_address)
        x = struct.unpack("<f", struct.pack("<I", x_bits))[0]
        y = struct.unpack("<f", struct.pack("<I", y_bits))[0]
        render_write_count = (
            self.render_watchpoint.write_count
            if self.render_watchpoint is not None
            else None
        )
        guest_flip_count = (
            self.render_watchpoint.flip_count
            if self.render_watchpoint is not None
            else None
        )

        memory.write_u32(record_address, x_bits)
        memory.write_u32(_u32(record_address + 0x04), y_bits)
        memory.write_u32(_u32(record_address + 0x08), default_z_bits)
        memory.write_u32(_u32(record_address + 0x10), packed_color)
        memory.write_u32(_u32(record_address + 0x14), tail0_bits)
        memory.write_u32(_u32(record_address + 0x18), tail1_bits)
        memory.write_u32(_u32(object_address + self.count_offset), _u32(vertex_index + 1))

        cpu.set_register("eax", object_address)
        _prepare_stdcall_return(cpu, memory, TITLE_VERTEX_APPEND_STACK_CLEANUP)
        self.invocation_count += 1
        self.last_object_address = object_address
        self.last_record_address = record_address
        self.last_vertex_index = vertex_index
        self.max_vertex_index = max(self.max_vertex_index, vertex_index)
        anomalies: list[str] = []
        if not math.isfinite(x) or not math.isfinite(y):
            anomalies.append("non_finite_position")
        else:
            self.finite_min_x = x if self.finite_min_x is None else min(self.finite_min_x, x)
            self.finite_max_x = x if self.finite_max_x is None else max(self.finite_max_x, x)
            self.finite_min_y = y if self.finite_min_y is None else min(self.finite_min_y, y)
            self.finite_max_y = y if self.finite_max_y is None else max(self.finite_max_y, y)
            if x < -640.0 or x > 1280.0 or y < -480.0 or y > 960.0:
                anomalies.append("outside_reference_envelope")
        if x_bits == 0x43A00000 and (y_bits & 0x7FFFFFFF) == 0:
            anomalies.append("exact_center_origin")

        caller_stat = self._record_producer(
            self.caller_stats,
            caller_return_address,
            object_address=object_address,
            x=x,
            y=y,
            anomalies=anomalies,
            render_write_count=render_write_count,
            guest_flip_count=guest_flip_count,
        )
        if 0x00010000 <= upstream_return_address < 0x00300000:
            self._record_producer(
                self.upstream_caller_stats,
                upstream_return_address,
                object_address=object_address,
                x=x,
                y=y,
                anomalies=anomalies,
                render_write_count=render_write_count,
                guest_flip_count=guest_flip_count,
            )
        if guest_flip_count is not None:
            self._record_flip_geometry(
                guest_flip_count,
                upstream_return_address=upstream_return_address,
                x=x,
                y=y,
                anomalies=anomalies,
            )
        if anomalies:
            self.anomaly_counts.update(anomalies)

        recent_vertex = (
            self.invocation_count,
            object_address,
            record_address,
            vertex_index,
            x if math.isfinite(x) else None,
            y if math.isfinite(y) else None,
            x_bits,
            y_bits,
            caller_return_address,
            upstream_return_address,
            render_write_count,
            guest_flip_count,
        )
        self.recent_vertices.append(recent_vertex)
        if anomalies:
            anomaly_total = sum(self.anomaly_counts.values())
            should_sample = (
                len(self.first_anomaly_samples) < self.anomaly_sample_limit // 2
                or caller_stat["anomaly_count"] == 1
                or (
                    guest_flip_count is not None
                    and guest_flip_count not in self.sampled_anomaly_flips
                )
                or anomaly_total % 1024 == 0
            )
            if should_sample:
                vertex_summary = self._vertex_summary(recent_vertex)
                register_snapshot = {
                    register: _hex32(
                        entry_eax
                        if register == "eax"
                        else esp
                        if register == "esp"
                        else cpu.get_register(register)
                    )
                    for register in (
                        "eax",
                        "ebx",
                        "ecx",
                        "edx",
                        "esi",
                        "edi",
                        "ebp",
                        "esp",
                    )
                }
                sample = {
                    **vertex_summary,
                    "anomalies": anomalies,
                    "registers": register_snapshot,
                    "frame_chain": self._frame_chain(
                        memory, esp, cpu.get_register("ebp")
                    ),
                    "stack_words": self._stack_words(memory, esp),
                    "stack_code_candidates": self._stack_code_candidates(memory, esp),
                    "frontend_context": self._frontend_context(memory),
                    "vertex_object_control_words": self._memory_words(
                        memory,
                        _u32(object_address + self.count_offset - 0x20),
                        13,
                    ),
                    "recent_vertices": [
                        self._vertex_summary(vertex)
                        for vertex in self.recent_vertices
                    ],
                }
                if guest_flip_count is not None:
                    self.sampled_anomaly_flips.add(guest_flip_count)
                if len(self.first_anomaly_samples) < self.anomaly_sample_limit // 2:
                    self.first_anomaly_samples.append(sample)
                self.latest_anomaly_samples.append(sample)

        if trace.enabled:
            vertex_summary = self._vertex_summary(recent_vertex)
            trace.add(
                target,
                "title_vertex_append_fast_path",
                **vertex_summary,
                default_z_bits_hex=_hex32(default_z_bits),
                color_vector_address=color_vector_address,
                color_vector_address_hex=_hex32(color_vector_address),
                packed_color=packed_color,
                packed_color_hex=_hex32(packed_color),
                tail0_bits_hex=_hex32(tail0_bits),
                tail1_bits_hex=_hex32(tail1_bits),
                anomalies=anomalies,
            )

    @staticmethod
    def _vertex_summary(vertex: tuple[Any, ...]) -> dict[str, Any]:
        (
            invocation_count,
            object_address,
            record_address,
            vertex_index,
            x,
            y,
            x_bits,
            y_bits,
            caller_return_address,
            upstream_return_address,
            render_write_count,
            guest_flip_count,
        ) = vertex
        return {
            "invocation_count": invocation_count,
            "object_address_hex": _hex32(object_address),
            "record_address_hex": _hex32(record_address),
            "vertex_index": vertex_index,
            "x": x,
            "y": y,
            "x_bits_hex": _hex32(x_bits),
            "y_bits_hex": _hex32(y_bits),
            "caller_return_address_hex": _hex32(caller_return_address),
            "upstream_return_address_hex": _hex32(upstream_return_address)
            if upstream_return_address
            else None,
            "render_write_count": render_write_count,
            "guest_flip_count": guest_flip_count,
        }

    def _record_producer(
        self,
        stats_by_address: dict[int, dict[str, Any]],
        address: int,
        *,
        object_address: int,
        x: float,
        y: float,
        anomalies: list[str],
        render_write_count: int | None,
        guest_flip_count: int | None,
    ) -> dict[str, Any]:
        stats = stats_by_address.setdefault(
            address,
            {
                "invocation_count": 0,
                "anomaly_count": 0,
                "anomaly_counts": Counter(),
                "object_counts": Counter(),
                "min_x": None,
                "max_x": None,
                "min_y": None,
                "max_y": None,
                "first_invocation": self.invocation_count,
                "last_invocation": self.invocation_count,
                "first_render_write_count": render_write_count,
                "last_render_write_count": render_write_count,
                "first_guest_flip_count": guest_flip_count,
                "last_guest_flip_count": guest_flip_count,
            },
        )
        stats["invocation_count"] += 1
        stats["last_invocation"] = self.invocation_count
        stats["last_render_write_count"] = render_write_count
        stats["last_guest_flip_count"] = guest_flip_count
        stats["object_counts"][object_address] += 1
        if math.isfinite(x) and math.isfinite(y):
            stats["min_x"] = x if stats["min_x"] is None else min(stats["min_x"], x)
            stats["max_x"] = x if stats["max_x"] is None else max(stats["max_x"], x)
            stats["min_y"] = y if stats["min_y"] is None else min(stats["min_y"], y)
            stats["max_y"] = y if stats["max_y"] is None else max(stats["max_y"], y)
        if anomalies:
            stats["anomaly_count"] += 1
            stats["anomaly_counts"].update(anomalies)
        return stats

    def _record_flip_geometry(
        self,
        guest_flip_count: int,
        *,
        upstream_return_address: int,
        x: float,
        y: float,
        anomalies: list[str],
    ) -> None:
        if guest_flip_count not in self.flip_geometry_stats:
            if len(self.flip_geometry_stats) >= 256:
                del self.flip_geometry_stats[next(iter(self.flip_geometry_stats))]
            self.flip_geometry_stats[guest_flip_count] = {
                "vertex_count": 0,
                "anomaly_count": 0,
                "anomaly_counts": Counter(),
                "min_x": None,
                "max_x": None,
                "min_y": None,
                "max_y": None,
                "upstream_producers": {},
            }
        stats = self.flip_geometry_stats[guest_flip_count]
        stats["vertex_count"] += 1
        stats["anomaly_count"] += bool(anomalies)
        stats["anomaly_counts"].update(anomalies)
        if math.isfinite(x) and math.isfinite(y):
            stats["min_x"] = x if stats["min_x"] is None else min(stats["min_x"], x)
            stats["max_x"] = x if stats["max_x"] is None else max(stats["max_x"], x)
            stats["min_y"] = y if stats["min_y"] is None else min(stats["min_y"], y)
            stats["max_y"] = y if stats["max_y"] is None else max(stats["max_y"], y)
        if not 0x00010000 <= upstream_return_address < 0x00300000:
            return
        producer = stats["upstream_producers"].setdefault(
            upstream_return_address,
            {
                "vertex_count": 0,
                "anomaly_count": 0,
                "min_x": None,
                "max_x": None,
                "min_y": None,
                "max_y": None,
            },
        )
        producer["vertex_count"] += 1
        producer["anomaly_count"] += bool(anomalies)
        if math.isfinite(x) and math.isfinite(y):
            producer["min_x"] = x if producer["min_x"] is None else min(producer["min_x"], x)
            producer["max_x"] = x if producer["max_x"] is None else max(producer["max_x"], x)
            producer["min_y"] = y if producer["min_y"] is None else min(producer["min_y"], y)
            producer["max_y"] = y if producer["max_y"] is None else max(producer["max_y"], y)

    @staticmethod
    def _frame_chain(
        memory: SparseMemory,
        esp: int,
        frame_pointer: int,
    ) -> list[dict[str, Any]]:
        frames: list[dict[str, Any]] = []
        current = frame_pointer
        for depth in range(8):
            if current < esp or current - esp > 0x00200000 or current & 3:
                break
            parent = memory.read_u32(current)
            return_address = memory.read_u32(_u32(current + 4))
            frames.append(
                {
                    "depth": depth,
                    "frame_pointer_hex": _hex32(current),
                    "return_address_hex": _hex32(return_address),
                }
            )
            if parent <= current:
                break
            current = parent
        return frames

    @staticmethod
    def _memory_words(
        memory: SparseMemory,
        base_address: int,
        word_count: int,
    ) -> list[dict[str, Any]]:
        return [
            {
                "offset_hex": _hex32(index * 4),
                "address_hex": _hex32(_u32(base_address + index * 4)),
                "value_hex": _hex32(
                    memory.read_u32(_u32(base_address + index * 4))
                ),
            }
            for index in range(word_count)
        ]

    @classmethod
    def _stack_words(
        cls,
        memory: SparseMemory,
        esp: int,
    ) -> list[dict[str, Any]]:
        return cls._memory_words(memory, esp, TITLE_VERTEX_APPEND_STACK_SCAN_WORDS)

    @classmethod
    def _frontend_context(cls, memory: SparseMemory) -> dict[str, Any]:
        frontend_index = memory.read_u32(0x00353104)
        frontend_object_address = (
            memory.read_u32(_u32(0x00353108 + frontend_index * 4))
            if frontend_index < 0x1000
            else 0
        )
        dynamic_object_address = memory.read_u32(
            _u32(
                TITLE_FRONTEND_DYNAMIC_OBJECT_OWNER_ADDRESS
                + TITLE_FRONTEND_DYNAMIC_OBJECT_POINTER_OFFSET
            )
        )
        return {
            "frontend_index": frontend_index,
            "frontend_object_address_hex": _hex32(frontend_object_address),
            "frontend_source_text_address_hex": _hex32(
                memory.read_u32(_u32(frontend_object_address + 0x0DE4))
                if frontend_object_address
                else 0
            ),
            "frontend_object_head_words": cls._memory_words(
                memory,
                frontend_object_address,
                16,
            )
            if frontend_object_address
            else [],
            "frontend_object_render_words": cls._memory_words(
                memory,
                _u32(frontend_object_address + 0x0DD0),
                16,
            )
            if frontend_object_address
            else [],
            "dynamic_object_address_hex": _hex32(dynamic_object_address),
            "dynamic_object_head_words": cls._memory_words(
                memory,
                dynamic_object_address,
                16,
            )
            if dynamic_object_address
            else [],
        }

    @staticmethod
    def _stack_code_candidates(
        memory: SparseMemory,
        esp: int,
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for index in range(TITLE_VERTEX_APPEND_STACK_SCAN_WORDS):
            offset = index * 4
            value = memory.read_u32(_u32(esp + offset))
            if 0x00010000 <= value < 0x00300000:
                candidates.append(
                    {
                        "stack_offset_hex": _hex32(offset),
                        "address_hex": _hex32(value),
                    }
                )
        return candidates

    def summary(self) -> dict[str, Any]:
        samples_by_invocation: dict[int, dict[str, Any]] = {}
        for sample in [*self.first_anomaly_samples, *self.latest_anomaly_samples]:
            samples_by_invocation[int(sample["invocation_count"])] = sample
        producer_callers = self._producer_summaries(
            self.caller_stats,
            address_field="caller_return_address_hex",
        )
        upstream_producers = self._producer_summaries(
            self.upstream_caller_stats,
            address_field="upstream_return_address_hex",
        )
        return {
            "append_address": self.append_address,
            "append_address_hex": _hex32(self.append_address),
            "count_offset": self.count_offset,
            "count_offset_hex": _hex32(self.count_offset),
            "default_z_offset": self.default_z_offset,
            "default_z_offset_hex": _hex32(self.default_z_offset),
            "stack_cleanup": TITLE_VERTEX_APPEND_STACK_CLEANUP,
            "stride": self.stride,
            "invocation_count": self.invocation_count,
            "max_vertex_index": self.max_vertex_index,
            "last_object_address_hex": _hex32(self.last_object_address or 0)
            if self.last_object_address is not None
            else None,
            "last_record_address_hex": _hex32(self.last_record_address or 0)
            if self.last_record_address is not None
            else None,
            "last_vertex_index": self.last_vertex_index,
            "geometry_bounds": {
                "min_x": self.finite_min_x,
                "max_x": self.finite_max_x,
                "min_y": self.finite_min_y,
                "max_y": self.finite_max_y,
            },
            "anomaly_counts": dict(sorted(self.anomaly_counts.items())),
            "producer_callers": producer_callers,
            "upstream_producers": upstream_producers,
            "geometry_by_guest_flip": [
                {
                    "guest_flip_count": flip,
                    "vertex_count": stats["vertex_count"],
                    "anomaly_count": stats["anomaly_count"],
                    "anomaly_counts": dict(
                        sorted(stats["anomaly_counts"].items())
                    ),
                    "min_x": stats["min_x"],
                    "max_x": stats["max_x"],
                    "min_y": stats["min_y"],
                    "max_y": stats["max_y"],
                    "upstream_producers": [
                        {
                            "upstream_return_address_hex": _hex32(address),
                            **producer,
                        }
                        for address, producer in sorted(
                            stats["upstream_producers"].items(),
                            key=lambda item: (
                                -int(item[1]["anomaly_count"]),
                                -int(item[1]["vertex_count"]),
                                item[0],
                            ),
                        )
                    ],
                }
                for flip, stats in self.flip_geometry_stats.items()
            ],
            "anomalous_samples": list(
                sample for _, sample in sorted(samples_by_invocation.items())
            ),
        }

    @staticmethod
    def _producer_summaries(
        stats_by_address: dict[int, dict[str, Any]],
        *,
        address_field: str,
    ) -> list[dict[str, Any]]:
        producers: list[dict[str, Any]] = []
        for address, stats in sorted(
            stats_by_address.items(),
            key=lambda item: (
                -int(item[1]["anomaly_count"]),
                -int(item[1]["invocation_count"]),
                item[0],
            ),
        ):
            producers.append(
                {
                    address_field: _hex32(address),
                    "invocation_count": stats["invocation_count"],
                    "anomaly_count": stats["anomaly_count"],
                    "anomaly_counts": dict(sorted(stats["anomaly_counts"].items())),
                    "min_x": stats["min_x"],
                    "max_x": stats["max_x"],
                    "min_y": stats["min_y"],
                    "max_y": stats["max_y"],
                    "first_invocation": stats["first_invocation"],
                    "last_invocation": stats["last_invocation"],
                    "first_render_write_count": stats["first_render_write_count"],
                    "last_render_write_count": stats["last_render_write_count"],
                    "first_guest_flip_count": stats["first_guest_flip_count"],
                    "last_guest_flip_count": stats["last_guest_flip_count"],
                    "object_counts": [
                        {
                            "object_address_hex": _hex32(object_address),
                            "invocation_count": count,
                        }
                        for object_address, count in stats["object_counts"].most_common()
                    ],
                }
            )
        return producers


class TitleTextDrawFastPath:
    """Fast-path the observed title text draw helper after its contract is known."""

    def __init__(
        self,
        *,
        draw_address: int = TITLE_TEXT_DRAW_ADDRESS,
        stack_cleanup: int = TITLE_TEXT_DRAW_STACK_CLEANUP,
        max_sample_bytes: int = TITLE_TEXT_DRAW_MAX_SAMPLE_BYTES,
        render_watchpoint: RenderWriteWatchpoint | None = None,
    ) -> None:
        self.draw_address = draw_address
        self.stack_cleanup = stack_cleanup
        self.max_sample_bytes = max_sample_bytes
        self.render_watchpoint = render_watchpoint
        self.invocation_count = 0
        self.total_character_count = 0
        self.max_character_count = 0
        self.last_object_address: int | None = None
        self.last_string_address: int | None = None
        self.last_character_count: int | None = None
        self.last_arguments: tuple[int, ...] = ()
        self.sampled_strings: list[dict[str, Any]] = []
        self.latest_sampled_strings: deque[dict[str, Any]] = deque(maxlen=16)
        self.caller_return_counts: Counter[int] = Counter()
        self.frontend_renderer_parent_return_counts: Counter[int] = Counter()
        self.source_text_counts: Counter[int] = Counter()

    def call_handlers(
        self,
    ) -> dict[int, Callable[[CpuState, SparseMemory, int, ExecutionTrace], None]]:
        return {self.draw_address: self.draw_handler}

    def draw_handler(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
    ) -> None:
        esp = cpu.get_register("esp")
        caller_return_address = memory.read_u32(esp)
        frontend_renderer_parent_return = (
            memory.read_u32(_u32(esp + 0xBC))
            if caller_return_address == 0x000B8B44
            else 0
        )
        arguments = tuple(memory.read_u32(_u32(esp + 4 + index * 4)) for index in range(5))
        string_address = arguments[0]
        object_address = cpu.get_register("ecx")
        frontend_index = memory.read_u32(0x00353104)
        frontend_object_address = memory.read_u32(
            _u32(0x00353108 + frontend_index * 4)
        )
        source_text_address = (
            memory.read_u32(_u32(frontend_object_address + 0x0DE4))
            if frontend_object_address
            else 0
        )
        parent_stack_strings: list[dict[str, Any]] = []
        if caller_return_address == 0x000B8B44 and not self.sampled_strings:
            for stack_offset in range(0xBC, 0x181, 4):
                candidate_address = memory.read_u32(_u32(esp + stack_offset))
                if not 0x00010000 <= candidate_address < 0x80000000:
                    continue
                candidate = _read_guest_c_string(
                    memory,
                    candidate_address,
                    max_bytes=160,
                )
                if len(candidate) < 3 or any(byte < 0x20 or byte >= 0x7F for byte in candidate):
                    continue
                parent_stack_strings.append(
                    {
                        "stack_offset_hex": _hex32(stack_offset),
                        "address_hex": _hex32(candidate_address),
                        "bytes_hex": candidate.hex().upper(),
                    }
                )
        payload = _read_guest_c_string(
            memory,
            string_address,
            max_bytes=self.max_sample_bytes,
        )
        character_count = len(payload)

        _prepare_stdcall_return(cpu, memory, self.stack_cleanup)
        self.invocation_count += 1
        self.total_character_count += character_count
        self.max_character_count = max(self.max_character_count, character_count)
        self.last_object_address = object_address
        self.last_string_address = string_address
        self.last_character_count = character_count
        self.last_arguments = arguments
        self.caller_return_counts[caller_return_address] += 1
        if frontend_renderer_parent_return:
            self.frontend_renderer_parent_return_counts[
                frontend_renderer_parent_return
            ] += 1
        self.source_text_counts[source_text_address] += 1
        sample = {
            "invocation_count": self.invocation_count,
            "string_address_hex": _hex32(string_address),
            "character_count": character_count,
            "bytes_hex": payload.hex().upper(),
            "caller_return_address_hex": _hex32(caller_return_address),
            "frontend_renderer_parent_return_hex": _hex32(
                frontend_renderer_parent_return
            ),
            "frontend_index": frontend_index,
            "frontend_object_address_hex": _hex32(frontend_object_address),
            "source_text_address_hex": _hex32(source_text_address),
            "arguments": [_hex32(argument) for argument in arguments],
            "parent_stack_strings": parent_stack_strings,
            "render_write_count": (
                self.render_watchpoint.write_count
                if self.render_watchpoint is not None
                else None
            ),
            "guest_flip_count": (
                self.render_watchpoint.flip_count
                if self.render_watchpoint is not None
                else None
            ),
        }
        if len(self.sampled_strings) < 16:
            self.sampled_strings.append(sample)
        self.latest_sampled_strings.append(sample)
        trace.add(
            target,
            "title_text_draw_fast_path",
            invocation_count=self.invocation_count,
            object_address=object_address,
            object_address_hex=_hex32(object_address),
            string_address=string_address,
            string_address_hex=_hex32(string_address),
            character_count=character_count,
            arguments=[_hex32(argument) for argument in arguments],
            stack_cleanup=self.stack_cleanup,
            bytes_hex=sample["bytes_hex"],
            caller_return_address=caller_return_address,
            caller_return_address_hex=_hex32(caller_return_address),
            frontend_renderer_parent_return_hex=_hex32(
                frontend_renderer_parent_return
            ),
            frontend_index=frontend_index,
            frontend_object_address_hex=_hex32(frontend_object_address),
            source_text_address_hex=_hex32(source_text_address),
            render_write_count=sample["render_write_count"],
            guest_flip_count=sample["guest_flip_count"],
        )

    def summary(self) -> dict[str, Any]:
        samples_by_invocation = {
            int(sample["invocation_count"]): sample
            for sample in [*self.sampled_strings, *self.latest_sampled_strings]
        }
        return {
            "draw_address": self.draw_address,
            "draw_address_hex": _hex32(self.draw_address),
            "stack_cleanup": self.stack_cleanup,
            "invocation_count": self.invocation_count,
            "total_character_count": self.total_character_count,
            "max_character_count": self.max_character_count,
            "last_object_address_hex": _hex32(self.last_object_address or 0)
            if self.last_object_address is not None
            else None,
            "last_string_address_hex": _hex32(self.last_string_address or 0)
            if self.last_string_address is not None
            else None,
            "last_character_count": self.last_character_count,
            "last_arguments": [_hex32(argument) for argument in self.last_arguments],
            "sampled_strings": [
                sample for _, sample in sorted(samples_by_invocation.items())
            ],
            "caller_return_counts": [
                {
                    "caller_return_address_hex": _hex32(address),
                    "invocation_count": count,
                }
                for address, count in self.caller_return_counts.most_common()
            ],
            "frontend_renderer_parent_return_counts": [
                {
                    "parent_return_address_hex": _hex32(address),
                    "invocation_count": count,
                }
                for address, count in (
                    self.frontend_renderer_parent_return_counts.most_common()
                )
            ],
            "source_text_counts": [
                {
                    "source_text_address_hex": _hex32(address),
                    "invocation_count": count,
                }
                for address, count in self.source_text_counts.most_common()
            ],
        }


class TitleDirectSoundBufferSyncFastPath:
    """Model the reached DirectSound effect-image transaction above private DSP internals."""

    def __init__(
        self,
        *,
        effect_image_address: int = TITLE_DIRECTSOUND_EFFECT_IMAGE_ADDRESS,
        sync_address: int = TITLE_DIRECTSOUND_BUFFER_SYNC_ADDRESS,
        workspace_address: int = TITLE_DIRECTSOUND_SYNTHETIC_WORKSPACE_ADDRESS,
        sample_limit: int = 16,
    ) -> None:
        self.effect_image_address = effect_image_address
        self.sync_address = sync_address
        self.workspace_address = workspace_address
        self.sample_limit = sample_limit
        self.invocation_count = 0
        self.null_object_count = 0
        self.samples: list[dict[str, Any]] = []

    def call_handlers(
        self,
    ) -> dict[int, Callable[[CpuState, SparseMemory, int, ExecutionTrace], None]]:
        return {self.effect_image_address: self.effect_image_handler}

    def effect_image_handler(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
    ) -> None:
        esp = cpu.get_register("esp")
        object_address = cpu.get_register("ecx")
        image_address = memory.read_u32(_u32(esp + 4))
        image_size = memory.read_u32(_u32(esp + 8))
        workspace_output_address = memory.read_u32(_u32(esp + 12))
        if workspace_output_address:
            memory.write_u32(workspace_output_address, self.workspace_address)
        memory.write_u32(self.workspace_address, image_size)
        self.invocation_count += 1
        if object_address == 0:
            self.null_object_count += 1
        cpu.set_register("eax", 0)
        _prepare_stdcall_return(cpu, memory, 12)
        sample = {
            "invocation_count": self.invocation_count,
            "object_address_hex": _hex32(object_address),
            "image_address_hex": _hex32(image_address),
            "image_size": image_size,
            "workspace_output_address_hex": _hex32(workspace_output_address),
            "workspace_address_hex": _hex32(self.workspace_address),
            "result_hex": "0x00000000",
        }
        if len(self.samples) < self.sample_limit:
            self.samples.append(sample)
        trace.add(target, "title_directsound_effect_image_fast_path", **sample)

    def sync_handler(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
    ) -> None:
        object_address = cpu.get_register("ecx")
        sound_object = memory.read_u32(_u32(object_address + 8)) if object_address else 0
        workspace = memory.read_u32(_u32(object_address + 0x20)) if object_address else 0
        self.invocation_count += 1
        if object_address == 0:
            self.null_object_count += 1
        cpu.set_register("eax", 0)
        sample = {
            "invocation_count": self.invocation_count,
            "object_address_hex": _hex32(object_address),
            "sound_object_hex": _hex32(sound_object),
            "workspace_before_hex": _hex32(workspace),
            "result_hex": "0x00000000",
        }
        if len(self.samples) < self.sample_limit:
            self.samples.append(sample)
        trace.add(target, "title_directsound_buffer_sync_fast_path", **sample)

    def summary(self) -> dict[str, Any]:
        return {
            "effect_image_address_hex": _hex32(self.effect_image_address),
            "sync_address_hex": _hex32(self.sync_address),
            "workspace_address_hex": _hex32(self.workspace_address),
            "invocation_count": self.invocation_count,
            "null_object_count": self.null_object_count,
            "samples": self.samples,
        }


def _pack_title_vertex_color(memory: SparseMemory, color_vector_address: int) -> int:
    components = [
        _truncate_title_color_component(
            _read_guest_float32(memory, _u32(color_vector_address + offset))
        )
        for offset in (0x0C, 0x00, 0x04, 0x08)
    ]
    packed = 0
    for component in components:
        packed = _u32((packed << 8) | component)
    return packed


def _read_guest_float32(memory: SparseMemory, address: int) -> float:
    return struct.unpack("<f", memory.read(address, 4))[0]


def _truncate_title_color_component(value: float) -> int:
    if math.isnan(value):
        return 0
    return int(value) & 0xFF


class RenderWriteWatchpoint:
    """Capture D3D MMIO/push-buffer writes as memory-observer events."""

    def __init__(
        self,
        *,
        max_writes: int = DEFAULT_RENDER_STREAM_MAX_WRITES,
        stop_after: int | None = None,
        capture_after: int = 0,
    ) -> None:
        self.max_writes = max_writes
        self.stop_after = stop_after
        self.capture_after = max(0, capture_after)
        self.mmio_write_count = 0
        self.push_buffer_write_count = 0
        self.writes: list[dict[str, Any]] = []
        self.history_writes: deque[dict[str, Any]] = deque(maxlen=max_writes)
        self.live_command_records: deque[bytes] = deque(maxlen=max_writes)
        self._texture_offsets: dict[int, int] = {}
        self._texture_formats: dict[int, int] = {}
        self._texture_bindings: list[tuple[int, int, int]] = []
        self._texture_binding_seen: set[tuple[int, int, int]] = set()
        self._texture_pending = bytearray()
        self._texture_pending_end: int | None = None
        self.flip_count = 0
        self._pending_flip_boundaries: deque[dict[str, int]] = deque()

    @property
    def write_count(self) -> int:
        return self.mmio_write_count + self.push_buffer_write_count

    def observe(self, address: int, payload: bytes) -> None:
        kind: str | None = None
        offset: int | None = None
        if 0xFED00000 <= address <= 0xFED0FFFF:
            kind = "d3d_mmio"
            offset = address - 0xFED00000
            self.mmio_write_count += 1
        elif 0x80000000 <= address <= 0x8000FFFF:
            kind = "d3d_push_buffer"
            offset = address - 0x80000000
            self.push_buffer_write_count += 1
        if kind is None:
            return
        if kind == "d3d_push_buffer":
            self._observe_texture_write(address, payload)
        else:
            # The MMIO submission that closes the pending push-buffer run is
            # part of the audited flip and must be present in its replay.
            self._flush_texture_writes(boundary_write_count=self.write_count)
        value = int.from_bytes(payload[: min(len(payload), 4)], "little", signed=False)
        record = {
            "sequence": self.write_count - 1,
            "instruction_address_hex": None,
            "kind": kind,
            "address": _u32(address),
            "address_hex": _hex32(address),
            "offset": offset,
            "offset_hex": _hex32(offset) if offset is not None else None,
            "value": value,
            "value_hex": _hex32(value),
            "size": len(payload),
            "bytes_hex": payload.hex().upper(),
        }
        self.live_command_records.append(
            struct.pack(
                "<BBHI8s",
                0 if kind == "d3d_mmio" else 1,
                len(payload),
                0,
                address,
                payload[:8].ljust(8, b"\x00"),
            )
        )
        self.history_writes.append(record)
        if self.write_count > self.capture_after and len(self.writes) < self.max_writes:
            self.writes.append(record)
        if self.stop_after is not None and self.write_count >= self.stop_after:
            raise RenderWatchpointStop(self.to_stream())

    def to_stream(self) -> dict[str, Any]:
        return {
            "write_count": self.write_count,
            "mmio_write_count": self.mmio_write_count,
            "push_buffer_write_count": self.push_buffer_write_count,
            "captured_write_count": len(self.writes),
            "capture_after_write_count": self.capture_after,
            "skipped_write_count": min(self.write_count, self.capture_after),
            "truncated": self.write_count > len(self.writes),
            "writes": list(self.writes),
        }

    def history_stream(self) -> dict[str, Any]:
        self._flush_texture_writes(clear=False)
        return {
            "write_count": self.write_count,
            "mmio_write_count": self.mmio_write_count,
            "push_buffer_write_count": self.push_buffer_write_count,
            "captured_write_count": len(self.history_writes),
            "truncated": self.write_count > len(self.history_writes),
            "writes": list(self.history_writes),
            "texture_bindings": [list(binding) for binding in self._texture_bindings],
        }

    def resource_binding_stream(self) -> dict[str, Any]:
        """Return retained texture bindings without cloning diagnostic writes."""
        self._flush_texture_writes(clear=False)
        return {
            "texture_bindings": [list(binding) for binding in self._texture_bindings]
        }

    def live_epoch_stream(self) -> dict[str, Any]:
        return {
            "write_count": self.write_count,
            "mmio_write_count": self.mmio_write_count,
            "push_buffer_write_count": self.push_buffer_write_count,
            "captured_write_count": len(self.live_command_records),
            "truncated": self.write_count > len(self.live_command_records),
            "writes": [],
        }

    def has_pending_flip(self) -> bool:
        return bool(self._pending_flip_boundaries)

    def consume_pending_flip_boundaries(self) -> list[dict[str, int]]:
        boundaries = list(self._pending_flip_boundaries)
        self._pending_flip_boundaries.clear()
        return boundaries

    def _observe_texture_write(self, address: int, payload: bytes) -> None:
        if self._texture_pending and address != self._texture_pending_end:
            self._flush_texture_writes(boundary_write_count=self.write_count - 1)
        self._texture_pending.extend(payload)
        self._texture_pending_end = address + len(payload)

    def _observe_texture_method(
        self,
        method: int,
        data: int,
        *,
        record_flips: bool,
        boundary_write_count: int,
    ) -> None:
        if method == 0x012C:
            if record_flips:
                self.flip_count += 1
                self._pending_flip_boundaries.append(
                    {
                        "flip_index": self.flip_count,
                        "write_count": max(0, boundary_write_count),
                        "flip_value": data,
                    }
                )
            return
        if method == 0x17FC and data != 0:
            for stage in sorted(self._texture_offsets.keys() & self._texture_formats.keys()):
                binding = (
                    stage,
                    self._texture_offsets[stage],
                    self._texture_formats[stage],
                )
                if binding not in self._texture_binding_seen:
                    self._texture_binding_seen.add(binding)
                    self._texture_bindings.append(binding)
            return
        if not 0x1B00 <= method < 0x1C00:
            return
        stage = (method - 0x1B00) // 0x40
        register = (method - 0x1B00) % 0x40
        if register == 0:
            self._texture_offsets[stage] = data
        elif register == 4:
            self._texture_formats[stage] = data

    def _flush_texture_writes(
        self,
        *,
        clear: bool = True,
        boundary_write_count: int | None = None,
    ) -> None:
        if boundary_write_count is None:
            boundary_write_count = self.write_count
        words = [
            int.from_bytes(self._texture_pending[offset : offset + 4], "little")
            for offset in range(
                0,
                len(self._texture_pending) - (len(self._texture_pending) % 4),
                4,
            )
        ]
        index = 0
        while index < len(words):
            command = words[index]
            non_increasing = False
            header_words = 1
            if (command & 0xE0030003) == 0:
                count = (command >> 18) & 0x7FF
            elif (command & 0xE0030003) == 0x40000000:
                count = (command >> 18) & 0x7FF
                non_increasing = True
            elif (
                (command & 0xFFFF0003) == 0x00030000
                and index + 1 < len(words)
            ):
                count = words[index + 1] & 0x00FFFFFF
                non_increasing = True
                header_words = 2
            else:
                index += 1
                continue
            first_method = ((command >> 2) & 0x7FF) * 4
            data_start = index + header_words
            data_end = min(data_start + count, len(words))
            for data_index in range(data_start, data_end):
                method_offset = 0 if non_increasing else data_index - data_start
                self._observe_texture_method(
                    first_method + method_offset * 4,
                    words[data_index],
                    record_flips=clear,
                    boundary_write_count=boundary_write_count,
                )
            index = data_end
        if clear:
            self._texture_pending = bytearray()
            self._texture_pending_end = None


class LiveHostBridge:
    """Exchange atomic render snapshots and controller state with the presenter."""

    def __init__(
        self,
        runtime: XboxRuntimeShims,
        render_watchpoint: RenderWriteWatchpoint,
        *,
        render_stream_path: Path,
        controller_state_path: Path,
        render_publish_interval_seconds: float = 1.0 / 45.0,
        render_publish_min_writes: int = 1,
        render_prefix_writes: int = 256,
        render_tail_writes: int = 256,
        render_resource_scan_interval_seconds: float = 0.25,
        flip_audit_ack_path: Path | None = None,
        flip_audit_timeout_seconds: float = 120.0,
        flip_audit_health_interval: int = 30,
        flip_audit_max_flips: int = 0,
        presentation_ack_path: Path | None = None,
        data_export_synchronizer: Callable[[SparseMemory], None] | None = None,
    ) -> None:
        self.runtime = runtime
        self.render_watchpoint = render_watchpoint
        self.render_stream_path = render_stream_path
        self.render_resource_path = render_stream_path.with_name(
            render_stream_path.name + ".resources.bin"
        )
        self.current_render_resource_path = self.render_resource_path
        self.command_stream_generation = time.time_ns()
        self.render_command_path = render_stream_path.with_name(
            render_stream_path.name
            + f".{self.command_stream_generation}.commands.bin"
        )
        self._render_command_file: BinaryIO | None = None
        self.controller_state_path = controller_state_path
        self.controller_consumed_path = controller_state_path.with_name(
            controller_state_path.name + ".consumed.json"
        )
        self.flip_audit_ack_path = flip_audit_ack_path
        self.presentation_ack_path = presentation_ack_path
        self._presentation_ack_file: BinaryIO | None = None
        self.presentation_event_name = (
            f"b2_recomp_presented_{self.command_stream_generation:016X}"
            if presentation_ack_path is not None
            else None
        )
        self.publication_event_name = (
            f"b2_recomp_published_{self.command_stream_generation:016X}"
            if presentation_ack_path is not None
            else None
        )
        self._presentation_event_handle: int | None = None
        self._publication_event_handle: int | None = None
        self._presentation_event_kernel: Any | None = None
        if os.name == "nt" and self.presentation_event_name is not None:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateEventW.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_wchar_p,
            ]
            kernel.CreateEventW.restype = ctypes.c_void_p
            handle = kernel.CreateEventW(
                None,
                False,
                False,
                self.presentation_event_name,
            )
            if handle:
                self._presentation_event_handle = int(handle)
                self._presentation_event_kernel = kernel
            publication_handle = kernel.CreateEventW(
                None,
                False,
                False,
                self.publication_event_name,
            )
            if publication_handle:
                self._publication_event_handle = int(publication_handle)
                self._presentation_event_kernel = kernel
        self.flip_audit_ledger_path = (
            flip_audit_ack_path.with_name("flips.jsonl")
            if flip_audit_ack_path is not None
            else None
        )
        self.flip_audit_timeout_seconds = max(1.0, float(flip_audit_timeout_seconds))
        self.flip_audit_health_interval = max(0, int(flip_audit_health_interval))
        self.flip_audit_max_flips = max(0, int(flip_audit_max_flips))
        self.data_export_synchronizer = data_export_synchronizer
        if self.flip_audit_ack_path is not None:
            self.render_command_path = (
                self.flip_audit_ack_path.parent
                / f"commands.{self.command_stream_generation}.bin"
            )
        self.render_publish_interval_seconds = max(
            0.0, float(render_publish_interval_seconds)
        )
        self.render_publish_min_writes = max(1, int(render_publish_min_writes))
        self.render_prefix_writes = max(0, int(render_prefix_writes))
        self.render_tail_writes = max(1, int(render_tail_writes))
        self.render_resource_scan_interval_seconds = max(
            0.0, float(render_resource_scan_interval_seconds)
        )
        self.published_write_count = -1
        self.published_command_record_count = 0
        self.resource_stream_generation = 0
        self.controller_mtime_ns = -1
        self.render_publish_count = 0
        self.slice_exchange_count = 0
        self.render_resource_publish_count = 0
        self.render_resource_scan_count = 0
        self.last_render_resource_scan_time = 0.0
        self.last_render_resource_signatures: tuple[tuple[int, str], ...] | None = None
        self.last_render_publish_time = 0.0
        self.last_command_flush_time = 0.0
        self.last_published_manifest_flip_count = 0
        self.published_manifest_write_count = 0
        self.controller_update_count = 0
        self.controller_read_defer_count = 0
        self.clock_advanced_flip_count = 0
        self._clocked_flip_count = self.render_watchpoint.flip_count
        self.stop_requested = False
        self.audited_flip_count = 0
        self.audit_selected_flip_count = 0
        self.audit_skipped_flip_count = 0
        self.audit_ack_wait_count = 0
        self.presentation_ack_wait_count = 0
        self._last_audit_write_count = 0
        self._last_audit_write_delta = 0
        self._last_audit_flip_value: int | None = None
        self._flip_audit_ledger_buffer: list[str] = []
        self._last_published_manifest: dict[str, Any] | None = None
        self._resource_snapshot_cache: dict[
            tuple[int, int, int], tuple[tuple[int, ...], dict[str, Any]]
        ] = {}
        self._performance: dict[str, dict[str, int]] = {}
        self._publish_cadence_samples: deque[tuple[int, int]] = deque(maxlen=120)
        self._video_frame_interval_ns = 1_000_000_000 // 60
        self._next_video_frame_deadline_ns = 0
        self.video_pacing_sleep_count = 0
        self.video_pacing_sleep_us = 0
        self.video_pacing_max_sleep_us = 0
        self.sample_controller()
        initial_state = self.runtime.input.poll_controller(0)
        if not initial_state.connected:
            # The Win32 presenter exposes one keyboard-backed controller as
            # connected even before its first state file is published.
            self.runtime.input.set_controller_state(
                0,
                ControllerState(
                    connected=True,
                    buttons=initial_state.buttons,
                    left_trigger=initial_state.left_trigger,
                    right_trigger=initial_state.right_trigger,
                    thumb_lx=initial_state.thumb_lx,
                    thumb_ly=initial_state.thumb_ly,
                    thumb_rx=initial_state.thumb_rx,
                    thumb_ry=initial_state.thumb_ry,
                ),
            )

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, separators=(",", ":")) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        deadline = time.monotonic() + 10.0
        while True:
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                # The Win32 presenter briefly opens the previous snapshot
                # without delete sharing while parsing a hot reload. Wait for
                # that reader to close instead of terminating the guest loop.
                time.sleep(0.02)

    @staticmethod
    def _write_json_shared(path: Path, payload: dict[str, Any]) -> None:
        """Publish one small validated manifest without a rename/open conflict."""
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        with path.open("wb", buffering=0) as output:
            if output.write(encoded) != len(encoded):
                raise OSError(f"short live-manifest write to {path}")

    @staticmethod
    def _write_texture_resources_binary(
        path: Path,
        resources: list[dict[str, Any]],
    ) -> None:
        payload = bytearray(b"B2TEX001")
        payload.extend(struct.pack("<I", len(resources)))
        for resource in resources:
            format_bytes = str(resource["format"]).encode("ascii")
            texture_bytes = bytes.fromhex(str(resource["bytes_hex"]))
            content_hash = bytes.fromhex(str(resource["sha256"]))
            if len(format_bytes) == 0 or len(format_bytes) > 32:
                raise ValueError("live texture format name is invalid")
            if len(content_hash) != 32:
                raise ValueError("live texture SHA-256 is invalid")
            payload.extend(
                struct.pack(
                    "<IIIIII32s",
                    int(resource["stage"]),
                    int(resource["address"]),
                    int(resource["width"]),
                    int(resource["height"]),
                    len(format_bytes),
                    len(texture_bytes),
                    content_hash,
                )
            )
            payload.extend(format_bytes)
            payload.extend(texture_bytes)
        # Normal live resource generations use unique immutable paths. Writing
        # the new file once avoids an expensive antivirus-observed rename while
        # the old manifest continues to reference a complete prior generation
        # until the new payload is fully closed.
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = bytes(payload)
        with path.open("xb", buffering=0) as output:
            if output.write(encoded) != len(encoded):
                raise OSError(f"short live texture snapshot write to {path}")

    @staticmethod
    def _reset_live_commands_atomic(
        path: Path,
        records: deque[bytes],
    ) -> None:
        payload = bytearray(b"B2APPND1")
        payload.extend(b"".join(records))
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_bytes(payload)
        deadline = time.monotonic() + 10.0
        while True:
            try:
                temporary.replace(path)
                return
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)

    def _append_live_commands(self, path: Path, records: list[bytes]) -> None:
        if not records:
            return
        if self._render_command_file is None or self._render_command_file.closed:
            self._render_command_file = path.open("ab", buffering=0)
        self._render_command_file.write(b"".join(records))

    def _close_render_command_file(self) -> None:
        if self._render_command_file is None:
            return
        self._render_command_file.close()
        self._render_command_file = None

    def _record_performance(self, name: str, started_ns: int) -> None:
        elapsed_us = max(0, (time.perf_counter_ns() - started_ns) // 1_000)
        metric = self._performance.setdefault(
            name,
            {"count": 0, "total_us": 0, "max_us": 0},
        )
        metric["count"] += 1
        metric["total_us"] += elapsed_us
        metric["max_us"] = max(metric["max_us"], elapsed_us)

    def _performance_summary(self) -> dict[str, Any]:
        metrics = {
            name: {
                **metric,
                "average_us": round(
                    metric["total_us"] / max(1, metric["count"]),
                    3,
                ),
            }
            for name, metric in sorted(self._performance.items())
        }
        return {
            "metrics": metrics,
            "hot_paths": [
                {
                    "name": name,
                    **metrics[name],
                }
                for name in sorted(
                    metrics,
                    key=lambda item: (-metrics[item]["total_us"], item),
                )
            ],
        }

    def _flush_render_commands(self, *, force: bool = False) -> bool:
        write_count = self.render_watchpoint.write_count
        if write_count == 0 or write_count == self.published_write_count:
            return False
        now = time.monotonic()
        if (
            not force
            and self.published_write_count >= 0
            and write_count - self.published_write_count
            < self.render_publish_min_writes
        ):
            return False
        if (
            not force
            and self.published_write_count >= 0
            and now - self.last_command_flush_time
            < self.render_publish_interval_seconds
        ):
            return False
        command_publish_started_ns = time.perf_counter_ns()
        live_records = self.render_watchpoint.live_command_records
        if self.published_write_count < 0 or not self.render_command_path.is_file():
            self._close_render_command_file()
            self._reset_live_commands_atomic(self.render_command_path, live_records)
            self.published_command_record_count = len(live_records)
        else:
            new_record_count = write_count - self.published_write_count
            if new_record_count > len(live_records):
                if self.flip_audit_ack_path is not None:
                    raise RuntimeError(
                        "lossless flip audit command history overflowed before publication"
                    )
                raise RuntimeError(
                    "live render command history overflowed before sidecar flush"
                )
            new_records = list(
                itertools.islice(
                    live_records,
                    len(live_records) - new_record_count,
                    None,
                )
            )
            self._append_live_commands(self.render_command_path, new_records)
            self.published_command_record_count += len(new_records)
        self.published_write_count = write_count
        self.last_command_flush_time = now
        self._record_performance("render_command_publish", command_publish_started_ns)
        return True

    def publish_render(
        self,
        memory: SparseMemory,
        *,
        force: bool = False,
        audit_flip: dict[str, Any] | None = None,
        completed_flip: dict[str, Any] | None = None,
        guest_steps: int | None = None,
    ) -> bool:
        write_count = self.render_watchpoint.write_count
        if write_count == 0:
            return False
        if (
            not force
            and audit_flip is None
            and completed_flip is None
            and self.render_publish_count > 0
            and write_count - self.published_manifest_write_count
            < self.render_publish_min_writes
        ):
            return False
        now = time.monotonic()
        if (
            not force
            and self.render_publish_count > 0
            and now - self.last_render_publish_time
            < self.render_publish_interval_seconds
        ):
            return False
        publish_started_ns = time.perf_counter_ns()
        self._flush_render_commands(force=True)
        self._close_render_command_file()
        # Live presentation needs the newest completed flip, not the bounded
        # probe's oldest retained prefix.
        stream_snapshot_started_ns = time.perf_counter_ns()
        history = self.render_watchpoint.resource_binding_stream()
        stream = self.render_watchpoint.live_epoch_stream()
        self._record_performance("render_stream_snapshot", stream_snapshot_started_ns)
        if (
            force
            or self.last_render_resource_signatures is None
            or now - self.last_render_resource_scan_time
            >= self.render_resource_scan_interval_seconds
        ):
            resource_scan_started_ns = time.perf_counter_ns()
            stream = _snapshot_render_texture_resources(
                stream,
                history,
                memory,
                cache=self._resource_snapshot_cache,
            )
            resource_signatures = tuple(
                (int(resource["address"]), str(resource["sha256"]))
                for resource in stream["resource_snapshots"]
            )
            if resource_signatures != self.last_render_resource_signatures:
                self.resource_stream_generation = time.time_ns()
                self.current_render_resource_path = (
                    self.flip_audit_ack_path.parent
                    / "resources"
                    / f"resources.{self.resource_stream_generation}.json"
                    if self.flip_audit_ack_path is not None
                    else self.render_stream_path.with_name(
                        self.render_stream_path.name
                        + f".resources.{self.resource_stream_generation}.bin"
                    )
                )
                if self.flip_audit_ack_path is not None:
                    self._write_json_atomic(
                        self.current_render_resource_path,
                        {
                            "resource_snapshot_count": len(stream["resource_snapshots"]),
                            "resource_snapshots": stream["resource_snapshots"],
                        },
                    )
                else:
                    self._write_texture_resources_binary(
                        self.current_render_resource_path,
                        stream["resource_snapshots"],
                    )
                self.last_render_resource_signatures = resource_signatures
                self.render_resource_publish_count += 1
            self.render_resource_scan_count += 1
            self.last_render_resource_scan_time = now
            self._record_performance("render_resource_scan", resource_scan_started_ns)
        else:
            stream["resource_snapshots"] = []
            stream["resource_snapshot_count"] = 0
        selected_flip = audit_flip if audit_flip is not None else completed_flip
        presentable_command_record_count = (
            int(selected_flip["write_count"])
            if selected_flip is not None
            else 0
        )
        manifest_guest_flip_count = (
            int(selected_flip["flip_index"])
            if selected_flip is not None
            else self.last_published_manifest_flip_count
        )
        if presentable_command_record_count > self.published_command_record_count:
            raise RuntimeError(
                "completed flip boundary exceeds flushed live command sidecar"
            )
        manifest: dict[str, Any] = {
                "format": "b2-recomp-live-render-manifest",
                "write_count": stream["write_count"],
                "captured_write_count": stream["captured_write_count"],
                "published_command_record_count": self.published_command_record_count,
                "presentable_command_record_count": presentable_command_record_count,
                "command_stream_generation": self.command_stream_generation,
                "resource_stream_generation": self.resource_stream_generation,
                "guest_flip_count": manifest_guest_flip_count,
                "guest_steps": int(guest_steps or 0),
                "command_snapshot_path": str(self.render_command_path).replace("\\", "/"),
            "resource_snapshot_path": str(self.current_render_resource_path).replace("\\", "/"),
        }
        if self.presentation_ack_path is not None:
            manifest["presentation_ack_path"] = str(
                self.presentation_ack_path
            ).replace("\\", "/")
            manifest["presentation_event_name"] = self.presentation_event_name
            manifest["publication_event_name"] = self.publication_event_name
        if audit_flip is not None:
            manifest.update(
                {
                    "lossless_flip_audit": True,
                    "audit_flip_index": int(audit_flip["flip_index"]),
                    "audit_flip_value": int(audit_flip["flip_value"]),
                    "audit_command_record_count": int(audit_flip["write_count"]),
                    "audit_guest_steps": int(guest_steps or 0),
                    "audit_health_selected": True,
                    "audit_health_reason": str(
                        audit_flip.get("health_reason", "selected")
                    ),
                    "audit_ack_path": str(self.flip_audit_ack_path).replace("\\", "/"),
                    "audit_ledger_path": str(self.flip_audit_ledger_path).replace("\\", "/"),
                }
            )
        manifest_publish_started_ns = time.perf_counter_ns()
        if self.flip_audit_ack_path is not None or self.render_publish_count == 0:
            self._write_json_atomic(self.render_stream_path, manifest)
        else:
            self._write_json_shared(self.render_stream_path, manifest)
        self._record_performance("render_manifest_publish", manifest_publish_started_ns)
        self._last_published_manifest = manifest
        if (
            self._publication_event_handle is not None
            and self._presentation_event_kernel is not None
        ):
            self._presentation_event_kernel.SetEvent(
                ctypes.c_void_p(self._publication_event_handle)
            )
        self.last_published_manifest_flip_count = manifest_guest_flip_count
        self.published_manifest_write_count = write_count
        self.render_publish_count += 1
        self.last_render_publish_time = now
        self._publish_cadence_samples.append(
            (manifest_guest_flip_count, time.perf_counter_ns())
        )
        self._record_performance("publish_render_total", publish_started_ns)
        return True

    def should_yield_for_completed_flip(self) -> bool:
        # Normal presentation also yields on the exact completed-flip write.
        # That prevents a large native slice from crossing multiple frame
        # boundaries before the bridge can publish them individually.
        return self.render_watchpoint.has_pending_flip()

    def should_yield_for_flip_audit(self) -> bool:
        return (
            self.flip_audit_ack_path is not None
            and self.should_yield_for_completed_flip()
        )

    def _pace_completed_flip(self) -> None:
        if self.flip_audit_ack_path is not None:
            return
        started_ns = time.perf_counter_ns()
        now_ns = started_ns
        if self._next_video_frame_deadline_ns == 0:
            self._next_video_frame_deadline_ns = now_ns + self._video_frame_interval_ns
            self._record_performance("video_frame_pacing", started_ns)
            return
        deadline_ns = self._next_video_frame_deadline_ns
        if now_ns < deadline_ns:
            requested_ns = deadline_ns - now_ns
            time.sleep(requested_ns / 1_000_000_000.0)
            after_sleep_ns = time.perf_counter_ns()
            actual_sleep_us = max(0, (after_sleep_ns - now_ns) // 1_000)
            self.video_pacing_sleep_count += 1
            self.video_pacing_sleep_us += actual_sleep_us
            self.video_pacing_max_sleep_us = max(
                self.video_pacing_max_sleep_us,
                actual_sleep_us,
            )
            now_ns = after_sleep_ns
        next_deadline_ns = deadline_ns + self._video_frame_interval_ns
        if next_deadline_ns <= now_ns:
            # When guest/resource work is already late, resume immediately and
            # schedule from the actual publication time; never issue a burst
            # of catch-up flips.
            next_deadline_ns = now_ns + self._video_frame_interval_ns
        self._next_video_frame_deadline_ns = next_deadline_ns
        self._record_performance("video_frame_pacing", started_ns)

    def _wait_for_flip_ack(self, flip_index: int) -> bool:
        if self.flip_audit_ack_path is None:
            return True
        deadline = time.monotonic() + self.flip_audit_timeout_seconds
        self.audit_ack_wait_count += 1
        while True:
            self.sample_controller()
            if self.stop_requested:
                return False
            try:
                payload = self.flip_audit_ack_path.read_bytes()
            except (
                FileNotFoundError,
                PermissionError,
            ):
                payload = b""
            if len(payload) >= 20 and payload[:8] == b"B2ACK001":
                generation, acknowledged_flip = struct.unpack_from("<QI", payload, 8)
                if (
                    generation == self.command_stream_generation
                    and acknowledged_flip >= flip_index
                ):
                    return True
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"timed out waiting for Vulkan audit acknowledgement of flip {flip_index}"
                )
            time.sleep(0.002)

    def _wait_for_presentation_ack(self, flip_index: int) -> bool:
        if self.presentation_ack_path is None:
            return True
        deadline = time.monotonic() + self.flip_audit_timeout_seconds
        self.presentation_ack_wait_count += 1
        while True:
            self.sample_controller()
            if self.stop_requested:
                return False
            try:
                if (
                    self._presentation_ack_file is None
                    or self._presentation_ack_file.closed
                ):
                    self._presentation_ack_file = self.presentation_ack_path.open(
                        "rb", buffering=0
                    )
                self._presentation_ack_file.seek(0)
                payload = self._presentation_ack_file.read(24)
            except (FileNotFoundError, PermissionError, OSError):
                if self._presentation_ack_file is not None:
                    self._presentation_ack_file.close()
                    self._presentation_ack_file = None
                payload = b""
            if len(payload) >= 24 and payload[:8] == b"B2PRS001":
                generation, acknowledged_flip = struct.unpack_from("<QQ", payload, 8)
                if (
                    generation == self.command_stream_generation
                    and acknowledged_flip >= flip_index
                ):
                    return True
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "timed out waiting for Vulkan presentation acknowledgement "
                    f"of flip {flip_index}"
                )
            if (
                self._presentation_event_handle is not None
                and self._presentation_event_kernel is not None
            ):
                self._presentation_event_kernel.WaitForSingleObject(
                    ctypes.c_void_p(self._presentation_event_handle),
                    10,
                )
            else:
                time.sleep(0.0005)

    def _flush_flip_audit_ledger(self) -> None:
        if not self._flip_audit_ledger_buffer or self.flip_audit_ledger_path is None:
            return
        self.flip_audit_ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self.flip_audit_ledger_path.open("a", encoding="utf-8", newline="\n") as ledger:
            ledger.writelines(self._flip_audit_ledger_buffer)
        self._flip_audit_ledger_buffer.clear()

    def _queue_flip_audit_ledger_record(self, record: dict[str, Any]) -> None:
        self._flip_audit_ledger_buffer.append(
            json.dumps(record, separators=(",", ":")) + "\n"
        )
        if len(self._flip_audit_ledger_buffer) >= 120:
            self._flush_flip_audit_ledger()

    def _cached_audit_resource_changed(self, memory: SparseMemory) -> bool:
        for (_stage, address, _format_raw), (generations, resource) in (
            self._resource_snapshot_cache.items()
        ):
            byte_count = int(resource["byte_count"])
            first_page = address & ~memory._PAGE_MASK
            last_page = (address + byte_count - 1) & ~memory._PAGE_MASK
            current = tuple(
                memory.page_generation(page_address)
                for page_address in range(
                    first_page,
                    last_page + 1,
                    memory._PAGE_SIZE,
                )
            )
            if current != generations:
                return True
        return False

    def _flip_audit_selection_reason(
        self,
        boundary: dict[str, int],
        memory: SparseMemory,
    ) -> str | None:
        flip_index = int(boundary["flip_index"])
        write_count = int(boundary["write_count"])
        flip_value = int(boundary["flip_value"])
        write_delta = write_count - min(write_count, self._last_audit_write_count)
        reason: str | None = None
        if self.audited_flip_count == 0:
            reason = "first_flip"
        elif write_delta != self._last_audit_write_delta:
            reason = "command_shape_changed"
        elif self._last_audit_flip_value is not None and flip_value != self._last_audit_flip_value:
            reason = "flip_value_changed"
        elif self._cached_audit_resource_changed(memory):
            reason = "resource_changed"
        elif (
            self.flip_audit_health_interval > 0
            and flip_index % self.flip_audit_health_interval == 0
        ):
            reason = "periodic_sample"
        if self.flip_audit_max_flips > 0 and flip_index >= self.flip_audit_max_flips:
            reason = reason or "final_flip"
        self._last_audit_write_count = write_count
        self._last_audit_write_delta = write_delta
        self._last_audit_flip_value = flip_value
        return reason

    def sample_controller(self) -> bool:
        try:
            stat = self.controller_state_path.stat()
        except FileNotFoundError:
            return False
        if stat.st_mtime_ns == self.controller_mtime_ns:
            return False
        try:
            payload = json.loads(
                self.controller_state_path.read_text(encoding="utf-8")
            )
        except (
            FileNotFoundError,
            PermissionError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ):
            # MoveFileExW briefly denies readers while the presenter replaces
            # the controller snapshot. Keep the last valid state and retry on
            # the next native slice instead of terminating guest execution.
            self.controller_read_defer_count += 1
            return False
        if bool(payload.get("stop", False)):
            self.stop_requested = True
        port = payload.get("ports", {}).get("0", payload.get("port0", {}))
        state = ControllerState(
            connected=bool(port.get("connected", False)),
            buttons=int(port.get("buttons", 0)) & 0xFFFF,
            left_trigger=int(port.get("left_trigger", 0)) & 0xFF,
            right_trigger=int(port.get("right_trigger", 0)) & 0xFF,
            thumb_lx=max(-32768, min(32767, int(port.get("thumb_lx", 0)))),
            thumb_ly=max(-32768, min(32767, int(port.get("thumb_ly", 0)))),
            thumb_rx=max(-32768, min(32767, int(port.get("thumb_rx", 0)))),
            thumb_ry=max(-32768, min(32767, int(port.get("thumb_ry", 0)))),
        )
        self.runtime.input.set_controller_state(0, state)
        self.controller_mtime_ns = stat.st_mtime_ns
        self.controller_update_count += 1
        self._write_json_atomic(
            self.controller_consumed_path,
            {
                "format": "b2-recomp-controller-consumed",
                "controller_update": self.controller_update_count,
                "source_mtime_ns": stat.st_mtime_ns,
                "connected": state.connected,
                "buttons": state.buttons,
            },
        )
        return True

    def on_slice(
        self,
        _state: CpuState,
        memory: SparseMemory,
        _steps: int,
        force_publish: bool = False,
    ) -> bool:
        slice_started_ns = time.perf_counter_ns()
        self.slice_exchange_count += 1
        flip_delta = max(0, self.render_watchpoint.flip_count - self._clocked_flip_count)
        if flip_delta:
            # The native guest can spend an entire frontend frame without
            # entering a wait shim. Advance the deterministic Xbox clock at
            # the video cadence so menu transitions and animation timers do
            # not remain frozen near boot forever.
            self.runtime.clock.advance_100ns(flip_delta * 166_667)
            self.clock_advanced_flip_count += flip_delta
            self._clocked_flip_count = self.render_watchpoint.flip_count
        if self.data_export_synchronizer is not None:
            data_export_started_ns = time.perf_counter_ns()
            self.data_export_synchronizer(memory)
            self._record_performance("data_export_sync", data_export_started_ns)
        controller_started_ns = time.perf_counter_ns()
        self.sample_controller()
        self._record_performance("controller_sample", controller_started_ns)
        if self.flip_audit_ack_path is not None:
            self._flush_render_commands(
                force=self.render_watchpoint.has_pending_flip(),
            )
            if self.render_watchpoint.has_pending_flip():
                boundaries = self.render_watchpoint.consume_pending_flip_boundaries()
                if len(boundaries) != 1:
                    raise RuntimeError(
                        "lossless flip audit observed multiple flips before a native yield"
                    )
                boundary = boundaries[0]
                health_reason = self._flip_audit_selection_reason(boundary, memory)
                if health_reason is not None:
                    selected_boundary = {**boundary, "health_reason": health_reason}
                    publish_attempt_started_ns = time.perf_counter_ns()
                    self.publish_render(
                        memory,
                        force=True,
                        audit_flip=selected_boundary,
                        guest_steps=_steps,
                    )
                    self._record_performance(
                        "publish_render_attempt",
                        publish_attempt_started_ns,
                    )
                    audit_wait_started_ns = time.perf_counter_ns()
                    acknowledged = self._wait_for_flip_ack(boundary["flip_index"])
                    self._record_performance("audit_ack_wait", audit_wait_started_ns)
                    if not acknowledged:
                        self._record_performance("on_slice_total", slice_started_ns)
                        return False
                    if self._last_published_manifest is None:
                        raise RuntimeError("lossless flip audit manifest was not retained")
                    ledger_record = self._last_published_manifest
                    self.audit_selected_flip_count += 1
                else:
                    ledger_record = {
                        "format": "b2-recomp-flip-audit-ledger-record",
                        "command_stream_generation": self.command_stream_generation,
                        "audit_flip_index": int(boundary["flip_index"]),
                        "audit_flip_value": int(boundary["flip_value"]),
                        "audit_command_record_count": int(boundary["write_count"]),
                        "audit_guest_steps": int(_steps),
                        "audit_health_selected": False,
                        "audit_health_reason": "not_selected",
                    }
                    self.audit_skipped_flip_count += 1
                if self.flip_audit_ledger_path is not None:
                    self._queue_flip_audit_ledger_record(ledger_record)
                self.audited_flip_count += 1
                if (
                    self.flip_audit_max_flips > 0
                    and self.audited_flip_count >= self.flip_audit_max_flips
                ):
                    self.stop_requested = True
                    self._flush_flip_audit_ledger()
        else:
            boundaries = (
                self.render_watchpoint.consume_pending_flip_boundaries()
                if self.render_watchpoint.has_pending_flip()
                else []
            )
            self._flush_render_commands(
                force=bool(boundaries) or force_publish,
            )
            completed_flip = boundaries[-1] if boundaries else None
            # The validated manifest is the presenter's frame boundary. Do not
            # publish an empty readiness manifest before the first completed
            # flip: live_test waits for this file before starting the host.
            should_publish_manifest = completed_flip is not None
            if should_publish_manifest:
                self._pace_completed_flip()
                publish_attempt_started_ns = time.perf_counter_ns()
                self.publish_render(
                    memory,
                    force=True,
                    completed_flip=completed_flip,
                    guest_steps=_steps,
                )
                self._record_performance(
                    "publish_render_attempt",
                    publish_attempt_started_ns,
                )
                presentation_wait_started_ns = time.perf_counter_ns()
                acknowledged = self._wait_for_presentation_ack(
                    int(completed_flip["flip_index"])
                )
                self._record_performance(
                    "presentation_ack_wait",
                    presentation_wait_started_ns,
                )
                if not acknowledged:
                    self._record_performance("on_slice_total", slice_started_ns)
                    return False
        self._record_performance("on_slice_total", slice_started_ns)
        return not self.stop_requested

    def summary(self) -> dict[str, Any]:
        self._close_render_command_file()
        if self._presentation_ack_file is not None:
            self._presentation_ack_file.close()
            self._presentation_ack_file = None
        if (
            self._presentation_event_handle is not None
            and self._presentation_event_kernel is not None
        ):
            self._presentation_event_kernel.CloseHandle(
                ctypes.c_void_p(self._presentation_event_handle)
            )
            self._presentation_event_handle = None
        if (
            self._publication_event_handle is not None
            and self._presentation_event_kernel is not None
        ):
            self._presentation_event_kernel.CloseHandle(
                ctypes.c_void_p(self._publication_event_handle)
            )
            self._publication_event_handle = None
        self._flush_flip_audit_ledger()
        recent_guest_flip_rate_hz = 0.0
        if len(self._publish_cadence_samples) >= 2:
            first_flip, first_time = self._publish_cadence_samples[0]
            last_flip, last_time = self._publish_cadence_samples[-1]
            elapsed_seconds = max(0.0, (last_time - first_time) / 1_000_000_000.0)
            if elapsed_seconds > 0.0:
                recent_guest_flip_rate_hz = max(0, last_flip - first_flip) / elapsed_seconds
        return {
            "render_stream_path": str(self.render_stream_path),
            "render_resource_path": str(self.render_resource_path),
            "current_render_resource_path": str(self.current_render_resource_path),
            "render_command_path": str(self.render_command_path),
            "controller_state_path": str(self.controller_state_path),
            "controller_consumed_path": str(self.controller_consumed_path),
            "stop_requested": self.stop_requested,
            "render_publish_count": self.render_publish_count,
            "recent_guest_flip_sample_count": len(self._publish_cadence_samples),
            "recent_guest_flip_rate_hz": round(recent_guest_flip_rate_hz, 3),
            "published_manifest_write_count": self.published_manifest_write_count,
            "last_published_manifest_flip_count": (
                self.last_published_manifest_flip_count
            ),
            "slice_exchange_count": self.slice_exchange_count,
            "render_resource_publish_count": self.render_resource_publish_count,
            "render_resource_scan_count": self.render_resource_scan_count,
            "controller_update_count": self.controller_update_count,
            "controller_read_defer_count": self.controller_read_defer_count,
            "clock_advanced_flip_count": self.clock_advanced_flip_count,
            "video_pacing_target_hz": 60,
            "video_pacing_sleep_count": self.video_pacing_sleep_count,
            "video_pacing_sleep_us": self.video_pacing_sleep_us,
            "video_pacing_max_sleep_us": self.video_pacing_max_sleep_us,
            "lossless_flip_audit": self.flip_audit_ack_path is not None,
            "flip_audit_ack_path": (
                str(self.flip_audit_ack_path)
                if self.flip_audit_ack_path is not None
                else None
            ),
            "presentation_ack_path": (
                str(self.presentation_ack_path)
                if self.presentation_ack_path is not None
                else None
            ),
            "presentation_ack_wait_count": self.presentation_ack_wait_count,
            "presentation_event_name": self.presentation_event_name,
            "publication_event_name": self.publication_event_name,
            "flip_audit_ledger_path": (
                str(self.flip_audit_ledger_path)
                if self.flip_audit_ledger_path is not None
                else None
            ),
            "audited_flip_count": self.audited_flip_count,
            "audit_selected_flip_count": self.audit_selected_flip_count,
            "audit_skipped_flip_count": self.audit_skipped_flip_count,
            "audit_ack_wait_count": self.audit_ack_wait_count,
            "published_write_count": max(0, self.published_write_count),
            "performance": self._performance_summary(),
        }


def _snapshot_render_texture_resources(
    stream: dict[str, Any],
    history_stream: dict[str, Any],
    memory: SparseMemory,
    *,
    cache: dict[
        tuple[int, int, int], tuple[tuple[int, ...], dict[str, Any]]
    ] | None = None,
) -> dict[str, Any]:
    retained_bindings = history_stream.get("texture_bindings", [])
    texture_states = (
        [tuple(int(value) for value in binding) for binding in retained_bindings]
        if retained_bindings
        else _scan_render_texture_bindings(history_stream.get("writes", []))
    )
    resources: dict[int, dict[str, Any]] = {}
    for stage, address, format_raw in texture_states:
        width = 1 << ((format_raw >> 20) & 0xF)
        height = 1 << ((format_raw >> 24) & 0xF)
        color_format = (format_raw >> 8) & 0xFF
        format_name = {
            0x05: "R5G6B5",
            0x06: "A8R8G8B8",
            0x07: "X8R8G8B8",
            0x0C: "DXT1",
            0x0E: "DXT3",
            0x0F: "DXT5",
            0x12: "A8R8G8B8_LINEAR",
            0x1E: "X8R8G8B8_LINEAR",
        }.get(color_format, f"format_{color_format:02X}")
        if address == 0 or width <= 0 or height <= 0:
            continue
        if format_name == "DXT1":
            byte_count = max(8, ((width + 3) // 4) * ((height + 3) // 4) * 8)
        elif format_name in {"DXT3", "DXT5"}:
            byte_count = max(16, ((width + 3) // 4) * ((height + 3) // 4) * 16)
        elif format_name in {"R5G6B5"}:
            byte_count = width * height * 2
        else:
            byte_count = width * height * 4
        if byte_count > 16 * 1024 * 1024:
            continue
        cache_key = (stage, address, format_raw)
        first_page = address & ~memory._PAGE_MASK
        last_page = (address + byte_count - 1) & ~memory._PAGE_MASK
        page_generations = tuple(
            memory.page_generation(page_address)
            for page_address in range(first_page, last_page + 1, memory._PAGE_SIZE)
        )
        cached = cache.get(cache_key) if cache is not None else None
        if cached is not None and cached[0] == page_generations:
            resources[address] = cached[1]
            continue
        payload = memory.read(address, byte_count)
        resource = {
            "stage": stage,
            "address": address,
            "address_hex": _hex32(address),
            "format": format_name,
            "width": width,
            "height": height,
            "byte_count": byte_count,
            "nonzero_byte_count": sum(byte != 0 for byte in payload),
            "sha256": hashlib.sha256(payload).hexdigest().upper(),
            "bytes_hex": payload.hex().upper(),
        }
        resources[address] = resource
        if cache is not None:
            cache[cache_key] = (page_generations, resource)
    stream["resource_snapshots"] = list(resources.values())
    stream["resource_snapshot_count"] = len(resources)
    return stream


def _scan_render_texture_bindings(
    writes: list[dict[str, Any]],
) -> list[tuple[int, int, int]]:
    """Find texture offset/format pairs without materializing a full frame decode."""

    bindings: list[tuple[int, int, int]] = []
    seen: set[tuple[int, int, int]] = set()
    drawn_bindings: list[tuple[int, int, int]] = []
    drawn_seen: set[tuple[int, int, int]] = set()
    offsets: dict[int, int] = {}
    formats: dict[int, int] = {}
    pending = bytearray()
    pending_end: int | None = None

    def observe_method(method: int, data: int) -> None:
        if method == 0x17FC and data != 0:
            for stage in sorted(offsets.keys() & formats.keys()):
                binding = (stage, offsets[stage], formats[stage])
                if binding not in drawn_seen:
                    drawn_seen.add(binding)
                    drawn_bindings.append(binding)
            return
        if not 0x1B00 <= method < 0x1C00:
            return
        stage = (method - 0x1B00) // 0x40
        register = (method - 0x1B00) % 0x40
        if register == 0:
            offsets[stage] = data
        elif register == 4:
            formats[stage] = data
        if stage in offsets and stage in formats:
            binding = (stage, offsets[stage], formats[stage])
            if binding not in seen:
                seen.add(binding)
                bindings.append(binding)

    def flush() -> None:
        nonlocal pending, pending_end
        words = [
            int.from_bytes(pending[offset : offset + 4], "little")
            for offset in range(0, len(pending) - (len(pending) % 4), 4)
        ]
        index = 0
        while index < len(words):
            command = words[index]
            non_increasing = False
            header_words = 1
            if (command & 0xE0030003) == 0:
                count = (command >> 18) & 0x7FF
            elif (command & 0xE0030003) == 0x40000000:
                count = (command >> 18) & 0x7FF
                non_increasing = True
            elif (command & 0xFFFF0003) == 0x00030000 and index + 1 < len(words):
                count = words[index + 1] & 0x00FFFFFF
                non_increasing = True
                header_words = 2
            else:
                index += 1
                continue
            first_method = ((command >> 2) & 0x7FF) * 4
            data_start = index + header_words
            data_end = min(data_start + count, len(words))
            for data_index in range(data_start, data_end):
                method_offset = 0 if non_increasing else data_index - data_start
                observe_method(first_method + method_offset * 4, words[data_index])
            index = data_end
        pending = bytearray()
        pending_end = None

    for write in writes:
        if write.get("kind") != "d3d_push_buffer":
            flush()
            continue
        address = int(write.get("address", 0))
        if pending and address != pending_end:
            flush()
        bytes_hex = write.get("bytes_hex")
        try:
            payload = bytes.fromhex(bytes_hex) if isinstance(bytes_hex, str) else b""
        except ValueError:
            payload = b""
        if not payload:
            size = max(1, min(int(write.get("size", 4)), 4))
            payload = int(write.get("value", 0)).to_bytes(4, "little")[:size]
        pending.extend(payload)
        pending_end = address + len(payload)
    flush()
    # Offset and format registers are often written separately. Pairing them at
    # every individual state write creates transient combinations (for example,
    # a new DXT5 format with the previous 512x512 DXT1 background address).
    # Prefer the complete state that was active when a draw actually began.
    return drawn_bindings or bindings


@dataclass(frozen=True)
class RecoveryWorkItem:
    target: int
    depth: int
    caller: int
    kind: str


@dataclass(frozen=True)
class RuntimeAbiInvocation:
    target_address: int
    shim_name: str
    ordinal: int
    subsystem: str
    guest_stack_pointer: int
    guest_return_address: int
    arguments: tuple[int, ...]
    handler_arguments: tuple[int, ...]
    return_kind: str
    eax: int | None = None
    stack_cleanup_bytes: int = 0
    result: Any = None
    memory_writes: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_address": self.target_address,
            "target_address_hex": _hex32(self.target_address),
            "shim_name": self.shim_name,
            "ordinal": self.ordinal,
            "subsystem": self.subsystem,
            "guest_stack_pointer": self.guest_stack_pointer,
            "guest_stack_pointer_hex": _hex32(self.guest_stack_pointer),
            "guest_return_address": self.guest_return_address,
            "guest_return_address_hex": _hex32(self.guest_return_address),
            "arguments": [_hex32(argument) for argument in self.arguments],
            "handler_arguments": [
                _hex32(argument) for argument in self.handler_arguments
            ],
            "return_kind": self.return_kind,
            "eax": self.eax,
            "eax_hex": _hex32(self.eax) if self.eax is not None else None,
            "stack_cleanup_bytes": self.stack_cleanup_bytes,
            "result": _json_safe(self.result),
            "memory_writes": [_json_safe(write) for write in self.memory_writes],
        }


@dataclass(frozen=True)
class RuntimeAbiResult:
    invocation: RuntimeAbiInvocation
    returned_value: Any

    def to_dict(self) -> dict[str, Any]:
        return self.invocation.to_dict()


class RuntimeAbiBridge:
    """Adapt lifted IA-32 calls to registered Python runtime shim handlers."""

    def __init__(
        self,
        runtime: XboxRuntimeShims,
        *,
        max_guest_arguments: int = DEFAULT_MAX_GUEST_ARGUMENTS,
        max_invocation_history: int = DEFAULT_RUNTIME_ABI_HISTORY,
    ) -> None:
        self.runtime = runtime
        self.max_guest_arguments = max_guest_arguments
        self._by_target = {
            shim.target_address: shim for shim in runtime.registered_shims
        }
        self._handler_argument_counts: dict[int, int] = {}
        for shim in self._by_target.values():
            self._handler_argument_count_for(shim)
        self._guest_argument_counts = {
            target: min(
                GUEST_ARGUMENT_COUNT_OVERRIDES.get(
                    shim.name, self._handler_argument_counts[target]
                ),
                self.max_guest_arguments,
            )
            for target, shim in self._by_target.items()
        }
        self._scalar_data_exports = tuple(
            (target, shim)
            for target, shim in self._by_target.items()
            if shim.behavior == "data"
        )
        self._volatile_data_exports = tuple(
            (target, shim)
            for target, shim in self._scalar_data_exports
            if shim.name == "KeTickCount"
        )
        self._invocations: deque[RuntimeAbiInvocation] = deque(
            maxlen=max_invocation_history
        )
        self._invocation_count = 0
        self._caller_counts: Counter[tuple[str, int]] = Counter()
        self._failed_caller_counts: Counter[tuple[str, int]] = Counter()
        self._materialized_data_exports: dict[int, tuple[str, int]] = {}
        self._data_export_update_count = 0

    @property
    def invocations(self) -> tuple[RuntimeAbiInvocation, ...]:
        return tuple(self._invocations)

    @property
    def invocation_count(self) -> int:
        return self._invocation_count

    @property
    def shim_targets(self) -> frozenset[int]:
        return frozenset(self._by_target)

    def has_target(self, target: int) -> bool:
        return target in self._by_target

    def call_handlers(
        self,
    ) -> dict[int, Callable[[CpuState, SparseMemory, int, ExecutionTrace], None]]:
        return {target: self.call_handler for target in self._by_target}

    def call_handler(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
    ) -> None:
        self.invoke(cpu, memory, target, trace)

    def synchronize_data_exports(
        self,
        memory: SparseMemory,
        *,
        volatile_only: bool = False,
    ) -> None:
        """Materialize imported kernel ULONG exports at their guest addresses."""
        exports = (
            self._volatile_data_exports
            if volatile_only
            else self._scalar_data_exports
        )
        for target, shim in exports:
            value = shim.handler()
            if not isinstance(value, (bool, int)):
                # Pointer-sized strings, structures, and key arrays need
                # dedicated layouts rather than overlapping the fixed host
                # target slots. Materialize only observed scalar exports here.
                continue
            scalar = _u32(int(value))
            previous = self._materialized_data_exports.get(target)
            if previous is not None and previous[1] == scalar:
                continue
            memory.write_u32(target, scalar)
            self._materialized_data_exports[target] = (shim.name, scalar)
            self._data_export_update_count += 1

    def invoke(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
    ) -> RuntimeAbiResult:
        shim = self._by_target.get(target)
        if shim is None:
            raise RuntimeAbiBridgeError(f"no registered runtime shim at {_hex32(target)}")

        guest_stack_pointer = cpu.get_register("esp")
        guest_return_address = memory.read_u32(guest_stack_pointer)
        guest_argument_count = self._guest_argument_count_for(shim)
        handler_argument_count = self._handler_argument_count_for(shim)
        arguments = _read_stack_arguments(
            memory, cpu.get_register("esp"), guest_argument_count
        )
        if shim.name in {"NtCreateFile", "NtOpenFile"}:
            handler_arguments = arguments
            returned_value = self._invoke_guest_file_api(shim, arguments, memory, trace)
        elif shim.name == "NtAllocateVirtualMemory":
            handler_arguments = arguments
            returned_value = self._invoke_guest_allocate_virtual_memory_api(
                arguments, memory, trace
            )
        elif shim.name == "NtCreateSemaphore":
            handler_arguments = arguments
            returned_value = self._invoke_guest_create_semaphore_api(arguments, trace)
        elif shim.name == "NtOpenSymbolicLinkObject":
            handler_arguments = arguments
            returned_value = self._invoke_guest_open_symbolic_link_api(
                arguments, memory, trace
            )
        elif shim.name == "NtQuerySymbolicLinkObject":
            handler_arguments = arguments
            returned_value = self._invoke_guest_query_symbolic_link_api(arguments, trace)
        elif shim.name == "NtQueryInformationFile":
            handler_arguments = arguments
            returned_value = self._invoke_guest_query_information_file_api(arguments, trace)
        elif shim.name == "NtQueryDirectoryFile":
            handler_arguments = arguments
            returned_value = self._invoke_guest_query_directory_file_api(
                arguments, memory, trace
            )
        elif shim.name == "NtQueryVolumeInformationFile":
            handler_arguments = arguments
            returned_value = self._invoke_guest_query_volume_information_file_api(
                arguments, trace
            )
        elif shim.name == "NtReadFile":
            handler_arguments = arguments
            returned_value = self._invoke_guest_read_file_api(arguments, memory, trace)
        elif shim.name == "NtReleaseSemaphore":
            handler_arguments = arguments[:handler_argument_count]
            try:
                returned_value = shim.handler(*handler_arguments)
            except (TypeError, XboxRuntimeError) as exc:
                trace.add(
                    None,
                    "runtime_abi_error",
                    target=target,
                    target_hex=_hex32(target),
                    shim_name=shim.name,
                    arguments=[_hex32(argument) for argument in arguments],
                    handler_arguments=[
                        _hex32(argument) for argument in handler_arguments
                    ],
                    error=str(exc),
                )
                raise RuntimeAbiBridgeError(
                    f"runtime ABI call to {shim.name} failed: {exc}"
                ) from exc
        elif shim.name == "NtSetInformationFile":
            handler_arguments = arguments
            returned_value = self._invoke_guest_set_information_file_api(arguments, memory, trace)
        elif shim.name == "NtWriteFile":
            handler_arguments = arguments
            returned_value = self._invoke_guest_write_file_api(arguments, memory, trace)
        elif shim.name == "HalReadWritePCISpace":
            handler_arguments = arguments
            returned_value = self._invoke_guest_hal_read_write_pci_space_api(
                arguments, memory, trace
            )
        elif shim.name == "KeQuerySystemTime":
            handler_arguments = ()
            returned_value = {
                "output_address": arguments[0],
                "system_time": shim.handler(),
            }
        elif shim.name == "RtlCompareMemoryUlong":
            handler_arguments = arguments
            returned_value = self._invoke_guest_compare_memory_ulong_api(
                arguments, memory, trace
            )
        elif shim.name == "ObfDereferenceObject":
            handler_arguments = (cpu.get_register("ecx"),)
            try:
                returned_value = shim.handler(*handler_arguments)
            except (TypeError, XboxRuntimeError) as exc:
                trace.add(
                    None,
                    "runtime_abi_error",
                    target=target,
                    target_hex=_hex32(target),
                    shim_name=shim.name,
                    arguments=[_hex32(argument) for argument in arguments],
                    handler_arguments=[
                        _hex32(argument) for argument in handler_arguments
                    ],
                    error=str(exc),
                )
                raise RuntimeAbiBridgeError(
                    f"runtime ABI call to {shim.name} failed: {exc}"
                ) from exc
        else:
            handler_arguments = arguments[:handler_argument_count]
            try:
                returned_value = shim.handler(*handler_arguments)
            except (TypeError, XboxRuntimeError) as exc:
                trace.add(
                    None,
                    "runtime_abi_error",
                    target=target,
                    target_hex=_hex32(target),
                    shim_name=shim.name,
                    arguments=[_hex32(argument) for argument in arguments],
                    handler_arguments=[
                        _hex32(argument) for argument in handler_arguments
                    ],
                    error=str(exc),
                )
                raise RuntimeAbiBridgeError(
                    f"runtime ABI call to {shim.name} failed: {exc}"
                ) from exc

        memory_writes = self._apply_guest_side_effects(
            shim, arguments, returned_value, memory, trace
        )
        # Waits and other runtime services can advance the deterministic clock.
        # Keep volatile kernel data coherent before returning to guest code.
        self.synchronize_data_exports(memory, volatile_only=True)
        return_kind, eax = _apply_return_value(cpu, returned_value)
        stack_cleanup_bytes = guest_argument_count * 4
        if stack_cleanup_bytes:
            _prepare_stdcall_return(cpu, memory, stack_cleanup_bytes)
        invocation = RuntimeAbiInvocation(
            target_address=target,
            shim_name=shim.name,
            ordinal=shim.ordinal,
            subsystem=shim.subsystem,
            guest_stack_pointer=guest_stack_pointer,
            guest_return_address=guest_return_address,
            arguments=arguments,
            handler_arguments=handler_arguments,
            return_kind=return_kind,
            eax=eax,
            stack_cleanup_bytes=stack_cleanup_bytes,
            result=_runtime_abi_result_for_summary(returned_value),
            memory_writes=tuple(memory_writes),
        )
        self._invocations.append(invocation)
        self._invocation_count += 1
        caller_key = (shim.name, guest_return_address)
        self._caller_counts[caller_key] += 1
        status = (
            returned_value.get("status")
            if isinstance(returned_value, dict)
            else returned_value
            if shim.name.startswith(("Nt", "Io", "Ob"))
            else None
        )
        if isinstance(status, int) and status & 0x80000000:
            self._failed_caller_counts[caller_key] += 1
        if trace.enabled:
            trace.add(None, "runtime_abi_call", **invocation.to_dict())
        return RuntimeAbiResult(invocation, returned_value)

    def summary(self) -> dict[str, Any]:
        subsystem_counts = Counter(shim.subsystem for shim in self._by_target.values())
        return {
            "registered_target_count": len(self._by_target),
            "registered_subsystem_counts": dict(sorted(subsystem_counts.items())),
            "invocation_count": self._invocation_count,
            "retained_invocation_count": len(self._invocations),
            "invocation_history_truncated": self._invocation_count > len(self._invocations),
            "data_export_update_count": self._data_export_update_count,
            "caller_counts": self._caller_count_summary(self._caller_counts),
            "failed_caller_counts": self._caller_count_summary(
                self._failed_caller_counts
            ),
            "materialized_data_exports": [
                {
                    "name": name,
                    "target_address": target,
                    "target_address_hex": _hex32(target),
                    "value": value,
                    "value_hex": _hex32(value),
                }
                for target, (name, value) in sorted(
                    self._materialized_data_exports.items()
                )
            ],
            "invocations": [invocation.to_dict() for invocation in self._invocations],
        }

    @staticmethod
    def _caller_count_summary(
        counts: Counter[tuple[str, int]],
        *,
        limit: int = 256,
    ) -> list[dict[str, Any]]:
        return [
            {
                "shim_name": shim_name,
                "guest_return_address": return_address,
                "guest_return_address_hex": _hex32(return_address),
                "invocation_count": count,
            }
            for (shim_name, return_address), count in counts.most_common(limit)
        ]

    def _guest_argument_count_for(self, shim: RuntimeShim) -> int:
        return self._guest_argument_counts[shim.target_address]

    def _handler_argument_count_for(self, shim: RuntimeShim) -> int:
        cached = self._handler_argument_counts.get(shim.target_address)
        if cached is not None:
            return cached
        signature = inspect.signature(shim.handler)
        count = 0
        for parameter in signature.parameters.values():
            if parameter.kind in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            }:
                count += 1
            elif parameter.kind == inspect.Parameter.VAR_POSITIONAL:
                break
        count = min(count, self.max_guest_arguments)
        self._handler_argument_counts[shim.target_address] = count
        return count

    def _invoke_guest_file_api(
        self,
        shim: RuntimeShim,
        arguments: tuple[int, ...],
        memory: SparseMemory,
        trace: ExecutionTrace,
    ) -> dict[str, Any]:
        if shim.name == "NtOpenFile":
            (
                file_handle_address,
                desired_access,
                object_attributes_address,
                io_status_block_address,
                share_access,
                open_options,
            ) = arguments[:6]
            create_disposition = None
            create_options = open_options
        else:
            (
                file_handle_address,
                desired_access,
                object_attributes_address,
                io_status_block_address,
                _allocation_size,
                _file_attributes,
                share_access,
                create_disposition,
                create_options,
                _ea_buffer,
                _ea_length,
            ) = arguments[:11]
        decoded = _decode_guest_object_path(memory, object_attributes_address)
        mode = _guest_file_mode(desired_access, create_disposition)
        if decoded is None:
            result: dict[str, Any] = {
                "status": XboxStatus.INVALID_PARAMETER,
                "handle": None,
                "guest_path": None,
                "decode_error": "object name not decoded",
            }
        else:
            result = self.runtime.filesystem.open_file(decoded["guest_path"], mode)
            result.update(decoded)
        result.update(
            {
                "file_handle_address": file_handle_address,
                "desired_access": desired_access,
                "object_attributes_address": object_attributes_address,
                "io_status_block_address": io_status_block_address,
                "share_access": share_access,
                "create_disposition": create_disposition,
                "create_options": create_options,
                "mode": mode,
            }
        )
        trace.add(
            None,
            "runtime_file_api",
            shim_name=shim.name,
            guest_path=result.get("guest_path"),
            status=result.get("status"),
            status_hex=_hex32(result.get("status", 0)),
            file_handle_address=file_handle_address,
            file_handle_address_hex=_hex32(file_handle_address),
            object_attributes_address=object_attributes_address,
            object_attributes_address_hex=_hex32(object_attributes_address),
        )
        return result

    def _invoke_guest_allocate_virtual_memory_api(
        self,
        arguments: tuple[int, ...],
        memory: SparseMemory,
        trace: ExecutionTrace,
    ) -> dict[str, Any]:
        (
            base_address_address,
            zero_bits,
            region_size_address,
            allocation_type,
            protection,
        ) = arguments[:5]
        requested_base = (
            memory.read_u32(base_address_address) if base_address_address else 0
        )
        requested_size = (
            memory.read_u32(region_size_address) if region_size_address else 0
        )
        if requested_size <= 0:
            result: dict[str, Any] = {
                "status": XboxStatus.INVALID_PARAMETER,
                "allocated_address": None,
                "allocated_size": 0,
            }
        else:
            allocation_policy = "fresh_allocation"
            allocated_address = None
            if requested_base and allocation_type & MEM_COMMIT:
                existing = self.runtime.memory.query(requested_base)
                if existing.get("status") == XboxStatus.SUCCESS:
                    allocated_address = requested_base
                    allocation_policy = "commit_existing_reservation"
            if allocated_address is None:
                allocated_address = self.runtime.memory.allocate_system_memory(
                    requested_size,
                    _guest_page_protection(protection),
                )
            result = {
                "status": XboxStatus.SUCCESS,
                "allocated_address": allocated_address,
                "allocated_size": requested_size,
                "allocation_policy": allocation_policy,
            }
        result.update(
            {
                "base_address_address": base_address_address,
                "zero_bits": zero_bits,
                "region_size_address": region_size_address,
                "allocation_type": allocation_type,
                "protection": protection,
                "protection_model": _guest_page_protection(protection),
                "requested_base": requested_base,
                "requested_size": requested_size,
            }
        )
        trace.add(
            None,
            "runtime_virtual_memory_api",
            shim_name="NtAllocateVirtualMemory",
            status=result["status"],
            status_hex=_hex32(result["status"]),
            base_address_address=base_address_address,
            base_address_address_hex=_hex32(base_address_address),
            region_size_address=region_size_address,
            region_size_address_hex=_hex32(region_size_address),
            requested_base=requested_base,
            requested_base_hex=_hex32(requested_base),
            requested_size=requested_size,
            allocated_address=result.get("allocated_address"),
            allocated_address_hex=_hex32(result["allocated_address"])
            if isinstance(result.get("allocated_address"), int)
            else None,
            allocation_type=allocation_type,
            protection=protection,
        )
        return result

    def _invoke_guest_create_semaphore_api(
        self,
        arguments: tuple[int, ...],
        trace: ExecutionTrace,
    ) -> dict[str, int]:
        if len(arguments) < 4:
            raise RuntimeAbiBridgeError(
                "NtCreateSemaphore expected handle pointer, object attributes, "
                "initial count, and limit"
            )
        handle_address, object_attributes, initial_count, limit = arguments[:4]
        try:
            handle = self.runtime.nt_create_semaphore(initial_count, limit)
        except XboxRuntimeError as exc:
            trace.add(
                None,
                "runtime_semaphore_api_error",
                shim_name="NtCreateSemaphore",
                handle_address=handle_address,
                handle_address_hex=_hex32(handle_address),
                object_attributes=object_attributes,
                object_attributes_hex=_hex32(object_attributes),
                initial_count=initial_count,
                limit=limit,
                error=str(exc),
            )
            raise RuntimeAbiBridgeError(
                f"runtime ABI call to NtCreateSemaphore failed: {exc}"
            ) from exc
        result = {
            "status": XboxStatus.SUCCESS,
            "handle": handle,
            "handle_address": handle_address,
            "object_attributes": object_attributes,
            "initial_count": initial_count,
            "limit": limit,
        }
        trace.add(
            None,
            "runtime_semaphore_api",
            shim_name="NtCreateSemaphore",
            handle=handle,
            handle_hex=_hex32(handle),
            handle_address=handle_address,
            handle_address_hex=_hex32(handle_address),
            object_attributes=object_attributes,
            object_attributes_hex=_hex32(object_attributes),
            initial_count=initial_count,
            limit=limit,
        )
        return result

    def _invoke_guest_open_symbolic_link_api(
        self,
        arguments: tuple[int, ...],
        memory: SparseMemory,
        trace: ExecutionTrace,
    ) -> dict[str, Any]:
        (
            link_handle_address,
            object_attributes_address,
        ) = arguments[:2]
        decoded = _decode_guest_object_path(memory, object_attributes_address)
        if decoded is None:
            result: dict[str, Any] = {
                "status": XboxStatus.INVALID_PARAMETER,
                "handle": None,
                "guest_path": None,
                "decode_error": "object name not decoded",
            }
        else:
            result = self.runtime.nt_open_symbolic_link_object(decoded["guest_path"])
            result.update(decoded)
        result.update(
            {
                "link_handle_address": link_handle_address,
                "desired_access": None,
                "object_attributes_address": object_attributes_address,
            }
        )
        trace.add(
            None,
            "runtime_symbolic_link_api",
            shim_name="NtOpenSymbolicLinkObject",
            guest_path=result.get("guest_path"),
            target=result.get("target"),
            status=result.get("status"),
            status_hex=_hex32(result.get("status", 0)),
            link_handle_address=link_handle_address,
            link_handle_address_hex=_hex32(link_handle_address),
            object_attributes_address=object_attributes_address,
            object_attributes_address_hex=_hex32(object_attributes_address),
        )
        return result

    def _invoke_guest_query_symbolic_link_api(
        self,
        arguments: tuple[int, ...],
        trace: ExecutionTrace,
    ) -> dict[str, Any]:
        (
            handle,
            link_target_address,
            returned_length_address,
        ) = arguments[:3]
        result = self.runtime.nt_query_symbolic_link_object(handle)
        result.update(
            {
                "handle": handle,
                "link_target_address": link_target_address,
                "returned_length_address": returned_length_address,
            }
        )
        trace.add(
            None,
            "runtime_symbolic_link_api",
            shim_name="NtQuerySymbolicLinkObject",
            handle=handle,
            handle_hex=_hex32(handle),
            link_target_address=link_target_address,
            link_target_address_hex=_hex32(link_target_address),
            returned_length_address=returned_length_address,
            returned_length_address_hex=_hex32(returned_length_address),
            target=result.get("target"),
            status=result.get("status"),
            status_hex=_hex32(result.get("status", 0)),
        )
        return result

    def _invoke_guest_query_information_file_api(
        self,
        arguments: tuple[int, ...],
        trace: ExecutionTrace,
    ) -> dict[str, Any]:
        (
            handle,
            io_status_block_address,
            file_information_address,
            length,
            file_information_class,
        ) = arguments[:5]
        result = self.runtime.filesystem.query_file_handle_information(handle)
        result.update(
            {
                "handle": handle,
                "io_status_block_address": io_status_block_address,
                "file_information_address": file_information_address,
                "requested_length": length,
                "file_information_class": file_information_class,
            }
        )
        trace.add(
            None,
            "runtime_file_query_api",
            shim_name="NtQueryInformationFile",
            handle=handle,
            handle_hex=_hex32(handle),
            io_status_block_address=io_status_block_address,
            io_status_block_address_hex=_hex32(io_status_block_address),
            file_information_address=file_information_address,
            file_information_address_hex=_hex32(file_information_address),
            requested_length=length,
            file_information_class=file_information_class,
            status=result.get("status"),
            status_hex=_hex32(result.get("status", 0)),
            file_size=result.get("size", 0),
        )
        return result

    def _invoke_guest_query_directory_file_api(
        self,
        arguments: tuple[int, ...],
        memory: SparseMemory,
        trace: ExecutionTrace,
    ) -> dict[str, Any]:
        (
            handle,
            _event,
            _apc_routine,
            _apc_context,
            io_status_block_address,
            file_information_address,
            length,
            file_information_class,
            file_name_address,
            restart_scan,
        ) = arguments[:10]
        decoded_mask = _read_guest_ansi_string(
            memory, file_name_address, require_path=False
        )
        file_mask = decoded_mask[0] if decoded_mask is not None else None
        if file_information_class != 1:
            listing = {"status": XboxStatus.INVALID_INFO_CLASS}
        else:
            listing = self.runtime.filesystem.query_directory_entry(
                handle,
                pattern=file_mask,
                restart_scan=bool(restart_scan),
                max_record_length=length,
            )
        result = {
            **listing,
            "handle": handle,
            "io_status_block_address": io_status_block_address,
            "file_information_address": file_information_address,
            "requested_length": length,
            "file_information_class": file_information_class,
            "file_mask": file_mask,
            "restart_scan": bool(restart_scan),
        }
        if result.get("status") == XboxStatus.SUCCESS:
            name_bytes = str(result.get("name", "")).encode(
                "ascii", errors="replace"
            )
            result["directory_record"] = struct.pack(
                "<IIQQQQQQII",
                0,
                int(result.get("file_index", 0)),
                int(result.get("creation_time", 0)),
                int(result.get("last_access_time", 0)),
                int(result.get("last_write_time", 0)),
                int(result.get("change_time", 0)),
                int(result.get("size", 0)),
                int(result.get("allocation_size", 0)),
                int(result.get("file_attributes", 0)),
                len(name_bytes),
            ) + name_bytes
        trace.add(
            None,
            "runtime_directory_query_api",
            shim_name="NtQueryDirectoryFile",
            handle=handle,
            handle_hex=_hex32(handle),
            status=result["status"],
            status_hex=_hex32(result["status"]),
            name=result.get("name"),
            file_mask=file_mask,
        )
        return result

    def _invoke_guest_query_volume_information_file_api(
        self,
        arguments: tuple[int, ...],
        trace: ExecutionTrace,
    ) -> dict[str, Any]:
        (
            handle,
            io_status_block_address,
            file_information_address,
            length,
            file_information_class,
        ) = arguments[:5]
        result = self.runtime.nt_query_volume_information_file(handle)
        result.update(
            {
                "handle": handle,
                "io_status_block_address": io_status_block_address,
                "file_information_address": file_information_address,
                "requested_length": length,
                "file_information_class": file_information_class,
            }
        )
        payload = b""
        if result.get("status") == XboxStatus.SUCCESS:
            if file_information_class == 1:
                label = str(result.get("label", "")).encode(
                    "ascii", errors="replace"
                )
                payload = struct.pack(
                    "<QIIB",
                    0,
                    int(result.get("serial_number", 0)),
                    len(label),
                    0,
                ) + label
            elif file_information_class == 3:
                payload = struct.pack(
                    "<QQII",
                    int(result.get("total_allocation_units", 0)),
                    int(result.get("available_allocation_units", 0)),
                    int(result.get("sectors_per_allocation_unit", 0)),
                    int(result.get("bytes_per_sector", 0)),
                )
            else:
                result["status"] = XboxStatus.INVALID_INFO_CLASS
            if payload and length < len(payload):
                result["status"] = XboxStatus.INFO_LENGTH_MISMATCH
                payload = b""
        if payload:
            result["volume_information"] = payload
        trace.add(
            None,
            "runtime_volume_query_api",
            shim_name="NtQueryVolumeInformationFile",
            handle=handle,
            handle_hex=_hex32(handle),
            status=result.get("status", XboxStatus.INVALID_HANDLE),
            status_hex=_hex32(result.get("status", XboxStatus.INVALID_HANDLE)),
            file_information_class=file_information_class,
            bytes_returned=len(payload),
        )
        return result

    def _invoke_guest_set_information_file_api(
        self,
        arguments: tuple[int, ...],
        memory: SparseMemory,
        trace: ExecutionTrace,
    ) -> dict[str, Any]:
        (
            handle,
            io_status_block_address,
            file_information_address,
            length,
            file_information_class,
        ) = arguments[:5]
        payload = memory.read(file_information_address, length) if file_information_address and length else b""
        result = self.runtime.filesystem.set_file_handle_information(
            handle,
            file_information_class,
            payload,
        )
        result.update(
            {
                "handle": handle,
                "io_status_block_address": io_status_block_address,
                "file_information_address": file_information_address,
                "requested_length": length,
                "file_information_class": file_information_class,
            }
        )
        trace.add(
            None,
            "runtime_file_set_information_api",
            shim_name="NtSetInformationFile",
            handle=handle,
            handle_hex=_hex32(handle),
            io_status_block_address=io_status_block_address,
            io_status_block_address_hex=_hex32(io_status_block_address),
            file_information_address=file_information_address,
            file_information_address_hex=_hex32(file_information_address),
            requested_length=length,
            file_information_class=file_information_class,
            status=result.get("status"),
            status_hex=_hex32(result.get("status", 0)),
            position=result.get("position"),
            bytes_consumed=result.get("bytes_consumed", 0),
        )
        return result

    def _invoke_guest_read_file_api(
        self,
        arguments: tuple[int, ...],
        memory: SparseMemory,
        trace: ExecutionTrace,
    ) -> dict[str, Any]:
        (
            handle,
            event_handle,
            apc_routine,
            apc_context,
            io_status_block_address,
            buffer_address,
            length,
            byte_offset_address,
        ) = arguments[:8]
        offset = _read_guest_byte_offset(memory, byte_offset_address)
        result = self.runtime.nt_read_file(handle, length, offset)
        result.update(
            {
                "handle": handle,
                "event_handle": event_handle,
                "apc_routine": apc_routine,
                "apc_context": apc_context,
                "io_status_block_address": io_status_block_address,
                "buffer_address": buffer_address,
                "requested_length": length,
                "byte_offset_address": byte_offset_address,
                "byte_offset": offset,
            }
        )
        trace.add(
            None,
            "runtime_file_io_api",
            shim_name="NtReadFile",
            handle=handle,
            handle_hex=_hex32(handle),
            io_status_block_address=io_status_block_address,
            io_status_block_address_hex=_hex32(io_status_block_address),
            buffer_address=buffer_address,
            buffer_address_hex=_hex32(buffer_address),
            requested_length=length,
            byte_offset=offset,
            status=result.get("status"),
            status_hex=_hex32(result.get("status", 0)),
            bytes_read=result.get("bytes_read", 0),
        )
        return result

    def _invoke_guest_write_file_api(
        self,
        arguments: tuple[int, ...],
        memory: SparseMemory,
        trace: ExecutionTrace,
    ) -> dict[str, Any]:
        (
            handle,
            event_handle,
            apc_routine,
            apc_context,
            io_status_block_address,
            buffer_address,
            length,
            byte_offset_address,
        ) = arguments[:8]
        offset = _read_guest_byte_offset(memory, byte_offset_address)
        payload = memory.read(buffer_address, length) if buffer_address and length else b""
        result = self.runtime.nt_write_file(handle, payload, offset)
        result.update(
            {
                "handle": handle,
                "event_handle": event_handle,
                "apc_routine": apc_routine,
                "apc_context": apc_context,
                "io_status_block_address": io_status_block_address,
                "buffer_address": buffer_address,
                "requested_length": length,
                "byte_offset_address": byte_offset_address,
                "byte_offset": offset,
            }
        )
        trace.add(
            None,
            "runtime_file_io_api",
            shim_name="NtWriteFile",
            handle=handle,
            handle_hex=_hex32(handle),
            io_status_block_address=io_status_block_address,
            io_status_block_address_hex=_hex32(io_status_block_address),
            buffer_address=buffer_address,
            buffer_address_hex=_hex32(buffer_address),
            requested_length=length,
            byte_offset=offset,
            status=result.get("status"),
            status_hex=_hex32(result.get("status", 0)),
            bytes_written=result.get("bytes_written", 0),
        )
        return result

    def _invoke_guest_compare_memory_ulong_api(
        self,
        arguments: tuple[int, ...],
        memory: SparseMemory,
        trace: ExecutionTrace,
    ) -> int:
        source_address, length, pattern = arguments[:3]
        payload = memory.read(source_address, length) if source_address and length else b""
        matched = self.runtime.rtl_compare_memory_ulong(payload, pattern)
        trace.add(
            None,
            "runtime_memory_compare_api",
            shim_name="RtlCompareMemoryUlong",
            source_address=source_address,
            source_address_hex=_hex32(source_address),
            length=length,
            pattern=pattern,
            pattern_hex=_hex32(pattern),
            matched=matched,
        )
        return matched

    def _invoke_guest_hal_read_write_pci_space_api(
        self,
        arguments: tuple[int, ...],
        memory: SparseMemory,
        trace: ExecutionTrace,
    ) -> dict[str, Any]:
        (
            bus,
            slot,
            offset,
            buffer_address,
            length,
            write_flag,
        ) = arguments[:6]
        write = write_flag != 0
        payload = (
            memory.read(buffer_address, length)
            if write and buffer_address and length
            else None
        )
        data = self.runtime.hal_read_write_pci_space(
            bus,
            slot,
            offset,
            payload,
        )
        result: dict[str, Any] = {
            "status": XboxStatus.SUCCESS,
            "bus": bus,
            "slot": slot,
            "offset": offset,
            "buffer_address": buffer_address,
            "length": length,
            "write": write,
            "bytes_transferred": len(data),
            "data": data,
        }
        trace.add(
            None,
            "runtime_pci_space_api",
            shim_name="HalReadWritePCISpace",
            bus=bus,
            slot=slot,
            offset=offset,
            offset_hex=_hex32(offset),
            buffer_address=buffer_address,
            buffer_address_hex=_hex32(buffer_address),
            length=length,
            write=write,
            bytes_transferred=len(data),
        )
        return result

    def _apply_guest_side_effects(
        self,
        shim: RuntimeShim,
        arguments: tuple[int, ...],
        returned_value: Any,
        memory: SparseMemory,
        trace: ExecutionTrace,
    ) -> list[dict[str, Any]]:
        if not isinstance(returned_value, dict):
            return []
        writes: list[dict[str, Any]] = []
        if shim.name == "KeQuerySystemTime":
            output_address = returned_value.get("output_address")
            system_time = returned_value.get("system_time")
            if (
                isinstance(output_address, int)
                and output_address
                and isinstance(system_time, int)
            ):
                value = system_time & 0xFFFFFFFFFFFFFFFF
                memory.write(output_address, value.to_bytes(8, "little"))
                write = {
                    "shim_name": shim.name,
                    "label": "system_time",
                    "address": output_address,
                    "address_hex": _hex32(output_address),
                    "size": 8,
                    "value": value,
                    "value_hex": f"0x{value:016X}",
                }
                trace.add(
                    None,
                    "runtime_abi_memory_write",
                    shim_name=shim.name,
                    label="system_time",
                    memory_address=output_address,
                    memory_address_hex=_hex32(output_address),
                    size=8,
                    value=value,
                    value_hex=f"0x{value:016X}",
                )
                writes.append(write)
            return writes
        if shim.name == "PsCreateSystemThreadEx":
            handle = returned_value.get("handle")
            if isinstance(handle, int) and arguments and arguments[0]:
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "thread_handle",
                        arguments[0],
                        handle,
                    )
                )
            thread_id = returned_value.get("thread_id", handle)
            if isinstance(thread_id, int) and len(arguments) >= 5 and arguments[4]:
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "thread_id",
                        arguments[4],
                        thread_id,
                    )
                )
            return writes
        if shim.name == "NtCreateSemaphore":
            handle = returned_value.get("handle")
            handle_address = returned_value.get("handle_address")
            if isinstance(handle, int) and isinstance(handle_address, int) and handle_address:
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "semaphore_handle",
                        handle_address,
                        handle,
                    )
                )
            return writes
        if shim.name == "NtReleaseSemaphore" and len(arguments) >= 3:
            previous_count_address = arguments[2]
            previous_count = returned_value.get("previous_count")
            if (
                isinstance(previous_count_address, int)
                and previous_count_address
                and isinstance(previous_count, int)
            ):
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "semaphore_previous_count",
                        previous_count_address,
                        previous_count,
                    )
                )
            return writes
        if shim.name == "ExQueryNonVolatileSetting" and len(arguments) >= 5:
            setting_type = returned_value.get("setting_type")
            if isinstance(setting_type, int) and arguments[1]:
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "setting_type",
                        arguments[1],
                        setting_type,
                    )
                )
            required_length = returned_value.get("required_length")
            if isinstance(required_length, int) and arguments[4]:
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "result_length",
                        arguments[4],
                        required_length,
                    )
                )
            value = returned_value.get("value")
            if returned_value.get("status") == 0 and arguments[2] and arguments[3]:
                if isinstance(value, int) and arguments[3] >= 4:
                    writes.append(
                        _write_runtime_out_u32(
                            memory,
                            trace,
                            shim.name,
                            "setting_value",
                            arguments[2],
                            value,
                        )
                    )
                elif isinstance(value, bytes):
                    payload = value[: arguments[3]]
                    memory.write(arguments[2], payload)
                    writes.append(
                        {
                            "shim_name": shim.name,
                            "label": "setting_value",
                            "address": arguments[2],
                            "address_hex": _hex32(arguments[2]),
                            "size": len(payload),
                        }
                    )
            return writes
        if shim.name == "RtlInitAnsiString" and len(arguments) >= 2:
            destination_address = arguments[0]
            source_address = arguments[1]
            if destination_address:
                source_bytes = (
                    _read_guest_c_string(memory, source_address)
                    if source_address
                    else b""
                )
                length = min(len(source_bytes), 0xFFFF)
                maximum_length = min(length + 1 if source_address else 0, 0xFFFF)
                payload = (
                    length.to_bytes(2, "little")
                    + maximum_length.to_bytes(2, "little")
                    + _u32(source_address).to_bytes(4, "little")
                )
                memory.write(destination_address, payload)
                write = {
                    "shim_name": shim.name,
                    "label": "ansi_string",
                    "address": destination_address,
                    "address_hex": _hex32(destination_address),
                    "length": length,
                    "maximum_length": maximum_length,
                    "buffer": source_address,
                    "buffer_hex": _hex32(source_address),
                }
                trace.add(
                    None,
                    "runtime_abi_memory_write",
                    shim_name=shim.name,
                    label="ansi_string",
                    memory_address=destination_address,
                    memory_address_hex=_hex32(destination_address),
                    length=length,
                    maximum_length=maximum_length,
                    buffer=source_address,
                    buffer_hex=_hex32(source_address),
                )
                writes.append(write)
            return writes
        if shim.name == "HalReadWritePCISpace" and len(arguments) >= 6:
            status = returned_value.get("status")
            data = returned_value.get("data")
            buffer_address = arguments[3]
            length = arguments[4]
            write = returned_value.get("write") is True
            if (
                status == XboxStatus.SUCCESS
                and not write
                and isinstance(data, bytes)
                and buffer_address
                and length
            ):
                payload = data[:length]
                memory.write(buffer_address, payload)
                trace.add(
                    None,
                    "runtime_abi_memory_write",
                    shim_name=shim.name,
                    label="pci_read_buffer",
                    memory_address=buffer_address,
                    memory_address_hex=_hex32(buffer_address),
                    size=len(payload),
                )
                writes.append(
                    {
                        "shim_name": shim.name,
                        "label": "pci_read_buffer",
                        "address": buffer_address,
                        "address_hex": _hex32(buffer_address),
                        "size": len(payload),
                    }
                )
            return writes
        if shim.name == "NtAllocateVirtualMemory":
            status = returned_value.get("status")
            allocated_address = returned_value.get("allocated_address")
            allocated_size = returned_value.get("allocated_size")
            base_address_address = returned_value.get("base_address_address")
            region_size_address = returned_value.get("region_size_address")
            if (
                status == XboxStatus.SUCCESS
                and isinstance(allocated_address, int)
                and isinstance(base_address_address, int)
                and base_address_address
            ):
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "base_address",
                        base_address_address,
                        allocated_address,
                    )
                )
            if (
                status == XboxStatus.SUCCESS
                and isinstance(allocated_size, int)
                and isinstance(region_size_address, int)
                and region_size_address
            ):
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "region_size",
                        region_size_address,
                        allocated_size,
                    )
                )
            return writes
        if shim.name == "NtQueryInformationFile":
            status = returned_value.get("status")
            file_information_address = returned_value.get("file_information_address")
            requested_length = returned_value.get("requested_length", 0)
            payload = _guest_file_information_payload(returned_value, requested_length)
            if (
                returned_value.get("status") == XboxStatus.SUCCESS
                and isinstance(file_information_address, int)
                and file_information_address
                and payload
            ):
                memory.write(file_information_address, payload)
                write = {
                    "shim_name": shim.name,
                    "label": "file_information",
                    "address": file_information_address,
                    "address_hex": _hex32(file_information_address),
                    "size": len(payload),
                    "file_size": returned_value.get("size", 0),
                    "file_information_class": returned_value.get("file_information_class"),
                }
                trace.add(
                    None,
                    "runtime_abi_memory_write",
                    shim_name=shim.name,
                    label="file_information",
                    memory_address=file_information_address,
                    memory_address_hex=_hex32(file_information_address),
                    size=len(payload),
                    file_size=returned_value.get("size", 0),
                    file_information_class=returned_value.get("file_information_class"),
                )
                writes.append(write)
            io_status_block_address = returned_value.get(
                "io_status_block_address"
            )
            if (
                isinstance(status, int)
                and isinstance(io_status_block_address, int)
                and io_status_block_address
            ):
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "io_status",
                        io_status_block_address,
                        status,
                    )
                )
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "io_information",
                        _u32(io_status_block_address + 4),
                        len(payload) if returned_value.get("status") == XboxStatus.SUCCESS else 0,
                    )
                )
            return writes
        if shim.name == "NtQueryDirectoryFile":
            status = returned_value.get("status")
            record = returned_value.get("directory_record")
            file_information_address = returned_value.get("file_information_address")
            if (
                status == XboxStatus.SUCCESS
                and isinstance(record, bytes)
                and isinstance(file_information_address, int)
                and file_information_address
            ):
                memory.write(file_information_address, record)
                trace.add(
                    None,
                    "runtime_abi_memory_write",
                    shim_name=shim.name,
                    label="directory_information",
                    memory_address=file_information_address,
                    memory_address_hex=_hex32(file_information_address),
                    size=len(record),
                )
                writes.append(
                    {
                        "shim_name": shim.name,
                        "label": "directory_information",
                        "address": file_information_address,
                        "address_hex": _hex32(file_information_address),
                        "size": len(record),
                    }
                )
            io_status_block_address = returned_value.get("io_status_block_address")
            if isinstance(status, int) and isinstance(io_status_block_address, int) and io_status_block_address:
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "io_status",
                        io_status_block_address,
                        status,
                    )
                )
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "io_information",
                        _u32(io_status_block_address + 4),
                        len(record)
                        if status == XboxStatus.SUCCESS and isinstance(record, bytes)
                        else 0,
                    )
                )
            return writes
        if shim.name == "NtQueryVolumeInformationFile":
            status = returned_value.get("status")
            payload = returned_value.get("volume_information")
            file_information_address = returned_value.get("file_information_address")
            if (
                status == XboxStatus.SUCCESS
                and isinstance(payload, bytes)
                and isinstance(file_information_address, int)
                and file_information_address
            ):
                memory.write(file_information_address, payload)
                trace.add(
                    None,
                    "runtime_abi_memory_write",
                    shim_name=shim.name,
                    label="volume_information",
                    memory_address=file_information_address,
                    memory_address_hex=_hex32(file_information_address),
                    size=len(payload),
                )
                writes.append(
                    {
                        "shim_name": shim.name,
                        "label": "volume_information",
                        "address": file_information_address,
                        "address_hex": _hex32(file_information_address),
                        "size": len(payload),
                    }
                )
            io_status_block_address = returned_value.get("io_status_block_address")
            if (
                isinstance(status, int)
                and isinstance(io_status_block_address, int)
                and io_status_block_address
            ):
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "io_status",
                        io_status_block_address,
                        status,
                    )
                )
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "io_information",
                        _u32(io_status_block_address + 4),
                        len(payload)
                        if status == XboxStatus.SUCCESS and isinstance(payload, bytes)
                        else 0,
                    )
                )
            return writes
        if shim.name == "NtReadFile":
            status = returned_value.get("status")
            data = returned_value.get("data")
            buffer_address = returned_value.get("buffer_address")
            bytes_read = returned_value.get(
                "bytes_read", len(data) if isinstance(data, bytes) else 0
            )
            if (
                isinstance(data, bytes)
                and data
                and isinstance(buffer_address, int)
                and buffer_address
            ):
                payload = data[: bytes_read if isinstance(bytes_read, int) else len(data)]
                memory.write(buffer_address, payload)
                write = {
                    "shim_name": shim.name,
                    "label": "read_buffer",
                    "address": buffer_address,
                    "address_hex": _hex32(buffer_address),
                    "size": len(payload),
                }
                trace.add(
                    None,
                    "runtime_abi_memory_write",
                    shim_name=shim.name,
                    label="read_buffer",
                    memory_address=buffer_address,
                    memory_address_hex=_hex32(buffer_address),
                    size=len(payload),
                )
                writes.append(write)
            io_status_block_address = returned_value.get("io_status_block_address")
            if isinstance(status, int) and isinstance(io_status_block_address, int) and io_status_block_address:
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "io_status",
                        io_status_block_address,
                        status,
                    )
                )
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "io_information",
                        _u32(io_status_block_address + 4),
                        bytes_read if isinstance(bytes_read, int) else 0,
                    )
                )
            return writes
        if shim.name == "NtSetInformationFile":
            status = returned_value.get("status")
            bytes_consumed = returned_value.get("bytes_consumed", 0)
            io_status_block_address = returned_value.get("io_status_block_address")
            if isinstance(status, int) and isinstance(io_status_block_address, int) and io_status_block_address:
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "io_status",
                        io_status_block_address,
                        status,
                    )
                )
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "io_information",
                        _u32(io_status_block_address + 4),
                        bytes_consumed if isinstance(bytes_consumed, int) else 0,
                    )
                )
            return writes
        if shim.name == "NtWriteFile":
            status = returned_value.get("status")
            bytes_written = returned_value.get("bytes_written", 0)
            io_status_block_address = returned_value.get("io_status_block_address")
            if isinstance(status, int) and isinstance(io_status_block_address, int) and io_status_block_address:
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "io_status",
                        io_status_block_address,
                        status,
                    )
                )
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "io_information",
                        _u32(io_status_block_address + 4),
                        bytes_written if isinstance(bytes_written, int) else 0,
                    )
                )
            return writes
        if shim.name == "NtOpenSymbolicLinkObject":
            handle = returned_value.get("handle")
            link_handle_address = returned_value.get("link_handle_address")
            if isinstance(handle, int) and isinstance(link_handle_address, int) and link_handle_address:
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "symbolic_link_handle",
                        link_handle_address,
                        handle,
                    )
                )
            return writes
        if shim.name == "NtQuerySymbolicLinkObject":
            status = returned_value.get("status")
            target = returned_value.get("target")
            link_target_address = returned_value.get("link_target_address")
            written_length = 0
            if (
                status == XboxStatus.SUCCESS
                and isinstance(target, str)
                and isinstance(link_target_address, int)
                and link_target_address
            ):
                write = _write_guest_ansi_string_payload(
                    memory,
                    trace,
                    shim.name,
                    "symbolic_link_target",
                    link_target_address,
                    target.encode("ascii", errors="replace"),
                )
                if write is not None:
                    written_length = write["length"]
                    writes.append(write)
            returned_length_address = returned_value.get("returned_length_address")
            if isinstance(returned_length_address, int) and returned_length_address:
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "returned_length",
                        returned_length_address,
                        written_length,
                    )
                )
            return writes
        if shim.name in {"NtCreateFile", "NtOpenFile"}:
            status = returned_value.get("status")
            handle = returned_value.get("handle")
            file_handle_address = returned_value.get("file_handle_address")
            if isinstance(handle, int) and isinstance(file_handle_address, int) and file_handle_address:
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "file_handle",
                        file_handle_address,
                        handle,
                    )
                )
            io_status_block_address = returned_value.get("io_status_block_address")
            if isinstance(status, int) and isinstance(io_status_block_address, int) and io_status_block_address:
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "io_status",
                        io_status_block_address,
                        status,
                    )
                )
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "io_information",
                        _u32(io_status_block_address + 4),
                        0,
                    )
                )
        return writes


class XbeBackedSparseMemory(SparseMemory):
    """Sparse writable overlay that falls back to mapped XBE image bytes."""

    _FULL_PAGE_WRITTEN_MASK = b"\x01" * SparseMemory._PAGE_SIZE

    @staticmethod
    def _canonical_address(address: int) -> int:
        address = _u32(address)
        # The title's GPU allocator keeps NV2A-visible physical addresses in
        # the 0x20000000 range, but accesses allocation headers and locked
        # surface payloads through the corresponding bit-31 CPU alias.
        if 0xA0000000 <= address <= 0xBFFFFFFF:
            return address & 0x7FFFFFFF
        return address

    def __init__(
        self,
        loaded: LoadedXbeImage,
        initial: dict[int, bytes | int] | None = None,
        write_observer: Callable[[int, bytes], None] | None = None,
        *,
        enable_title_sentinel_fallbacks: bool = False,
    ) -> None:
        self._loaded = loaded
        self._write_observers = (
            [write_observer] if write_observer is not None else []
        )
        self._title_sentinel_fallbacks_enabled = enable_title_sentinel_fallbacks
        self._nv2a_status_busy_clear_count = 0
        self._title_gpu_completion_poll_count = 0
        self._title_gpu_completion_last_value: int | None = None
        self._title_gpu_completion_signal_count = 0
        self._title_gpu_completion_last_register_address: int | None = None
        self._title_gpu_interrupt_ack_count = 0
        self._title_gpu_interrupt_last_ack: int | None = None
        self._title_pfifo_interrupt_ack_count = 0
        self._title_pfifo_interrupt_last_ack: int | None = None
        self._title_pfifo_runout_status_read_count = 0
        self._title_pfifo_cache1_status_read_count = 0
        self._title_gpu_progress_counter_poll_count = 0
        self._title_gpu_progress_counter_last_value: int | None = None
        self._title_gpu_software_completion_clear_count = 0
        self._title_gpu_software_completion_last_value: int | None = None
        self._title_mcpx_frame_counter_read_count = 0
        self._title_mcpx_frame_counter_last_value: int | None = None
        self._title_audio_dsp_reset_count = 0
        self._title_audio_dsp_status_read_count = 0
        self._title_audio_dsp_status_last_value: int | None = None
        self._title_audio_dsp_voice_command_count = 0
        self._title_audio_dsp_voice_command_last_address: int | None = None
        self._title_d3d_context_seed_count = 0
        self._title_d3d_context_last_address: int | None = None
        self._title_d3d_context_list_seed_count = 0
        self._title_d3d_context_list_last_count: int | None = None
        self._title_d3d_state_descriptor_seed_count = 0
        self._title_d3d_state_descriptor_last_address: int | None = None
        self._title_d3d_state_descriptor_last_value: int | None = None
        self._title_d3d_get_pointer_sync_count = 0
        self._title_d3d_get_pointer_last_value: int | None = None
        self._title_gpu_submission_window_seed_count = 0
        self._title_gpu_submission_window_last_base: int | None = None
        self._title_gpu_submission_window_last_limit: int | None = None
        self._title_cleanup_list_seed_count = 0
        self._title_cleanup_list_repair_count = 0
        self._title_frontend_resource_cache_seed_count = 0
        self._title_frontend_resource_cache_repair_count = 0
        self._title_frontend_registry_list_seed_count = 0
        self._title_frontend_registry_list_repair_count = 0
        self._title_frontend_initializer_list_seed_count = 0
        self._title_frontend_initializer_list_repair_count = 0
        super().__init__(initial)
        if self._title_sentinel_fallbacks_enabled:
            self._seed_title_cleanup_list_sentinel()
            self._seed_title_frontend_resource_cache_sentinel()
            self._seed_title_frontend_registry_list_sentinel()
            self._seed_title_frontend_initializer_list_sentinel()

    def clone_for_speculative_execution(self) -> "XbeBackedSparseMemory":
        """Copy writable state without duplicating the immutable loaded XBE."""
        clone = object.__new__(type(self))
        clone.__dict__ = self.__dict__.copy()
        clone._pages = {
            page_number: bytearray(page)
            for page_number, page in self._pages.items()
        }
        clone._written_pages = {
            page_number: bytearray(mask)
            for page_number, mask in self._written_pages.items()
        }
        clone._page_generations = self._page_generations.copy()
        clone._changed_pages = self._changed_pages.copy()
        clone._write_observers = []
        return clone

    def add_write_observer(self, observer: Callable[[int, bytes], None]) -> None:
        self._write_observers.append(observer)

    def _observe_write(self, address: int, payload: bytes) -> None:
        for observer in tuple(self._write_observers):
            observer(address, payload)

    def read(self, address: int, size: int) -> bytes:
        if size < 0:
            raise X86ExecutionError("cannot read a negative size")
        if self._title_sentinel_fallbacks_enabled:
            self._apply_title_cleanup_list_read(address, size)
            self._apply_title_frontend_resource_cache_read(address, size)
            self._apply_title_frontend_registry_list_read(address, size)
            self._apply_title_frontend_initializer_list_read(address, size)
        self._apply_title_d3d_context_read(address, size)
        self._apply_title_d3d_get_pointer_read(address, size)
        self._apply_title_gpu_submission_window_read(address, size)
        self._apply_title_gpu_completion_poll(address, size)
        self._apply_title_pfifo_idle_status_read(address, size)
        self._apply_title_gpu_progress_counter_read(address, size)
        self._apply_title_gpu_software_completion_read(address, size)
        self._apply_title_mcpx_frame_counter_read(address, size)
        self._apply_title_audio_dsp_status_read(address, size)
        if size >= 64:
            payload = bytearray(size)
            cursor = 0
            while cursor < size:
                current = self._canonical_address(address + cursor)
                page_address = current & ~self._PAGE_MASK
                page_offset = current & self._PAGE_MASK
                chunk_size = min(size - cursor, self._PAGE_SIZE - page_offset)
                page = self.native_page_snapshot(page_address)
                payload[cursor : cursor + chunk_size] = page[
                    page_offset : page_offset + chunk_size
                ]
                cursor += chunk_size
            return bytes(payload)
        payload = bytearray()
        for offset in range(size):
            current = self._canonical_address(address + offset)
            if self._has_byte(current):
                payload.append(self._read_byte(current))
                continue
            try:
                payload.extend(self._loaded.arena.read(current, 1))
            except XbeMemoryAccessError:
                payload.append(0)
        return bytes(payload)

    def read_u32(self, address: int) -> int:
        """Read a fully overlaid word directly when it has no dynamic semantics."""
        current = self._canonical_address(address)
        page_offset = current & self._PAGE_MASK
        if (
            not self._title_sentinel_fallbacks_enabled
            and address not in TITLE_DYNAMIC_U32_READ_ADDRESSES
            and current not in TITLE_DYNAMIC_U32_READ_ADDRESSES
            and page_offset != TITLE_GPU_COMPLETION_DMA_STATUS_OFFSET
            and page_offset <= self._PAGE_SIZE - 4
        ):
            page_number = current >> self._PAGE_BITS
            page = self._pages.get(page_number)
            written = self._written_pages.get(page_number)
            if (
                page is not None
                and written is not None
                and written[page_offset]
                and written[page_offset + 1]
                and written[page_offset + 2]
                and written[page_offset + 3]
            ):
                return struct.unpack_from("<I", page, page_offset)[0]
        return struct.unpack("<I", self.read(address, 4))[0]

    def native_page_snapshot(self, page_address: int) -> bytes:
        """Materialize one cache page without per-byte arena lookups."""
        page_address = self._canonical_address(page_address) & ~self._PAGE_MASK
        page_end = page_address + self._PAGE_SIZE
        payload = bytearray(self._PAGE_SIZE)
        for region in self._loaded.arena.regions:
            start = max(page_address, region.virtual_address)
            end = min(page_end, region.virtual_end)
            if start >= end:
                continue
            payload[start - page_address : end - page_address] = (
                self._loaded.arena.read(start, end - start)
            )

        page_number = page_address >> self._PAGE_BITS
        overlay = self._pages.get(page_number)
        written = self._written_pages.get(page_number)
        if overlay is None or written is None:
            return bytes(payload)
        cursor = 0
        while cursor < self._PAGE_SIZE:
            start = written.find(1, cursor)
            if start < 0:
                break
            end = written.find(0, start)
            if end < 0:
                end = self._PAGE_SIZE
            payload[start:end] = overlay[start:end]
            cursor = end
        return bytes(payload)

    def write(self, address: int, payload: bytes) -> None:
        self._observe_write(address, payload)
        if self._apply_title_mmio_write(address, payload):
            return
        super().write(self._canonical_address(address), payload)

    def write_native_page_range(self, address: int, payload: bytes) -> None:
        """Commit a non-MMIO native cache range without reclassifying it as host work."""
        current = self._canonical_address(address)
        page_offset = current & self._PAGE_MASK
        if not payload:
            return
        if page_offset + len(payload) > self._PAGE_SIZE:
            raise X86ExecutionError("native page writeback crossed a cache-page boundary")
        self._observe_write(address, payload)
        page_number = current >> self._PAGE_BITS
        page = self._pages.setdefault(page_number, bytearray(self._PAGE_SIZE))
        written = self._written_pages.setdefault(
            page_number, bytearray(self._PAGE_SIZE)
        )
        end = page_offset + len(payload)
        page[page_offset:end] = payload
        written[page_offset:end] = self._FULL_PAGE_WRITTEN_MASK[: len(payload)]
        self._page_generations[page_number] = (
            self._page_generations.get(page_number, 0) + 1
        )

    def write_u32(self, address: int, value: int) -> None:
        payload = struct.pack("<I", _u32(value))
        self._observe_write(address, payload)
        if self._apply_title_mmio_write(address, payload):
            return
        super().write_u32(self._canonical_address(address), value)

    def _apply_title_mmio_write(self, address: int, payload: bytes) -> bool:
        if address in TITLE_AUDIO_DSP_VOICE_COMMAND_ADDRESSES and payload:
            # MCPX voice command registers acknowledge requests by clearing
            # the pending bit. The title polls that same byte before issuing
            # the next configuration command.
            value = payload[0]
            if value & TITLE_AUDIO_DSP_VOICE_COMMAND_PENDING_BIT:
                value &= ~TITLE_AUDIO_DSP_VOICE_COMMAND_PENDING_BIT
                self._title_audio_dsp_voice_command_count += 1
                self._title_audio_dsp_voice_command_last_address = address
            super().write(address, bytes([value]) + payload[1:])
            return True
        if len(payload) < 4:
            return False
        value = struct.unpack("<I", payload[:4])[0]
        if address == NV2A_STATUS_POLL_ADDRESS and value & NV2A_STATUS_POLL_BUSY_BIT:
            cleared = value & ~NV2A_STATUS_POLL_BUSY_BIT
            self._nv2a_status_busy_clear_count += 1
            super().write(address, struct.pack("<I", cleared))
            return True
        if address == TITLE_GPU_INTERRUPT_STATUS_ADDRESS:
            if value:
                self._title_gpu_interrupt_ack_count += 1
                self._title_gpu_interrupt_last_ack = value
            self._write_title_mmio_status_ack(address, value)
            return True
        if address == TITLE_GPU_PFIFO_INTERRUPT_STATUS_ADDRESS:
            if value:
                self._title_pfifo_interrupt_ack_count += 1
                self._title_pfifo_interrupt_last_ack = value
            self._write_title_mmio_status_ack(address, value)
            return True
        if address == TITLE_AUDIO_DSP_CONTROL_ADDRESS:
            super().write(address, struct.pack("<I", value))
            if value & TITLE_AUDIO_DSP_RESET_REQUEST_BIT:
                status = self._read_overlay_u32(TITLE_AUDIO_DSP_STATUS_ADDRESS)
                status |= TITLE_AUDIO_DSP_RESET_READY_BIT
                super().write(
                    TITLE_AUDIO_DSP_STATUS_ADDRESS,
                    struct.pack("<I", status),
                )
                self._title_audio_dsp_reset_count += 1
            return True
        if address == TITLE_GPU_COMMAND_KICK_ADDRESS:
            # The boot D3D path submits a marker through this CPU alias and
            # waits for the NV2A DMA state block to report the current push
            # address. The state block is allocated dynamically, so publish
            # completion when the command is submitted instead of relying on
            # one fixed status-register address.
            super().write(self._canonical_address(address), payload)
            dma_state = self._read_overlay_or_image_u32(
                TITLE_GPU_SUBMISSION_BASE_ADDRESS
                + TITLE_GPU_COMPLETION_DMA_POINTER_OFFSET
            )
            submitted = self._read_overlay_or_image_u32(
                TITLE_GPU_SUBMISSION_BASE_ADDRESS
            )
            if dma_state and submitted:
                completion_address = self._canonical_address(
                    dma_state + TITLE_GPU_COMPLETION_DMA_STATUS_OFFSET
                )
                self._publish_title_gpu_completion(
                    completion_address,
                    submitted,
                    signal=True,
                )
            return True

        return False

    def _write_title_mmio_status_ack(self, address: int, value: int) -> None:
        current = self._read_overlay_u32(address)
        cleared = current & ~value
        super().write(address, struct.pack("<I", cleared))

    def _apply_title_gpu_completion_poll(self, address: int, size: int) -> None:
        if size < 4:
            return
        canonical_address = self._canonical_address(address)
        if canonical_address != TITLE_GPU_COMPLETION_REGISTER_ADDRESS:
            if canonical_address & self._PAGE_MASK != TITLE_GPU_COMPLETION_DMA_STATUS_OFFSET:
                return
            dma_state = self._read_overlay_or_image_u32(
                TITLE_GPU_SUBMISSION_BASE_ADDRESS
                + TITLE_GPU_COMPLETION_DMA_POINTER_OFFSET
            )
            if not dma_state or canonical_address != self._canonical_address(
                dma_state + TITLE_GPU_COMPLETION_DMA_STATUS_OFFSET
            ):
                return
        submitted = self._read_overlay_or_image_u32(TITLE_GPU_SUBMISSION_BASE_ADDRESS)
        if submitted == 0:
            return
        self._title_gpu_completion_poll_count += 1
        self._publish_title_gpu_completion(canonical_address, submitted)

    def _publish_title_gpu_completion(
        self,
        address: int,
        submitted: int,
        *,
        signal: bool = False,
    ) -> None:
        completed = submitted & TITLE_GPU_COMPLETION_MASK
        self._title_gpu_completion_last_value = completed
        self._title_gpu_completion_last_register_address = address
        if signal:
            self._title_gpu_completion_signal_count += 1
        super().write(address, struct.pack("<I", completed))

    def _apply_title_pfifo_idle_status_read(self, address: int, size: int) -> None:
        if size < 1:
            return
        if address == TITLE_GPU_PFIFO_RUNOUT_STATUS_ADDRESS:
            self._title_pfifo_runout_status_read_count += 1
            super().write(address, struct.pack("<I", TITLE_GPU_PFIFO_IDLE_BIT))
        elif address == TITLE_GPU_PFIFO_CACHE1_STATUS_ADDRESS:
            self._title_pfifo_cache1_status_read_count += 1
            super().write(address, struct.pack("<I", TITLE_GPU_PFIFO_IDLE_BIT))

    def _apply_title_gpu_progress_counter_read(self, address: int, size: int) -> None:
        if address != TITLE_GPU_PROGRESS_COUNTER_ADDRESS or size < 4:
            return
        current = self._read_overlay_u32(address)
        if current == 0:
            return
        advanced = _u32(current + 1)
        self._title_gpu_progress_counter_poll_count += 1
        self._title_gpu_progress_counter_last_value = advanced
        super().write(address, struct.pack("<I", advanced))

    def _apply_title_gpu_software_completion_read(
        self,
        address: int,
        size: int,
    ) -> None:
        if address != TITLE_GPU_SOFTWARE_COMPLETION_FLAG_ADDRESS or size < 4:
            return
        current = self._read_overlay_or_image_u32(address)
        if not current & TITLE_GPU_SOFTWARE_COMPLETION_PENDING_BIT:
            return
        cleared = current & ~TITLE_GPU_SOFTWARE_COMPLETION_PENDING_BIT
        self._title_gpu_software_completion_clear_count += 1
        self._title_gpu_software_completion_last_value = cleared
        super().write(address, struct.pack("<I", cleared))

    def _apply_title_mcpx_frame_counter_read(self, address: int, size: int) -> None:
        if address != TITLE_MCPX_FRAME_COUNTER_ADDRESS or size < 1:
            return
        value = _u32(
            self._read_overlay_u32(address) + TITLE_MCPX_FRAME_COUNTER_INCREMENT
        )
        self._title_mcpx_frame_counter_read_count += 1
        self._title_mcpx_frame_counter_last_value = value
        super().write(address, struct.pack("<I", value))

    def _apply_title_audio_dsp_status_read(self, address: int, size: int) -> None:
        if address != TITLE_AUDIO_DSP_STATUS_ADDRESS or size < 4:
            return
        status = self._read_overlay_u32(address)
        control = self._read_overlay_u32(TITLE_AUDIO_DSP_CONTROL_ADDRESS)
        if control & TITLE_AUDIO_DSP_RESET_REQUEST_BIT:
            status |= TITLE_AUDIO_DSP_RESET_READY_BIT
        self._title_audio_dsp_status_read_count += 1
        self._title_audio_dsp_status_last_value = status
        super().write(address, struct.pack("<I", status))

    def _apply_title_cleanup_list_read(self, address: int, size: int) -> None:
        sentinel = TITLE_CLEANUP_LIST_SENTINEL_ADDRESS
        if size < 4 or address not in {sentinel, _u32(sentinel + 4)}:
            return
        current_next = self._read_overlay_u32(sentinel)
        current_previous = self._read_overlay_u32(_u32(sentinel + 4))
        if current_next and current_previous:
            return
        self._write_overlay_u32(sentinel, sentinel)
        self._write_overlay_u32(_u32(sentinel + 4), sentinel)
        self._title_cleanup_list_repair_count += 1

    def _apply_title_frontend_resource_cache_read(
        self,
        address: int,
        size: int,
    ) -> None:
        sentinel = TITLE_FRONTEND_RESOURCE_CACHE_SENTINEL_ADDRESS
        if size < 4 or address != sentinel:
            return
        if self._read_overlay_u32(sentinel):
            return
        self._write_overlay_u32(sentinel, sentinel)
        self._title_frontend_resource_cache_repair_count += 1

    def _apply_title_frontend_registry_list_read(
        self,
        address: int,
        size: int,
    ) -> None:
        sentinel = TITLE_FRONTEND_REGISTRY_LIST_SENTINEL_ADDRESS
        if size < 4 or address not in {sentinel, _u32(sentinel + 4)}:
            return
        current_next = self._read_overlay_u32(sentinel)
        current_previous = self._read_overlay_u32(_u32(sentinel + 4))
        if current_next and current_previous:
            return
        self._write_overlay_u32(sentinel, sentinel)
        self._write_overlay_u32(_u32(sentinel + 4), sentinel)
        self._title_frontend_registry_list_repair_count += 1

    def _apply_title_frontend_initializer_list_read(
        self,
        address: int,
        size: int,
    ) -> None:
        sentinel = TITLE_FRONTEND_INITIALIZER_LIST_SENTINEL_ADDRESS
        if size < 4 or address != sentinel:
            return
        current_next = self._read_overlay_u32(sentinel)
        if current_next and current_next % 4 == 0:
            return
        self._write_overlay_u32(sentinel, sentinel)
        self._title_frontend_initializer_list_repair_count += 1

    def _apply_title_gpu_submission_window_read(
        self,
        address: int,
        size: int,
    ) -> None:
        if size < 4 or address not in {
            TITLE_GPU_SUBMISSION_BASE_ADDRESS,
            TITLE_GPU_SUBMISSION_LIMIT_ADDRESS,
        }:
            return
        current_base = self._read_overlay_or_image_u32(TITLE_GPU_SUBMISSION_BASE_ADDRESS)
        current_limit = self._read_overlay_or_image_u32(
            TITLE_GPU_SUBMISSION_LIMIT_ADDRESS
        )
        if current_base and current_limit:
            return
        self._seed_title_gpu_submission_window(
            TITLE_D3D_PUSH_BUFFER_BASE_ADDRESS,
            TITLE_D3D_PUSH_BUFFER_END_ADDRESS,
        )

    def _seed_title_gpu_submission_window(self, base_address: int, limit_address: int) -> None:
        self._write_overlay_u32(TITLE_GPU_SUBMISSION_BASE_ADDRESS, base_address)
        self._write_overlay_u32(TITLE_GPU_SUBMISSION_LIMIT_ADDRESS, limit_address)
        self._title_gpu_submission_window_seed_count += 1
        self._title_gpu_submission_window_last_base = base_address
        self._title_gpu_submission_window_last_limit = limit_address

    def _apply_title_d3d_context_read(self, address: int, size: int) -> None:
        if address != TITLE_D3D_CONTEXT_GLOBAL_ADDRESS or size < 4:
            return
        current = self._read_overlay_or_image_u32(address)
        if current != 0:
            return
        self._seed_title_d3d_context()

    def _seed_title_d3d_context(self) -> None:
        context = TITLE_D3D_CONTEXT_SYNTHETIC_ADDRESS
        fields = {
            0x0000: TITLE_D3D_PUSH_BUFFER_BASE_ADDRESS,
            0x0004: TITLE_D3D_PUSH_BUFFER_END_ADDRESS,
            0x0008: 0,
            0x0024: TITLE_D3D_PUSH_BUFFER_BASE_ADDRESS,
            0x0028: TITLE_D3D_PUSH_BUFFER_END_ADDRESS,
            0x002C: 0,
            0x0030: TITLE_D3D_CONTEXT_GET_POINTER_ADDRESS,
            0x0034: 0,
            0x0038: 0x3F,
            0x003C: 0,
            0x0040: TITLE_D3D_PUSH_BUFFER_SIZE,
            0x0048: TITLE_D3D_CONTEXT_MARKER_QUEUE_ADDRESS,
            0x051C: TITLE_D3D_CONTEXT_SURFACE_STATE_ADDRESS,
            0x15DC: 0,
            0x15E0: 0,
            TITLE_D3D_CONTEXT_LIST_COUNT_OFFSET: TITLE_D3D_CONTEXT_LIST_SEEDED_COUNT,
            TITLE_D3D_CONTEXT_LIST_FIRST_OFFSET: TITLE_D3D_CONTEXT_LIST_NODE0_ADDRESS,
            TITLE_D3D_CONTEXT_LIST_SECOND_OFFSET: TITLE_D3D_CONTEXT_LIST_NODE1_ADDRESS,
            0x17F4: TITLE_D3D_CONTEXT_DMA_STATE_ADDRESS,
            0x17F8: 0,
            0x19A0: 0,
        }
        self._write_overlay_u32(TITLE_D3D_CONTEXT_GLOBAL_ADDRESS, context)
        for offset, value in fields.items():
            self._write_overlay_u32(_u32(context + offset), value)
        self._write_overlay_u32(TITLE_D3D_CONTEXT_GET_POINTER_ADDRESS, 0)
        self._write_overlay_u32(_u32(TITLE_D3D_CONTEXT_DMA_STATE_ADDRESS + 0x40), 0)
        self._write_overlay_u32(_u32(TITLE_D3D_CONTEXT_DMA_STATE_ADDRESS + 0x44), 0)
        self._write_overlay_u32(
            _u32(TITLE_D3D_CONTEXT_SURFACE_STATE_ADDRESS + 0x324C),
            0,
        )
        self._seed_title_d3d_context_list_nodes()
        self._seed_title_d3d_state_descriptor()
        if (
            not self._read_overlay_or_image_u32(TITLE_GPU_SUBMISSION_BASE_ADDRESS)
            or not self._read_overlay_or_image_u32(TITLE_GPU_SUBMISSION_LIMIT_ADDRESS)
        ):
            self._seed_title_gpu_submission_window(
                TITLE_D3D_PUSH_BUFFER_BASE_ADDRESS,
                TITLE_D3D_PUSH_BUFFER_END_ADDRESS,
            )
        self._title_d3d_context_seed_count += 1
        self._title_d3d_context_last_address = context

    def _seed_title_d3d_context_list_nodes(self) -> None:
        for node_address in (
            TITLE_D3D_CONTEXT_LIST_NODE0_ADDRESS,
            TITLE_D3D_CONTEXT_LIST_NODE1_ADDRESS,
        ):
            self._write_overlay_u32(_u32(node_address + 0x04), 0)
            self._write_overlay_u32(_u32(node_address + 0x08), 0)
        self._title_d3d_context_list_seed_count += 1
        self._title_d3d_context_list_last_count = TITLE_D3D_CONTEXT_LIST_SEEDED_COUNT

    def _seed_title_d3d_state_descriptor(self) -> None:
        descriptor_address = _u32(
            TITLE_D3D_STATE_DESCRIPTOR_BASE_ADDRESS
            + TITLE_D3D_STATE_DESCRIPTOR_OBSERVED_INDEX
            * TITLE_D3D_STATE_DESCRIPTOR_STRIDE
        )
        switch_address = _u32(
            descriptor_address + TITLE_D3D_STATE_DESCRIPTOR_SWITCH_OFFSET
        )
        if self._read_overlay_or_image_u32(switch_address) != 0:
            return
        self._write_overlay_u32(switch_address, TITLE_D3D_STATE_DESCRIPTOR_SWITCH_VALUE)
        self._title_d3d_state_descriptor_seed_count += 1
        self._title_d3d_state_descriptor_last_address = switch_address
        self._title_d3d_state_descriptor_last_value = (
            TITLE_D3D_STATE_DESCRIPTOR_SWITCH_VALUE
        )

    def _apply_title_d3d_get_pointer_read(self, address: int, size: int) -> None:
        if address != TITLE_D3D_CONTEXT_GET_POINTER_ADDRESS or size < 4:
            return
        context = self._read_overlay_u32(TITLE_D3D_CONTEXT_GLOBAL_ADDRESS)
        if context != TITLE_D3D_CONTEXT_SYNTHETIC_ADDRESS:
            return
        put_pointer = self._read_overlay_u32(_u32(context + 0x2C))
        current = self._read_overlay_u32(address)
        if current == put_pointer:
            return
        self._write_overlay_u32(address, put_pointer)
        self._title_d3d_get_pointer_sync_count += 1
        self._title_d3d_get_pointer_last_value = put_pointer

    def _read_overlay_or_image_u32(self, address: int) -> int:
        payload = bytearray()
        for offset in range(4):
            current = _u32(address + offset)
            if self._has_byte(current):
                payload.append(self._read_byte(current))
                continue
            try:
                payload.extend(self._loaded.arena.read(current, 1))
            except XbeMemoryAccessError:
                payload.append(0)
        return struct.unpack("<I", bytes(payload))[0]

    def _read_overlay_u32(self, address: int) -> int:
        payload = bytes(self._read_byte(_u32(address + offset)) for offset in range(4))
        return struct.unpack("<I", payload)[0]

    def _write_overlay_u32(self, address: int, value: int) -> None:
        super().write(address, struct.pack("<I", _u32(value)))

    def _seed_title_cleanup_list_sentinel(self) -> None:
        sentinel = TITLE_CLEANUP_LIST_SENTINEL_ADDRESS
        current_next = self._read_overlay_u32(sentinel)
        current_previous = self._read_overlay_u32(_u32(sentinel + 4))
        if current_next or current_previous:
            return
        self._write_overlay_u32(sentinel, sentinel)
        self._write_overlay_u32(_u32(sentinel + 4), sentinel)
        self._title_cleanup_list_seed_count += 1

    def _seed_title_frontend_resource_cache_sentinel(self) -> None:
        sentinel = TITLE_FRONTEND_RESOURCE_CACHE_SENTINEL_ADDRESS
        if self._read_overlay_u32(sentinel):
            return
        self._write_overlay_u32(sentinel, sentinel)
        self._title_frontend_resource_cache_seed_count += 1

    def _seed_title_frontend_registry_list_sentinel(self) -> None:
        sentinel = TITLE_FRONTEND_REGISTRY_LIST_SENTINEL_ADDRESS
        current_next = self._read_overlay_u32(sentinel)
        current_previous = self._read_overlay_u32(_u32(sentinel + 4))
        if current_next or current_previous:
            return
        self._write_overlay_u32(sentinel, sentinel)
        self._write_overlay_u32(_u32(sentinel + 4), sentinel)
        self._title_frontend_registry_list_seed_count += 1

    def _seed_title_frontend_initializer_list_sentinel(self) -> None:
        sentinel = TITLE_FRONTEND_INITIALIZER_LIST_SENTINEL_ADDRESS
        if self._read_overlay_u32(sentinel):
            return
        self._write_overlay_u32(sentinel, sentinel)
        self._title_frontend_initializer_list_seed_count += 1

    def title_hardware_completion_summary(self) -> dict[str, Any]:
        return {
            "sparse_overlay": self.storage_summary(),
            "nv2a_status_poll_address_hex": _hex32(NV2A_STATUS_POLL_ADDRESS),
            "nv2a_status_busy_clear_count": self._nv2a_status_busy_clear_count,
            "title_cleanup_list_sentinel_address_hex": _hex32(
                TITLE_CLEANUP_LIST_SENTINEL_ADDRESS
            ),
            "title_sentinel_fallbacks_enabled": (
                self._title_sentinel_fallbacks_enabled
            ),
            "title_cleanup_list_seed_count": self._title_cleanup_list_seed_count,
            "title_cleanup_list_repair_count": self._title_cleanup_list_repair_count,
            "title_cleanup_list_next_hex": _hex32(
                self._read_overlay_u32(TITLE_CLEANUP_LIST_SENTINEL_ADDRESS)
            ),
            "title_cleanup_list_previous_hex": _hex32(
                self._read_overlay_u32(
                    _u32(TITLE_CLEANUP_LIST_SENTINEL_ADDRESS + 4)
                )
            ),
            "title_frontend_resource_cache_sentinel_address_hex": _hex32(
                TITLE_FRONTEND_RESOURCE_CACHE_SENTINEL_ADDRESS
            ),
            "title_frontend_resource_cache_seed_count": (
                self._title_frontend_resource_cache_seed_count
            ),
            "title_frontend_resource_cache_repair_count": (
                self._title_frontend_resource_cache_repair_count
            ),
            "title_frontend_resource_cache_next_hex": _hex32(
                self._read_overlay_u32(
                    TITLE_FRONTEND_RESOURCE_CACHE_SENTINEL_ADDRESS
                )
            ),
            "title_frontend_registry_list_sentinel_address_hex": _hex32(
                TITLE_FRONTEND_REGISTRY_LIST_SENTINEL_ADDRESS
            ),
            "title_frontend_registry_list_seed_count": (
                self._title_frontend_registry_list_seed_count
            ),
            "title_frontend_registry_list_repair_count": (
                self._title_frontend_registry_list_repair_count
            ),
            "title_frontend_registry_list_next_hex": _hex32(
                self._read_overlay_u32(
                    TITLE_FRONTEND_REGISTRY_LIST_SENTINEL_ADDRESS
                )
            ),
            "title_frontend_registry_list_previous_hex": _hex32(
                self._read_overlay_u32(
                    _u32(TITLE_FRONTEND_REGISTRY_LIST_SENTINEL_ADDRESS + 4)
                )
            ),
            "title_frontend_initializer_list_sentinel_address_hex": _hex32(
                TITLE_FRONTEND_INITIALIZER_LIST_SENTINEL_ADDRESS
            ),
            "title_frontend_initializer_list_seed_count": (
                self._title_frontend_initializer_list_seed_count
            ),
            "title_frontend_initializer_list_repair_count": (
                self._title_frontend_initializer_list_repair_count
            ),
            "title_frontend_initializer_list_next_hex": _hex32(
                self._read_overlay_u32(
                    TITLE_FRONTEND_INITIALIZER_LIST_SENTINEL_ADDRESS
                )
            ),
            "d3d_context_global_address_hex": _hex32(
                TITLE_D3D_CONTEXT_GLOBAL_ADDRESS
            ),
            "d3d_context_synthetic_address_hex": _hex32(
                TITLE_D3D_CONTEXT_SYNTHETIC_ADDRESS
            ),
            "d3d_context_seed_count": self._title_d3d_context_seed_count,
            "d3d_context_last_address_hex": _hex32(
                self._title_d3d_context_last_address or 0
            )
            if self._title_d3d_context_last_address is not None
            else None,
            "d3d_context_list_count_offset_hex": _hex32(
                TITLE_D3D_CONTEXT_LIST_COUNT_OFFSET
            ),
            "d3d_context_list_first_offset_hex": _hex32(
                TITLE_D3D_CONTEXT_LIST_FIRST_OFFSET
            ),
            "d3d_context_list_second_offset_hex": _hex32(
                TITLE_D3D_CONTEXT_LIST_SECOND_OFFSET
            ),
            "d3d_context_list_seed_count": self._title_d3d_context_list_seed_count,
            "d3d_context_list_last_count": self._title_d3d_context_list_last_count,
            "d3d_context_list_node0_address_hex": _hex32(
                TITLE_D3D_CONTEXT_LIST_NODE0_ADDRESS
            ),
            "d3d_context_list_node1_address_hex": _hex32(
                TITLE_D3D_CONTEXT_LIST_NODE1_ADDRESS
            ),
            "d3d_state_descriptor_base_address_hex": _hex32(
                TITLE_D3D_STATE_DESCRIPTOR_BASE_ADDRESS
            ),
            "d3d_state_descriptor_stride": TITLE_D3D_STATE_DESCRIPTOR_STRIDE,
            "d3d_state_descriptor_observed_index": (
                TITLE_D3D_STATE_DESCRIPTOR_OBSERVED_INDEX
            ),
            "d3d_state_descriptor_switch_offset_hex": _hex32(
                TITLE_D3D_STATE_DESCRIPTOR_SWITCH_OFFSET
            ),
            "d3d_state_descriptor_seed_count": (
                self._title_d3d_state_descriptor_seed_count
            ),
            "d3d_state_descriptor_last_address_hex": _hex32(
                self._title_d3d_state_descriptor_last_address or 0
            )
            if self._title_d3d_state_descriptor_last_address is not None
            else None,
            "d3d_state_descriptor_last_value_hex": _hex32(
                self._title_d3d_state_descriptor_last_value or 0
            )
            if self._title_d3d_state_descriptor_last_value is not None
            else None,
            "d3d_push_buffer_base_address_hex": _hex32(
                TITLE_D3D_PUSH_BUFFER_BASE_ADDRESS
            ),
            "d3d_push_buffer_end_address_hex": _hex32(
                TITLE_D3D_PUSH_BUFFER_END_ADDRESS
            ),
            "d3d_get_pointer_address_hex": _hex32(
                TITLE_D3D_CONTEXT_GET_POINTER_ADDRESS
            ),
            "d3d_get_pointer_sync_count": self._title_d3d_get_pointer_sync_count,
            "d3d_get_pointer_last_value_hex": _hex32(
                self._title_d3d_get_pointer_last_value or 0
            )
            if self._title_d3d_get_pointer_last_value is not None
            else None,
            "d3d_get_pointer_current_hex": _hex32(
                self._read_overlay_u32(TITLE_D3D_CONTEXT_GET_POINTER_ADDRESS)
            ),
            "gpu_submission_base_address_hex": _hex32(
                TITLE_GPU_SUBMISSION_BASE_ADDRESS
            ),
            "gpu_submission_limit_address_hex": _hex32(
                TITLE_GPU_SUBMISSION_LIMIT_ADDRESS
            ),
            "gpu_submission_window_seed_count": (
                self._title_gpu_submission_window_seed_count
            ),
            "gpu_submission_window_last_base_hex": _hex32(
                self._title_gpu_submission_window_last_base or 0
            )
            if self._title_gpu_submission_window_last_base is not None
            else None,
            "gpu_submission_window_last_limit_hex": _hex32(
                self._title_gpu_submission_window_last_limit or 0
            )
            if self._title_gpu_submission_window_last_limit is not None
            else None,
            "gpu_submission_current_base_hex": _hex32(
                self._read_overlay_u32(TITLE_GPU_SUBMISSION_BASE_ADDRESS)
            ),
            "gpu_submission_current_limit_hex": _hex32(
                self._read_overlay_u32(TITLE_GPU_SUBMISSION_LIMIT_ADDRESS)
            ),
            "gpu_completion_register_address_hex": _hex32(
                TITLE_GPU_COMPLETION_REGISTER_ADDRESS
            ),
            "gpu_completion_mask_hex": _hex32(TITLE_GPU_COMPLETION_MASK),
            "gpu_completion_poll_count": self._title_gpu_completion_poll_count,
            "gpu_completion_signal_count": self._title_gpu_completion_signal_count,
            "gpu_completion_last_register_address_hex": _hex32(
                self._title_gpu_completion_last_register_address or 0
            )
            if self._title_gpu_completion_last_register_address is not None
            else None,
            "gpu_completion_last_value_hex": _hex32(
                self._title_gpu_completion_last_value or 0
            )
            if self._title_gpu_completion_last_value is not None
            else None,
            "gpu_interrupt_status_address_hex": _hex32(
                TITLE_GPU_INTERRUPT_STATUS_ADDRESS
            ),
            "gpu_interrupt_ack_count": self._title_gpu_interrupt_ack_count,
            "gpu_interrupt_last_ack_hex": _hex32(
                self._title_gpu_interrupt_last_ack or 0
            )
            if self._title_gpu_interrupt_last_ack is not None
            else None,
            "gpu_interrupt_status_current_hex": _hex32(
                self._read_overlay_u32(TITLE_GPU_INTERRUPT_STATUS_ADDRESS)
            ),
            "pfifo_interrupt_status_address_hex": _hex32(
                TITLE_GPU_PFIFO_INTERRUPT_STATUS_ADDRESS
            ),
            "pfifo_interrupt_ack_count": self._title_pfifo_interrupt_ack_count,
            "pfifo_interrupt_last_ack_hex": _hex32(
                self._title_pfifo_interrupt_last_ack or 0
            )
            if self._title_pfifo_interrupt_last_ack is not None
            else None,
            "pfifo_interrupt_status_current_hex": _hex32(
                self._read_overlay_u32(TITLE_GPU_PFIFO_INTERRUPT_STATUS_ADDRESS)
            ),
            "pfifo_runout_status_address_hex": _hex32(
                TITLE_GPU_PFIFO_RUNOUT_STATUS_ADDRESS
            ),
            "pfifo_runout_status_read_count": (
                self._title_pfifo_runout_status_read_count
            ),
            "pfifo_runout_status_current_hex": _hex32(
                self._read_overlay_u32(TITLE_GPU_PFIFO_RUNOUT_STATUS_ADDRESS)
            ),
            "pfifo_cache1_status_address_hex": _hex32(
                TITLE_GPU_PFIFO_CACHE1_STATUS_ADDRESS
            ),
            "pfifo_cache1_status_read_count": (
                self._title_pfifo_cache1_status_read_count
            ),
            "pfifo_cache1_status_current_hex": _hex32(
                self._read_overlay_u32(TITLE_GPU_PFIFO_CACHE1_STATUS_ADDRESS)
            ),
            "gpu_progress_counter_address_hex": _hex32(
                TITLE_GPU_PROGRESS_COUNTER_ADDRESS
            ),
            "gpu_progress_counter_poll_count": (
                self._title_gpu_progress_counter_poll_count
            ),
            "gpu_progress_counter_last_value_hex": _hex32(
                self._title_gpu_progress_counter_last_value or 0
            )
            if self._title_gpu_progress_counter_last_value is not None
            else None,
            "gpu_progress_counter_current_hex": _hex32(
                self._read_overlay_u32(TITLE_GPU_PROGRESS_COUNTER_ADDRESS)
            ),
            "gpu_software_completion_flag_address_hex": _hex32(
                TITLE_GPU_SOFTWARE_COMPLETION_FLAG_ADDRESS
            ),
            "gpu_software_completion_pending_bit_hex": _hex32(
                TITLE_GPU_SOFTWARE_COMPLETION_PENDING_BIT
            ),
            "gpu_software_completion_clear_count": (
                self._title_gpu_software_completion_clear_count
            ),
            "gpu_software_completion_last_value_hex": _hex32(
                self._title_gpu_software_completion_last_value or 0
            )
            if self._title_gpu_software_completion_last_value is not None
            else None,
            "gpu_software_completion_current_hex": _hex32(
                self._read_overlay_u32(TITLE_GPU_SOFTWARE_COMPLETION_FLAG_ADDRESS)
            ),
            "mcpx_frame_counter_address_hex": _hex32(
                TITLE_MCPX_FRAME_COUNTER_ADDRESS
            ),
            "mcpx_frame_counter_increment_hex": _hex32(
                TITLE_MCPX_FRAME_COUNTER_INCREMENT
            ),
            "mcpx_frame_counter_read_count": (
                self._title_mcpx_frame_counter_read_count
            ),
            "mcpx_frame_counter_last_value_hex": _hex32(
                self._title_mcpx_frame_counter_last_value or 0
            )
            if self._title_mcpx_frame_counter_last_value is not None
            else None,
            "mcpx_frame_counter_current_hex": _hex32(
                self._read_overlay_u32(TITLE_MCPX_FRAME_COUNTER_ADDRESS)
            ),
            "audio_dsp_control_address_hex": _hex32(
                TITLE_AUDIO_DSP_CONTROL_ADDRESS
            ),
            "audio_dsp_status_address_hex": _hex32(TITLE_AUDIO_DSP_STATUS_ADDRESS),
            "audio_dsp_reset_request_bit_hex": _hex32(
                TITLE_AUDIO_DSP_RESET_REQUEST_BIT
            ),
            "audio_dsp_reset_ready_bit_hex": _hex32(
                TITLE_AUDIO_DSP_RESET_READY_BIT
            ),
            "audio_dsp_reset_count": self._title_audio_dsp_reset_count,
            "audio_dsp_status_read_count": self._title_audio_dsp_status_read_count,
            "audio_dsp_status_last_value_hex": _hex32(
                self._title_audio_dsp_status_last_value or 0
            )
            if self._title_audio_dsp_status_last_value is not None
            else None,
            "audio_dsp_voice_command_addresses_hex": [
                _hex32(address)
                for address in TITLE_AUDIO_DSP_VOICE_COMMAND_ADDRESSES
            ],
            "audio_dsp_voice_command_pending_bit_hex": _hex32(
                TITLE_AUDIO_DSP_VOICE_COMMAND_PENDING_BIT
            ),
            "audio_dsp_voice_command_count": (
                self._title_audio_dsp_voice_command_count
            ),
            "audio_dsp_voice_command_last_address_hex": _hex32(
                self._title_audio_dsp_voice_command_last_address or 0
            )
            if self._title_audio_dsp_voice_command_last_address is not None
            else None,
            "audio_dsp_status_current_hex": _hex32(
                self._read_overlay_u32(TITLE_AUDIO_DSP_STATUS_ADDRESS)
            ),
        }


class DynamicBlockCache:
    """JSON-backed decoded dynamic-block cache for local probe iteration."""

    VERSION = 1

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.records: dict[str, dict[str, Any]] = {}
        self.load_errors: list[str] = []
        self.hits = 0
        self.misses = 0
        self.stores = 0
        if path is not None and path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("format") == "b2-recomp-dynamic-block-cache":
                    records = data.get("records", {})
                    if isinstance(records, dict):
                        self.records = records
            except (OSError, json.JSONDecodeError) as exc:
                self.load_errors.append(str(exc))

    def key(
        self,
        *,
        image_sha256: str,
        target: int,
        entry_bytes: int,
        max_block_instructions: int,
    ) -> str:
        return ":".join(
            [
                str(self.VERSION),
                image_sha256,
                _hex32(target),
                str(entry_bytes),
                str(max_block_instructions),
            ]
        )

    def get(self, key: str) -> LiftedFunction | None:
        record = self.records.get(key)
        if record is None:
            self.misses += 1
            return None
        try:
            function = _lifted_function_from_cache_record(record)
        except (KeyError, TypeError, ValueError) as exc:
            self.load_errors.append(f"{key}: {exc}")
            self.misses += 1
            return None
        self.hits += 1
        return function

    def put(self, key: str, function: LiftedFunction) -> None:
        if key in self.records:
            return
        self.records[key] = _lifted_function_to_cache_record(function)
        self.stores += 1

    def save(self) -> None:
        if self.path is None or self.stores == 0:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "format": "b2-recomp-dynamic-block-cache",
            "public_safe": False,
            "version": self.VERSION,
            "records": self.records,
        }
        self.path.write_text(
            json.dumps(payload, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def summary(self) -> dict[str, Any]:
        return {
            "enabled": self.path is not None,
            "path": str(self.path) if self.path is not None else None,
            "record_count": len(self.records),
            "hits": self.hits,
            "misses": self.misses,
            "stores": self.stores,
            "load_errors": self.load_errors,
        }

    def lifted_functions(self) -> list[LiftedFunction]:
        functions: list[LiftedFunction] = []
        for key, record in self.records.items():
            try:
                functions.append(_lifted_function_from_cache_record(record))
            except (KeyError, TypeError, ValueError) as exc:
                self.load_errors.append(f"{key}: {exc}")
        return functions


def build_playability_probe_summary(
    xbe_path: Path,
    *,
    extracted_root: Path | None = None,
    entry_bytes: int = DEFAULT_ENTRY_BYTES,
    max_instructions: int = DEFAULT_MAX_INSTRUCTIONS,
    max_block_instructions: int = DEFAULT_MAX_BLOCK_INSTRUCTIONS,
    execute_entry: bool = True,
    max_steps: int = DEFAULT_MAX_STEPS,
    internal_depth: int = DEFAULT_INTERNAL_DEPTH,
    max_blocks: int = DEFAULT_MAX_RECOVERED_BLOCKS,
    max_dynamic_blocks: int = DEFAULT_MAX_DYNAMIC_BLOCKS,
    dynamic_block_cache_path: Path | None = None,
    render_watchpoint_limit: int | None = None,
    render_watchpoint_start: int = 0,
    save_data_root: Path | None = None,
    dashboard_data_root: Path | None = None,
    cache_data_root: Path | None = None,
    native_guest_loop: bool = False,
    native_build_dir: Path = Path("build/native-guest-loop"),
    live_render_stream_path: Path | None = None,
    live_controller_state_path: Path | None = None,
    live_flip_audit_ack_path: Path | None = None,
    live_flip_audit_health_interval: int = 30,
    live_flip_audit_max_flips: int = 0,
    native_slice_steps: int = 100000,
    audit_title_main_loop_exit: bool = False,
) -> dict[str, Any]:
    info = parse_xbe_file(xbe_path)
    image_sha256 = _file_sha256(xbe_path)
    imported_ordinals = [
        import_info["ordinal"] for import_info in info["kernel_imports"]["imports"]
    ]

    runtime = XboxRuntimeShims(
        XboxRuntimeConfig(
            extracted_disc_root=extracted_root,
            save_data_root=save_data_root,
            dashboard_data_root=dashboard_data_root,
            cache_data_root=cache_data_root,
            title_id=int(info["certificate"]["title_id"]["hex"], 16),
        )
    )
    resolver = ImportResolver()
    runtime.register_kernel_imports(resolver, imported_ordinals)
    loaded = load_xbe_file(xbe_path, resolver=resolver)
    bridge = RuntimeAbiBridge(runtime)
    unresolved = [
        resolution for resolution in loaded.import_resolutions if not resolution.resolved
    ]
    dynamic_block_cache = DynamicBlockCache(dynamic_block_cache_path)

    entry_summary = _recover_entry_summary(
        loaded,
        bridge,
        image_sha256=image_sha256,
        entry_bytes=entry_bytes,
        max_instructions=max_instructions,
        max_block_instructions=max_block_instructions,
        execute_entry=execute_entry,
        max_steps=max_steps,
        internal_depth=internal_depth,
        max_blocks=max_blocks,
        max_dynamic_blocks=max_dynamic_blocks,
        dynamic_block_cache=dynamic_block_cache,
        render_watchpoint_limit=render_watchpoint_limit,
        render_watchpoint_start=render_watchpoint_start,
        native_guest_loop=native_guest_loop,
        native_build_dir=native_build_dir,
        live_render_stream_path=live_render_stream_path,
        live_controller_state_path=live_controller_state_path,
        live_flip_audit_ack_path=live_flip_audit_ack_path,
        live_flip_audit_health_interval=live_flip_audit_health_interval,
        live_flip_audit_max_flips=live_flip_audit_max_flips,
        native_slice_steps=native_slice_steps,
        audit_title_main_loop_exit=audit_title_main_loop_exit,
    )
    dynamic_block_cache.save()

    runtime_summary = runtime.summary()
    bridge_summary = bridge.summary()
    asset_io_summary = _asset_io_summary_from_invocations(
        bridge_summary["invocations"]
    )
    return {
        "format": "b2-recomp-playability-probe",
        "public_safe": False,
        "source": {
            "input_file_name": xbe_path.name,
            "input_sha256": image_sha256,
            "title_name": info["certificate"]["title_name"],
            "kernel_import_count": len(imported_ordinals),
        },
        "loader": {
            "entry_point": loaded.entry_point,
            "entry_point_hex": _hex32(loaded.entry_point) if loaded.entry_point else None,
            "patched_import_count": len(loaded.import_resolutions),
            "unresolved_import_count": len(unresolved),
        },
        "runtime": {
            "registered_kernel_shim_count": len(runtime.registered_shims),
            "registered_behavior_counts": runtime_summary["registered_behavior_counts"],
            "registered_subsystem_counts": runtime_summary["registered_subsystem_counts"],
            "open_handles": runtime_summary["open_handles"],
            "open_files": runtime_summary["open_files"],
            "threads": runtime_summary["threads"],
            "input": runtime_summary["input"],
            "audio_initialized": runtime_summary["audio_initialized"],
            "audio_streams": runtime_summary["audio_streams"],
            "clock": runtime_summary["clock"],
            "determinism": runtime_summary["determinism"],
            "deterministic_service_validation": _deterministic_service_validation_summary(
                runtime_summary
            ),
        },
        "runtime_abi_bridge": bridge_summary,
        "asset_io": asset_io_summary,
        "entry_recovery": entry_summary,
        "playability_gaps": _playability_gaps(entry_summary, unresolved),
    }


def summary_json(summary: dict[str, Any], *, pretty: bool = False) -> str:
    return json.dumps(summary, indent=2 if pretty else None, sort_keys=True)


def _recover_entry_summary(
    loaded: LoadedXbeImage,
    bridge: RuntimeAbiBridge,
    *,
    image_sha256: str,
    entry_bytes: int,
    max_instructions: int,
    max_block_instructions: int,
    execute_entry: bool,
    max_steps: int,
    internal_depth: int,
    max_blocks: int,
    max_dynamic_blocks: int,
    dynamic_block_cache: DynamicBlockCache,
    render_watchpoint_limit: int | None,
    render_watchpoint_start: int,
    native_guest_loop: bool,
    native_build_dir: Path,
    live_render_stream_path: Path | None,
    live_controller_state_path: Path | None,
    live_flip_audit_ack_path: Path | None,
    live_flip_audit_health_interval: int,
    live_flip_audit_max_flips: int,
    native_slice_steps: int,
    audit_title_main_loop_exit: bool,
) -> dict[str, Any]:
    if loaded.entry_point is None:
        return {"status": "missing_entry_point"}

    try:
        code = loaded.arena.read(loaded.entry_point, entry_bytes)
        function = lift_x86_function(
            code,
            base_address=loaded.entry_point,
            symbol="xbe_entry",
            max_instructions=max_instructions,
        )
    except (X86DecodeError, XbeMemoryAccessError) as exc:
        return {
            "status": "decode_failed",
            "entry_point": loaded.entry_point,
            "entry_point_hex": _hex32(loaded.entry_point),
            "error": str(exc),
        }

    summary = {
        "status": "decoded",
        "entry_point": loaded.entry_point,
        "entry_point_hex": _hex32(loaded.entry_point),
        **_lifted_function_summary(function, bridge),
    }
    recovery = _recover_internal_functions(
        loaded,
        function,
        bridge,
        entry_bytes=entry_bytes,
        max_block_instructions=max_block_instructions,
        internal_depth=internal_depth,
        max_blocks=max_blocks,
    )
    summary["control_flow_recovery"] = {
        "internal_depth": internal_depth,
        "max_blocks": max_blocks,
        "max_block_instructions": max_block_instructions,
        "decoded_block_count": len(recovery["functions"]),
        "decoded_block_targets": [
            _hex32(function.base_address) for function in recovery["functions"]
        ],
        "decoded_internal_count": len(recovery["functions"]),
        "decoded_internal_targets": [
            _hex32(function.base_address) for function in recovery["functions"]
        ],
        "frontier_count": len(recovery["frontiers"]),
        "frontier_status_counts": dict(
            sorted(Counter(frontier["status"] for frontier in recovery["frontiers"]).items())
        ),
        "truncated": recovery["truncated"],
    }
    summary["frontier_batch"] = recovery["frontiers"]
    summary["internal_handoffs"] = [
        recovered
        for recovered in recovery["summaries"]
        if recovered.get("depth") == 1 and recovered.get("kind") == "direct_call"
    ]
    summary["recovered_internal_functions"] = recovery["summaries"]
    if execute_entry:
        summary["execution"] = _execute_recovered_control_flow_frame(
            loaded,
            function,
            recovery["functions"],
            bridge,
            image_sha256=image_sha256,
            max_steps=max_steps,
            entry_bytes=entry_bytes,
            max_block_instructions=max_block_instructions,
            max_dynamic_blocks=max_dynamic_blocks,
            dynamic_block_cache=dynamic_block_cache,
            render_watchpoint_limit=render_watchpoint_limit,
            render_watchpoint_start=render_watchpoint_start,
            native_guest_loop=native_guest_loop,
            native_build_dir=native_build_dir,
            live_render_stream_path=live_render_stream_path,
            live_controller_state_path=live_controller_state_path,
            live_flip_audit_ack_path=live_flip_audit_ack_path,
            live_flip_audit_health_interval=live_flip_audit_health_interval,
            live_flip_audit_max_flips=live_flip_audit_max_flips,
            native_slice_steps=native_slice_steps,
            audit_title_main_loop_exit=audit_title_main_loop_exit,
        )
    else:
        summary["execution"] = {"status": "skipped"}
    return summary


def _recover_internal_functions(
    loaded: LoadedXbeImage,
    entry_function: LiftedFunction,
    bridge: RuntimeAbiBridge,
    *,
    entry_bytes: int,
    max_block_instructions: int,
    internal_depth: int,
    max_blocks: int,
) -> dict[str, Any]:
    if internal_depth <= 0:
        return {
            "functions": [],
            "summaries": [],
            "frontiers": [],
            "truncated": False,
        }

    queue: list[RecoveryWorkItem] = [
        RecoveryWorkItem(target, 1, entry_function.base_address, "direct_call")
        for target in _internal_call_targets(entry_function, bridge)
    ]
    seen = {entry_function.base_address}
    covered_addresses = {instruction.address for instruction in entry_function.instructions}
    functions: list[LiftedFunction] = []
    summaries: list[dict[str, Any]] = []
    frontiers: list[dict[str, Any]] = []
    truncated = False

    while queue:
        item = queue.pop(0)
        if item.target in seen or item.target in covered_addresses:
            continue
        if item.depth > internal_depth:
            frontier = _recovery_frontier("depth_limit", item)
            summaries.append(frontier)
            frontiers.append(frontier)
            seen.add(item.target)
            continue
        if len(functions) >= max_blocks:
            truncated = True
            frontier = _recovery_frontier("recovery_limit", item)
            summaries.append(frontier)
            frontiers.append(frontier)
            seen.add(item.target)
            continue

        seen.add(item.target)
        try:
            code = loaded.arena.read(item.target, entry_bytes)
            function = lift_x86_block(
                code,
                base_address=item.target,
                symbol=f"block_{item.target:08X}",
                max_instructions=max_block_instructions,
            )
        except (X86DecodeError, XbeMemoryAccessError) as exc:
            frontier = _recovery_frontier("decode_failed", item, error=str(exc))
            summaries.append(frontier)
            frontiers.append(frontier)
            continue

        functions.append(function)
        covered_addresses.update(instruction.address for instruction in function.instructions)
        summaries.append(
            {
                "status": "decoded",
                "target": item.target,
                "target_hex": _hex32(item.target),
                "caller": item.caller,
                "caller_hex": _hex32(item.caller),
                "depth": item.depth,
                "kind": item.kind,
                **_lifted_function_summary(function, bridge),
            }
        )
        _enqueue_block_successors(
            queue,
            function,
            bridge,
            item,
            seen=seen,
            covered_addresses=covered_addresses,
        )

    return {
        "functions": functions,
        "summaries": summaries,
        "frontiers": frontiers,
        "truncated": truncated,
    }


def _recovery_frontier(
    status: str,
    item: RecoveryWorkItem,
    *,
    error: str | None = None,
) -> dict[str, Any]:
    frontier: dict[str, Any] = {
        "status": status,
        "target": item.target,
        "target_hex": _hex32(item.target),
        "caller": item.caller,
        "caller_hex": _hex32(item.caller),
        "depth": item.depth,
        "kind": item.kind,
    }
    if error is not None:
        frontier["error"] = error
    return frontier


def _enqueue_block_successors(
    queue: list[RecoveryWorkItem],
    function: LiftedFunction,
    bridge: RuntimeAbiBridge,
    item: RecoveryWorkItem,
    *,
    seen: set[int],
    covered_addresses: set[int],
) -> None:
    for child in _internal_call_targets(function, bridge):
        _enqueue_recovery_target(
            queue,
            child,
            depth=item.depth + 1,
            caller=function.base_address,
            kind="direct_call",
            seen=seen,
            covered_addresses=covered_addresses,
        )
    for branch_target in function.branch_targets:
        _enqueue_recovery_target(
            queue,
            branch_target,
            depth=item.depth,
            caller=function.base_address,
            kind="branch_target",
            seen=seen,
            covered_addresses=covered_addresses,
        )
    fallthrough = _conditional_fallthrough_target(function)
    if fallthrough is not None:
        _enqueue_recovery_target(
            queue,
            fallthrough,
            depth=item.depth,
            caller=function.base_address,
            kind="branch_fallthrough",
            seen=seen,
            covered_addresses=covered_addresses,
        )


def _enqueue_recovery_target(
    queue: list[RecoveryWorkItem],
    target: int,
    *,
    depth: int,
    caller: int,
    kind: str,
    seen: set[int],
    covered_addresses: set[int],
) -> None:
    if target in seen or target in covered_addresses:
        return
    if any(item.target == target for item in queue):
        return
    queue.append(RecoveryWorkItem(target, depth, caller, kind))


def _conditional_fallthrough_target(function: LiftedFunction) -> int | None:
    if not function.instructions:
        return None
    last = function.instructions[-1]
    if last.mnemonic == "jcc":
        return last.next_address
    return None


def _recover_native_frontier_batch(
    entry_function: LiftedFunction,
    *,
    block_loader: Callable[[int], LiftedFunction | None],
    external_targets: set[int],
    covered_addresses: set[int],
    max_depth: int,
    max_blocks: int,
) -> list[LiftedFunction]:
    """Recover a frontier's direct CFG before rebuilding its native module."""

    if max_blocks <= 0:
        return []

    known_addresses = set(covered_addresses)
    pending: deque[tuple[LiftedFunction, int]] = deque()
    recovered: list[LiftedFunction] = []
    attempted_targets: set[int] = set()

    def add_function(function: LiftedFunction, depth: int) -> None:
        if not any(
            instruction.address not in known_addresses
            for instruction in function.instructions
        ):
            return
        recovered.append(function)
        known_addresses.update(
            instruction.address for instruction in function.instructions
        )
        pending.append((function, depth))

    add_function(entry_function, 0)
    while pending and len(recovered) < max_blocks:
        function, depth = pending.popleft()
        successors: list[tuple[int, int]] = [
            (target, depth + 1)
            for target in function.call_targets
            if target not in external_targets and depth < max_depth
        ]
        successors.extend((target, depth) for target in function.branch_targets)
        fallthrough = _conditional_fallthrough_target(function)
        if fallthrough is not None:
            successors.append((fallthrough, depth))

        for target, target_depth in successors:
            if (
                len(recovered) >= max_blocks
                or target in attempted_targets
                or target in known_addresses
            ):
                continue
            attempted_targets.add(target)
            loaded_function = block_loader(target)
            if loaded_function is not None:
                add_function(loaded_function, target_depth)
    return recovered


def _seed_guest_thread_fs_block(memory: SparseMemory, fs_base: int) -> None:
    memory.write_u32(_u32(fs_base + THREAD_FS_SELF_POINTER_OFFSET), fs_base)
    memory.write_u32(_u32(fs_base + THREAD_FS_CALLBACK_TABLE_OFFSET), 0)


def _execute_recovered_control_flow_frame(
    loaded: LoadedXbeImage,
    entry_function: LiftedFunction,
    internal_functions: list[LiftedFunction],
    bridge: RuntimeAbiBridge,
    *,
    image_sha256: str,
    max_steps: int,
    entry_bytes: int,
    max_block_instructions: int,
    max_dynamic_blocks: int,
    dynamic_block_cache: DynamicBlockCache,
    render_watchpoint_limit: int | None,
    render_watchpoint_start: int,
    native_guest_loop: bool,
    native_build_dir: Path,
    live_render_stream_path: Path | None,
    live_controller_state_path: Path | None,
    live_flip_audit_ack_path: Path | None,
    live_flip_audit_health_interval: int,
    live_flip_audit_max_flips: int,
    native_slice_steps: int,
    audit_title_main_loop_exit: bool,
) -> dict[str, Any]:
    state = CpuState.with_registers(
        esp=DEFAULT_STACK_BASE,
        ebp=0,
        esi=0,
        edi=0,
        fs_base=DEFAULT_FS_BASE,
    )
    render_watchpoint = RenderWriteWatchpoint(
        stop_after=(
            render_watchpoint_start + render_watchpoint_limit
            if render_watchpoint_limit is not None
            else None
        ),
        capture_after=render_watchpoint_start,
    )
    memory = XbeBackedSparseMemory(
        loaded,
        {DEFAULT_STACK_BASE: BOOT_PROBE_RETURN},
        write_observer=render_watchpoint.observe,
    )
    # Kernel data imports point at storage, unlike callable kernel imports.
    # Seed their scalar values before any recovered guest code can dereference
    # the resolved target address.
    bridge.synchronize_data_exports(memory)
    _seed_guest_thread_fs_block(memory, DEFAULT_FS_BASE)
    frame = _merge_lifted_functions(entry_function, internal_functions)
    covered_addresses = {instruction.address for instruction in frame.instructions}
    spin_delay_fast_path = TitleSpinDelayFastPath()
    title_heap_fast_path = TitleGuestHeapFastPath()
    title_allocation_list_count_fast_path = TitleAllocationListCountFastPath()
    title_global_list_fast_path = TitleGlobalListRegistrationFastPath()
    title_drive_array_fast_path = TitleStaticDriveArrayFastPath()
    title_frontend_asset_init_fast_path = TitleFrontendAssetInitFastPath()
    title_asset_stream_open_fast_path = TitleAssetStreamOpenFastPath(bridge.runtime)
    host_audio_output = (
        WindowsPcmOutput(master_volume=0.5)
        if live_render_stream_path is not None
        else None
    )
    if host_audio_output is not None and host_audio_output.available:
        bridge.runtime.audio.set_output_backend(host_audio_output)
    title_frontend_special_audio_fast_path = TitleFrontendSpecialAudioFastPath(
        bridge.runtime,
        title_asset_stream_open_fast_path,
        host_audio_output,
    )
    title_music_mode_fast_path = TitleMusicModeFastPath(
        bridge.runtime,
        host_audio_output,
        cache_dir=native_build_dir / "audio-cache",
    )
    title_frontend_crt_initializer_audit = TitleFrontendCrtInitializerAudit()
    title_frontend_object_fast_path = TitleFrontendObjectConstructorFastPath()
    title_fixed_width_compare_fast_path = TitleFixedWidthCompareFastPath()
    title_d3d_flush_fast_path = TitleD3DFlushFastPath()
    title_d3d_packet_alloc_fast_path = TitleD3DPacketAllocFastPath()
    title_d3d_reserve_fast_path = TitleD3DReserveFastPath()
    title_d3d_primitive_draw_fast_path = TitleD3DPrimitiveDrawFastPath(
        render_watchpoint=render_watchpoint,
        execute_fast_path=False,
    )
    title_immediate_draw_audit = TitleImmediateDrawAudit(
        render_watchpoint=render_watchpoint
    )
    title_vertex_append_fast_path = TitleVertexAppendFastPath(
        render_watchpoint=render_watchpoint
    )
    title_text_draw_fast_path = TitleTextDrawFastPath(
        render_watchpoint=render_watchpoint
    )
    title_directsound_buffer_sync_fast_path = TitleDirectSoundBufferSyncFastPath()
    title_xinput_fast_path = TitleXInputFastPath(bridge.runtime.input)
    handlers = {
        **bridge.call_handlers(),
        **spin_delay_fast_path.call_handlers(),
        **title_heap_fast_path.call_handlers(),
        **title_allocation_list_count_fast_path.call_handlers(),
        **title_global_list_fast_path.call_handlers(),
        **title_drive_array_fast_path.call_handlers(),
        **title_frontend_asset_init_fast_path.call_handlers(),
        **title_asset_stream_open_fast_path.call_handlers(),
        **title_frontend_special_audio_fast_path.call_handlers(),
        **title_music_mode_fast_path.call_handlers(),
        **title_frontend_crt_initializer_audit.call_handlers(),
        **title_frontend_object_fast_path.call_handlers(),
        **title_fixed_width_compare_fast_path.call_handlers(),
        **title_d3d_flush_fast_path.call_handlers(),
        **title_d3d_packet_alloc_fast_path.call_handlers(),
        **title_d3d_reserve_fast_path.call_handlers(),
        **title_d3d_primitive_draw_fast_path.call_handlers(),
        **title_vertex_append_fast_path.call_handlers(),
        **title_text_draw_fast_path.call_handlers(),
        **title_directsound_buffer_sync_fast_path.call_handlers(),
        **title_xinput_fast_path.call_handlers(),
    }
    dynamic_functions: list[LiftedFunction] = []
    dynamic_summaries: list[dict[str, Any]] = []
    dynamic_frontiers: list[dict[str, Any]] = []
    dynamic_seen: set[int] = set()

    def dynamic_block_loader(target: int) -> LiftedFunction | None:
        if target in dynamic_seen or target in covered_addresses:
            return None
        if not _is_executable_address(loaded, target):
            return None
        item = RecoveryWorkItem(target, 0, target, "dynamic_execution")
        dynamic_seen.add(target)
        if len(dynamic_functions) >= max_dynamic_blocks:
            frontier = _recovery_frontier("recovery_limit", item)
            dynamic_summaries.append(frontier)
            dynamic_frontiers.append(frontier)
            return None
        cache_key = dynamic_block_cache.key(
            image_sha256=image_sha256,
            target=target,
            entry_bytes=entry_bytes,
            max_block_instructions=max_block_instructions,
        )
        cached = dynamic_block_cache.get(cache_key)
        if cached is not None:
            dynamic_functions.append(cached)
            covered_addresses.update(
                instruction.address for instruction in cached.instructions
            )
            dynamic_summaries.append(
                {
                    "status": "cache_hit",
                    "target": target,
                    "target_hex": _hex32(target),
                    "caller": target,
                    "caller_hex": _hex32(target),
                    "depth": 0,
                    "kind": "dynamic_execution",
                    **_lifted_function_summary(cached, bridge),
                }
            )
            return cached
        try:
            adaptive_instruction_limit = max_block_instructions
            while True:
                code = _read_dynamic_block_window(
                    loaded,
                    target,
                    minimum_size=entry_bytes,
                    preferred_size=max(entry_bytes, adaptive_instruction_limit * 15),
                )
                try:
                    function = lift_x86_block(
                        code,
                        base_address=target,
                        symbol=f"dynamic_block_{target:08X}",
                        max_instructions=adaptive_instruction_limit,
                    )
                    break
                except X86DecodeError as exc:
                    if (
                        "instruction limit reached before block end" not in str(exc)
                        or adaptive_instruction_limit >= 16384
                    ):
                        raise
                    adaptive_instruction_limit = min(
                        adaptive_instruction_limit * 2,
                        16384,
                    )
        except (X86DecodeError, XbeMemoryAccessError) as exc:
            frontier = _recovery_frontier("decode_failed", item, error=str(exc))
            dynamic_summaries.append(frontier)
            dynamic_frontiers.append(frontier)
            return None

        dynamic_functions.append(function)
        dynamic_block_cache.put(cache_key, function)
        covered_addresses.update(
            instruction.address for instruction in function.instructions
        )
        dynamic_summaries.append(
            {
                "status": "decoded",
                "target": target,
                "target_hex": _hex32(target),
                "caller": target,
                "caller_hex": _hex32(target),
                "depth": 0,
                "kind": "dynamic_execution",
                "adaptive_instruction_limit": adaptive_instruction_limit,
                **_lifted_function_summary(function, bridge),
            }
        )
        return function

    try:
        result = execute_lifted_function(
            frame,
            state=state,
            memory=memory,
            call_handlers=handlers,
            unhandled_call_handler=_stop_at_internal_call,
            block_loader=dynamic_block_loader,
            max_steps=max_steps,
        )
    except RenderWatchpointStop as exc:
        return {
            "status": "render_watchpoint_stop",
            "render_watchpoint_stream": _snapshot_render_texture_resources(
                exc.stream,
                render_watchpoint.history_stream(),
                memory,
            ),
            "dynamic_block_cache": dynamic_block_cache.summary(),
            **_dynamic_recovery_summary(
                dynamic_functions,
                dynamic_summaries,
                dynamic_frontiers,
            ),
            "title_spin_delay_fast_path": spin_delay_fast_path.summary(),
            "title_heap_fast_path": title_heap_fast_path.summary(),
            "title_allocation_list_count_fast_path": (
                title_allocation_list_count_fast_path.summary()
            ),
            "title_global_list_fast_path": title_global_list_fast_path.summary(),
            "title_static_drive_array_fast_path": title_drive_array_fast_path.summary(),
            "title_frontend_asset_init_fast_path": (
                title_frontend_asset_init_fast_path.summary()
            ),
            "title_asset_stream_open_fast_path": title_asset_stream_open_fast_path.summary(),
            "title_frontend_special_audio_fast_path": title_frontend_special_audio_fast_path.summary(),
            "title_music_mode_fast_path": title_music_mode_fast_path.summary(),
            "title_frontend_object_fast_path": (
                title_frontend_object_fast_path.summary()
            ),
            "title_fixed_width_compare_fast_path": (
                title_fixed_width_compare_fast_path.summary()
            ),
            "title_d3d_flush_fast_path": title_d3d_flush_fast_path.summary(),
            "title_d3d_packet_alloc_fast_path": (
                title_d3d_packet_alloc_fast_path.summary()
            ),
            "title_d3d_reserve_fast_path": title_d3d_reserve_fast_path.summary(),
            "title_d3d_primitive_draw_fast_path": (
                title_d3d_primitive_draw_fast_path.summary()
            ),
            "title_immediate_draw_audit": title_immediate_draw_audit.summary(),
            "title_vertex_append_fast_path": title_vertex_append_fast_path.summary(),
            "title_text_draw_fast_path": title_text_draw_fast_path.summary(),
            "title_directsound_buffer_sync_fast_path": title_directsound_buffer_sync_fast_path.summary(),
            "title_hardware_completion": memory.title_hardware_completion_summary(),
        }
    except BootProbeStop as exc:
        trace_events = exc.trace.to_list()
        return {
            "status": "blocked_internal_call",
            "target": exc.target,
            "target_hex": _hex32(exc.target),
            "stack_args": [_hex32(argument) for argument in exc.stack_args],
            "state": exc.state.to_dict(),
            "trace_event_count": len(trace_events),
            "executed_internal_targets": _executed_internal_targets(
                trace_events,
                {
                    function.base_address
                    for function in [*internal_functions, *dynamic_functions]
                },
            ),
            **_dynamic_recovery_summary(
                dynamic_functions,
                dynamic_summaries,
                dynamic_frontiers,
            ),
            "dynamic_block_cache": dynamic_block_cache.summary(),
            "render_watchpoint_stream": _snapshot_render_texture_resources(
                render_watchpoint.to_stream(),
                render_watchpoint.history_stream(),
                memory,
            ),
            "title_spin_delay_fast_path": spin_delay_fast_path.summary(),
            "title_heap_fast_path": title_heap_fast_path.summary(),
            "title_allocation_list_count_fast_path": (
                title_allocation_list_count_fast_path.summary()
            ),
            "title_global_list_fast_path": title_global_list_fast_path.summary(),
            "title_static_drive_array_fast_path": title_drive_array_fast_path.summary(),
            "title_frontend_asset_init_fast_path": (
                title_frontend_asset_init_fast_path.summary()
            ),
            "title_asset_stream_open_fast_path": title_asset_stream_open_fast_path.summary(),
            "title_frontend_special_audio_fast_path": title_frontend_special_audio_fast_path.summary(),
            "title_music_mode_fast_path": title_music_mode_fast_path.summary(),
            "title_frontend_object_fast_path": (
                title_frontend_object_fast_path.summary()
            ),
            "title_fixed_width_compare_fast_path": (
                title_fixed_width_compare_fast_path.summary()
            ),
            "title_d3d_flush_fast_path": title_d3d_flush_fast_path.summary(),
            "title_d3d_packet_alloc_fast_path": (
                title_d3d_packet_alloc_fast_path.summary()
            ),
            "title_d3d_reserve_fast_path": title_d3d_reserve_fast_path.summary(),
            "title_d3d_primitive_draw_fast_path": (
                title_d3d_primitive_draw_fast_path.summary()
            ),
            "title_immediate_draw_audit": title_immediate_draw_audit.summary(),
            "title_vertex_append_fast_path": title_vertex_append_fast_path.summary(),
            "title_text_draw_fast_path": title_text_draw_fast_path.summary(),
            "title_directsound_buffer_sync_fast_path": title_directsound_buffer_sync_fast_path.summary(),
            "title_hardware_completion": memory.title_hardware_completion_summary(),
        }
    except (X86ExecutionError, RuntimeAbiBridgeError) as exc:
        return {
            "status": "execution_failed",
            "error": str(exc),
            **_dynamic_recovery_summary(
                dynamic_functions,
                dynamic_summaries,
                dynamic_frontiers,
            ),
            "dynamic_block_cache": dynamic_block_cache.summary(),
            "render_watchpoint_stream": _snapshot_render_texture_resources(
                render_watchpoint.to_stream(),
                render_watchpoint.history_stream(),
                memory,
            ),
            "title_spin_delay_fast_path": spin_delay_fast_path.summary(),
            "title_heap_fast_path": title_heap_fast_path.summary(),
            "title_allocation_list_count_fast_path": (
                title_allocation_list_count_fast_path.summary()
            ),
            "title_global_list_fast_path": title_global_list_fast_path.summary(),
            "title_static_drive_array_fast_path": title_drive_array_fast_path.summary(),
            "title_frontend_asset_init_fast_path": (
                title_frontend_asset_init_fast_path.summary()
            ),
            "title_asset_stream_open_fast_path": title_asset_stream_open_fast_path.summary(),
            "title_frontend_special_audio_fast_path": title_frontend_special_audio_fast_path.summary(),
            "title_music_mode_fast_path": title_music_mode_fast_path.summary(),
            "title_frontend_object_fast_path": (
                title_frontend_object_fast_path.summary()
            ),
            "title_fixed_width_compare_fast_path": (
                title_fixed_width_compare_fast_path.summary()
            ),
            "title_d3d_flush_fast_path": title_d3d_flush_fast_path.summary(),
            "title_d3d_packet_alloc_fast_path": (
                title_d3d_packet_alloc_fast_path.summary()
            ),
            "title_d3d_reserve_fast_path": title_d3d_reserve_fast_path.summary(),
            "title_d3d_primitive_draw_fast_path": (
                title_d3d_primitive_draw_fast_path.summary()
            ),
            "title_immediate_draw_audit": title_immediate_draw_audit.summary(),
            "title_vertex_append_fast_path": title_vertex_append_fast_path.summary(),
            "title_text_draw_fast_path": title_text_draw_fast_path.summary(),
            "title_directsound_buffer_sync_fast_path": title_directsound_buffer_sync_fast_path.summary(),
            "title_hardware_completion": memory.title_hardware_completion_summary(),
        }

    returned_to_probe = result.return_address == BOOT_PROBE_RETURN
    trace_events = result.trace.to_list()
    thread_executions: list[dict[str, Any]] = []
    executed_thread_keys: set[int] = set()
    if returned_to_probe:
        while len(thread_executions) < DEFAULT_MAX_GUEST_THREAD_EXECUTIONS:
            next_thread = _next_unexecuted_guest_thread(
                bridge, loaded, executed_thread_keys
            )
            if next_thread is None:
                break
            thread_key, thread = next_thread
            executed_thread_keys.add(thread_key)
            thread_execution = _execute_guest_thread_start(
                loaded,
                entry_function,
                internal_functions,
                dynamic_functions,
                bridge,
                thread,
                thread_index=len(thread_executions),
                memory=memory,
                render_watchpoint=render_watchpoint,
                handlers=handlers,
                frontend_crt_initializer_audit=(
                    title_frontend_crt_initializer_audit
                ),
                title_d3d_primitive_draw_audit=(
                    title_d3d_primitive_draw_fast_path
                ),
                title_immediate_draw_audit=title_immediate_draw_audit,
                block_loader=dynamic_block_loader,
                max_dynamic_blocks=max_dynamic_blocks,
                max_steps=(
                    0
                    if max_steps == 0
                    else max(max_steps, DEFAULT_MAX_THREAD_STEPS)
                ),
                native_guest_loop=native_guest_loop,
                native_build_dir=native_build_dir,
                live_render_stream_path=(
                    live_render_stream_path
                    if not thread_executions
                    else None
                ),
                live_controller_state_path=(
                    live_controller_state_path
                    if not thread_executions
                    else None
                ),
                live_flip_audit_ack_path=(
                    live_flip_audit_ack_path
                    if not thread_executions
                    else None
                ),
                live_flip_audit_health_interval=live_flip_audit_health_interval,
                live_flip_audit_max_flips=live_flip_audit_max_flips,
                native_slice_steps=native_slice_steps,
                audit_title_main_loop_exit=audit_title_main_loop_exit,
                cached_functions=dynamic_block_cache.lifted_functions(),
            )
            thread_executions.append(thread_execution)
            # A live controller stop terminates the process, not merely the
            # first Xbox system thread. Starting another unlimited guest
            # thread here would orphan it after the presenter has closed.
            if _guest_thread_requested_live_stop(thread_execution):
                break
    scheduled_threads = _scheduled_guest_threads(bridge, loaded)
    return {
        "status": "returned" if returned_to_probe else "returned_to_guest",
        "return_address": result.return_address,
        "return_address_hex": _hex32(result.return_address)
        if result.return_address is not None
        else None,
        "steps": result.steps,
        "state": result.state.to_dict(),
        "trace_event_count": len(trace_events),
        "executed_internal_targets": _executed_internal_targets(
            trace_events,
            {
                function.base_address
                for function in [*internal_functions, *dynamic_functions]
            },
        ),
        "guest_thread_count": len(scheduled_threads),
        "guest_threads": scheduled_threads,
        "guest_thread_execution_count": len(thread_executions),
        "guest_thread_executions": thread_executions,
        "live_stop_requested": any(
            _guest_thread_requested_live_stop(execution)
            for execution in thread_executions
        ),
        "title_xinput_fast_path": title_xinput_fast_path.summary(memory),
        **_dynamic_recovery_summary(
            dynamic_functions,
            dynamic_summaries,
            dynamic_frontiers,
        ),
        "dynamic_block_cache": dynamic_block_cache.summary(),
        "render_watchpoint_stream": _snapshot_render_texture_resources(
            render_watchpoint.to_stream(),
            render_watchpoint.history_stream(),
            memory,
        ),
        "title_spin_delay_fast_path": spin_delay_fast_path.summary(),
        "title_heap_fast_path": title_heap_fast_path.summary(),
        "title_allocation_list_count_fast_path": (
            title_allocation_list_count_fast_path.summary()
        ),
        "title_global_list_fast_path": title_global_list_fast_path.summary(),
        "title_static_drive_array_fast_path": title_drive_array_fast_path.summary(),
        "title_frontend_asset_init_fast_path": (
            title_frontend_asset_init_fast_path.summary()
        ),
        "title_asset_stream_open_fast_path": title_asset_stream_open_fast_path.summary(),
        "title_frontend_special_audio_fast_path": title_frontend_special_audio_fast_path.summary(),
        "title_music_mode_fast_path": title_music_mode_fast_path.summary(),
        "host_audio_output": host_audio_output.summary() if host_audio_output is not None else None,
        "title_frontend_crt_initializer_audit": title_frontend_crt_initializer_audit.summary(),
        "title_frontend_object_fast_path": title_frontend_object_fast_path.summary(),
        "title_fixed_width_compare_fast_path": (
            title_fixed_width_compare_fast_path.summary()
        ),
        "title_d3d_flush_fast_path": title_d3d_flush_fast_path.summary(),
        "title_d3d_packet_alloc_fast_path": (
            title_d3d_packet_alloc_fast_path.summary()
        ),
        "title_d3d_reserve_fast_path": title_d3d_reserve_fast_path.summary(),
        "title_d3d_primitive_draw_fast_path": (
            title_d3d_primitive_draw_fast_path.summary()
        ),
        "title_immediate_draw_audit": title_immediate_draw_audit.summary(),
        "title_vertex_append_fast_path": title_vertex_append_fast_path.summary(),
        "title_text_draw_fast_path": title_text_draw_fast_path.summary(),
        "title_directsound_buffer_sync_fast_path": title_directsound_buffer_sync_fast_path.summary(),
        "title_hardware_completion": memory.title_hardware_completion_summary(),
    }


def _next_unexecuted_guest_thread(
    bridge: RuntimeAbiBridge,
    loaded: LoadedXbeImage,
    executed_thread_keys: set[int],
) -> tuple[int, dict[str, Any]] | None:
    for thread in _scheduled_guest_threads(bridge, loaded):
        key = thread.get("handle")
        if not isinstance(key, int):
            key = thread["invocation_index"]
        if key in executed_thread_keys:
            continue
        return key, thread
    return None


def _read_dynamic_block_window(
    loaded: LoadedXbeImage,
    target: int,
    *,
    minimum_size: int,
    preferred_size: int,
) -> bytes:
    # A valid block target can be closer than ``minimum_size`` to the end of
    # its mapped section. Validate the target itself, then clip the decode
    # window to the containing region instead of rejecting the block before
    # the decoder has a chance to find its terminator.
    region = loaded.arena.region_for(target, 1)
    available_size = region.virtual_end - target
    requested_size = max(minimum_size, preferred_size)
    return loaded.arena.read(target, min(requested_size, available_size))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _lifted_function_to_cache_record(function: LiftedFunction) -> dict[str, Any]:
    return function.to_dict(include_bytes=False)


def _lifted_function_from_cache_record(record: dict[str, Any]) -> LiftedFunction:
    instructions = tuple(
        _instruction_from_cache_record(instruction)
        for instruction in record.get("instructions", [])
    )
    return LiftedFunction(
        symbol=str(record["symbol"]),
        base_address=int(record["base_address"]),
        code_size=int(record["code_size"]),
        instructions=instructions,
        target_platform=str(record.get("target_platform", "windows")),
        generated_language=str(record.get("generated_language", "c++17")),
        renderer_backend=str(record.get("renderer_backend", "vulkan")),
    )


def _instruction_from_cache_record(record: dict[str, Any]) -> X86Instruction:
    return X86Instruction(
        address=int(record["address"]),
        size=int(record["size"]),
        mnemonic=str(record["mnemonic"]),
        operands=tuple(
            _operand_from_cache_record(operand)
            for operand in record.get("operands", [])
        ),
        bytes_hex=str(record.get("bytes_hex", "")),
        target=record.get("target"),
        condition=record.get("condition"),
        ret_stack_adjust=int(record.get("ret_stack_adjust", 0)),
    )


def _operand_from_cache_record(record: dict[str, Any]) -> Operand:
    return Operand(
        kind=str(record["kind"]),
        size=int(record.get("size", 32)),
        reg=record.get("reg"),
        immediate=record.get("immediate"),
        base=record.get("base"),
        index=record.get("index"),
        scale=int(record.get("scale", 1)),
        displacement=int(record.get("displacement", 0)),
        absolute=record.get("absolute"),
        segment=record.get("segment"),
    )


def _scheduled_guest_threads(
    bridge: RuntimeAbiBridge,
    loaded: LoadedXbeImage,
) -> list[dict[str, Any]]:
    threads: list[dict[str, Any]] = []
    for index, invocation in enumerate(bridge.invocations):
        if invocation.shim_name != "PsCreateSystemThreadEx":
            continue
        result = invocation.result if isinstance(invocation.result, dict) else {}
        start_address = result.get("start_address")
        if not isinstance(start_address, int):
            start_address = 0
        handle = result.get("handle")
        thread = {
            "invocation_index": index,
            "handle": handle if isinstance(handle, int) else None,
            "handle_hex": _hex32(handle) if isinstance(handle, int) else None,
            "start_address": start_address,
            "start_address_hex": _hex32(start_address),
            "start_context1": result.get("start_context1", 0),
            "start_context1_hex": _hex32(result.get("start_context1", 0)),
            "start_context2": result.get("start_context2", 0),
            "start_context2_hex": _hex32(result.get("start_context2", 0)),
            "suspended": bool(result.get("suspended", False)),
            "executable": bool(start_address and _is_executable_address(loaded, start_address)),
        }
        threads.append(thread)
    return threads


def _guest_thread_requested_live_stop(execution: dict[str, Any]) -> bool:
    native_run = execution.get("native_run")
    return (
        isinstance(native_run, dict)
        and native_run.get("reason") == "yield_handler_stop"
    )


def _execute_guest_thread_start(
    loaded: LoadedXbeImage,
    entry_function: LiftedFunction,
    internal_functions: list[LiftedFunction],
    dynamic_functions: list[LiftedFunction],
    bridge: RuntimeAbiBridge,
    thread: dict[str, Any],
    *,
    thread_index: int,
    memory: SparseMemory,
    render_watchpoint: RenderWriteWatchpoint,
    handlers: dict[int, Callable[[CpuState, SparseMemory, int, ExecutionTrace], None]],
    frontend_crt_initializer_audit: TitleFrontendCrtInitializerAudit,
    title_d3d_primitive_draw_audit: TitleD3DPrimitiveDrawFastPath,
    title_immediate_draw_audit: TitleImmediateDrawAudit,
    block_loader: Callable[[int], LiftedFunction | None],
    max_dynamic_blocks: int,
    max_steps: int,
    native_guest_loop: bool = False,
    native_build_dir: Path = Path("build/native-guest-loop"),
    cached_functions: list[LiftedFunction] | None = None,
    live_render_stream_path: Path | None = None,
    live_controller_state_path: Path | None = None,
    live_flip_audit_ack_path: Path | None = None,
    live_flip_audit_health_interval: int = 30,
    live_flip_audit_max_flips: int = 0,
    native_slice_steps: int = 100000,
    enable_title_repair_fallbacks: bool = False,
    audit_title_main_loop_exit: bool = False,
) -> dict[str, Any]:
    start_address = thread["start_address"]
    summary = {
        "thread_index": thread_index,
        "handle": thread.get("handle"),
        "handle_hex": thread.get("handle_hex"),
        "start_address": start_address,
        "start_address_hex": thread["start_address_hex"],
        "start_context1": thread["start_context1"],
        "start_context1_hex": thread["start_context1_hex"],
        "start_context2": thread["start_context2"],
        "start_context2_hex": thread["start_context2_hex"],
        "title_repair_fallbacks_enabled": enable_title_repair_fallbacks,
    }
    if thread["suspended"]:
        return {**summary, "status": "skipped_suspended"}
    if not start_address:
        return {**summary, "status": "skipped_missing_start"}
    if not thread["executable"] or not _is_executable_address(loaded, start_address):
        return {**summary, "status": "skipped_non_executable_start"}

    stack_base = DEFAULT_THREAD_STACK_BASE + thread_index * THREAD_STACK_STRIDE
    return_sentinel = BOOT_THREAD_RETURN_BASE + thread_index * 4
    memory.write_u32(stack_base, return_sentinel)
    memory.write_u32(stack_base + 4, thread["start_context1"])
    memory.write_u32(stack_base + 8, thread["start_context2"])
    fs_base = DEFAULT_FS_BASE + (thread_index + 1) * THREAD_STACK_STRIDE
    _seed_guest_thread_fs_block(memory, fs_base)
    state = CpuState.with_registers(
        esp=stack_base,
        ebp=0,
        esi=0,
        edi=0,
        fs_base=fs_base,
    )
    frame_functions = [
        *internal_functions,
        *dynamic_functions,
        *(cached_functions or []),
    ]
    native_branch_functions = (
        _recover_missing_branch_targets(
            frame_functions,
            block_loader,
            start_address=TITLE_INPUT_CONSUMER_ADDRESS,
            end_address=TITLE_INPUT_CONSUMER_END_ADDRESS,
        )
        if native_guest_loop
        else []
    )
    frame = _merge_lifted_functions(
        entry_function,
        [*frame_functions, *native_branch_functions],
        symbol=f"guest_thread_{thread_index:02d}_{start_address:08X}",
        base_address=start_address,
    )
    native_base_frame = frame
    invocation_start = bridge.invocation_count
    scheduler_loop_detector = SchedulerLoopConvergenceDetector()
    subsystem_initializer_audit = TitleSubsystemInitializerAudit()
    frontend_registry_audit = TitleFrontendRegistryAudit()
    frontend_compare_search_repair = TitleFrontendCompareSearchSentinelRepair()
    frontend_record_table_count_repair = TitleFrontendRecordTableCountRepair()
    frontend_post_audio_list_repair = TitleFrontendPostAudioListRepair()
    frontend_static_singleton_repair = TitleFrontendStaticSingletonRepair()
    runtime_object_table_repair = TitleRuntimeObjectTableConstructorRepair()
    runtime_callback_list_repair = TitleRuntimeCallbackListRepair()
    native_run_summary: dict[str, Any] | None = None
    native_run_summaries: list[dict[str, Any]] = []
    native_frontier_functions: list[LiftedFunction] = []
    title_main_loop_exit_initial_value = memory.read_u32(
        TITLE_MAIN_LOOP_EXIT_FLAG_ADDRESS
    )
    title_main_loop_exit_writes: list[dict[str, Any]] = []
    title_main_loop_control_events: list[dict[str, Any]] = []
    pending_native_write_observations: list[tuple[int, bytes]] = []

    def record_title_main_loop_exit_write(
        *,
        source: str,
        instruction_address: int,
        write_address: int,
        payload: bytes,
        steps: int | None,
    ) -> None:
        size = len(payload)
        value = int.from_bytes(payload, "little")
        flag_start = TITLE_MAIN_LOOP_EXIT_FLAG_ADDRESS
        flag_end = flag_start + 4
        write_end = write_address + size
        if write_address >= flag_end or write_end <= flag_start:
            return
        before = memory.read_u32(flag_start)
        after_bytes = bytearray(before.to_bytes(4, "little"))
        for current in range(max(write_address, flag_start), min(write_end, flag_end)):
            after_bytes[current - flag_start] = payload[current - write_address]
        after = int.from_bytes(after_bytes, "little")
        title_main_loop_exit_writes.append(
            {
                "source": source,
                "instruction_address": instruction_address,
                "instruction_address_hex": _hex32(instruction_address),
                "steps": steps,
                "write_address": write_address,
                "write_address_hex": _hex32(write_address),
                "size": size,
                "value": value,
                "value_hex": _hex32(value),
                "before": before,
                "before_hex": _hex32(before),
                "after": after,
                "after_hex": _hex32(after),
            }
        )

    def observe_native_memory_write(
        instruction_address: int,
        write_address: int,
        size: int,
        value: int,
        steps: int,
    ) -> None:
        if not audit_title_main_loop_exit:
            return
        payload = int(value).to_bytes(size, "little", signed=False)
        pending_native_write_observations.append((write_address, payload))
        record_title_main_loop_exit_write(
            source="native_guest",
            instruction_address=instruction_address,
            write_address=write_address,
            payload=payload,
            steps=steps,
        )

    def observe_host_memory_write(write_address: int, payload: bytes) -> None:
        if not audit_title_main_loop_exit:
            return
        pending = (write_address, payload)
        if pending in pending_native_write_observations:
            pending_native_write_observations.remove(pending)
            return
        record_title_main_loop_exit_write(
            source="host_model",
            instruction_address=state.eip,
            write_address=write_address,
            payload=payload,
            steps=None,
        )

    if audit_title_main_loop_exit and isinstance(memory, XbeBackedSparseMemory):
        memory.add_write_observer(observe_host_memory_write)

    def title_main_loop_exit_audit_summary() -> dict[str, Any]:
        return {
            "enabled": audit_title_main_loop_exit,
            "address": TITLE_MAIN_LOOP_EXIT_FLAG_ADDRESS,
            "address_hex": _hex32(TITLE_MAIN_LOOP_EXIT_FLAG_ADDRESS),
            "initial_value": title_main_loop_exit_initial_value,
            "initial_value_hex": _hex32(title_main_loop_exit_initial_value),
            "current_value": memory.read_u32(TITLE_MAIN_LOOP_EXIT_FLAG_ADDRESS),
            "current_value_hex": _hex32(
                memory.read_u32(TITLE_MAIN_LOOP_EXIT_FLAG_ADDRESS)
            ),
            "write_count": len(title_main_loop_exit_writes),
            "writes": title_main_loop_exit_writes[-64:],
            "control_event_count": len(title_main_loop_control_events),
            "control_events": title_main_loop_control_events[-1024:],
        }
    live_host_bridge = (
        LiveHostBridge(
            bridge.runtime,
            render_watchpoint,
            render_stream_path=live_render_stream_path,
            controller_state_path=live_controller_state_path,
            flip_audit_ack_path=live_flip_audit_ack_path,
            flip_audit_health_interval=live_flip_audit_health_interval,
            flip_audit_max_flips=live_flip_audit_max_flips,
            presentation_ack_path=(
                live_render_stream_path.with_name(
                    live_render_stream_path.name + ".presented.bin"
                )
                if live_flip_audit_ack_path is None
                else None
            ),
            data_export_synchronizer=lambda memory: bridge.synchronize_data_exports(
                memory,
                volatile_only=True,
            ),
        )
        if live_render_stream_path is not None
        and live_controller_state_path is not None
        else None
    )

    def observe_guest_thread_step(
        cpu: CpuState,
        observed_memory: SparseMemory,
        trace: ExecutionTrace,
        steps: int,
    ) -> None:
        if audit_title_main_loop_exit and cpu.eip in TITLE_MAIN_LOOP_AUDIT_ADDRESSES:
            esp = cpu.get_register("esp")
            event = {
                    "instruction_address": cpu.eip,
                    "instruction_address_hex": _hex32(cpu.eip),
                    "steps": steps,
                    "eax": cpu.get_register("eax"),
                    "eax_hex": _hex32(cpu.get_register("eax")),
                    "ebx": cpu.get_register("ebx"),
                    "ebx_hex": _hex32(cpu.get_register("ebx")),
                    "ecx": cpu.get_register("ecx"),
                    "ecx_hex": _hex32(cpu.get_register("ecx")),
                    "edx": cpu.get_register("edx"),
                    "edx_hex": _hex32(cpu.get_register("edx")),
                    "esi": cpu.get_register("esi"),
                    "esi_hex": _hex32(cpu.get_register("esi")),
                    "edi": cpu.get_register("edi"),
                    "edi_hex": _hex32(cpu.get_register("edi")),
                    "ebp": cpu.get_register("ebp"),
                    "ebp_hex": _hex32(cpu.get_register("ebp")),
                    "esp": esp,
                    "esp_hex": _hex32(esp),
                    "stack_return": observed_memory.read_u32(esp),
                    "stack_return_hex": _hex32(observed_memory.read_u32(esp)),
                    "exit_flag": observed_memory.read_u32(
                        TITLE_MAIN_LOOP_EXIT_FLAG_ADDRESS
                    ),
                    "exit_flag_hex": _hex32(
                        observed_memory.read_u32(TITLE_MAIN_LOOP_EXIT_FLAG_ADDRESS)
                    ),
                }
            if cpu.eip in {0x000D4E56, 0x000D4830, 0x0010C530}:
                ebx = cpu.get_register("ebx")
                descriptor = observed_memory.read_u32(_u32(ebx + 4))
                dispatch_table = observed_memory.read_u32(_u32(descriptor + 4))
                event["dispatch_object"] = {
                    "address_hex": _hex32(ebx),
                    "object_hex": _hex32(observed_memory.read_u32(ebx)),
                    "descriptor_hex": _hex32(descriptor),
                    "dispatch_table_hex": _hex32(dispatch_table),
                    "current_eax_target_hex": _hex32(
                        observed_memory.read_u32(cpu.get_register("eax"))
                    ),
                }
            title_main_loop_control_events.append(event)
        subsystem_initializer_audit.observer(cpu, observed_memory, trace, steps)
        frontend_registry_audit.observer(cpu, observed_memory, trace, steps)
        if enable_title_repair_fallbacks:
            frontend_compare_search_repair.observer(
                cpu, observed_memory, trace, steps
            )
            frontend_record_table_count_repair.observer(
                cpu, observed_memory, trace, steps
            )
            frontend_post_audio_list_repair.observer(
                cpu, observed_memory, trace, steps
            )
            frontend_static_singleton_repair.observer(
                cpu, observed_memory, trace, steps
            )
            runtime_object_table_repair.observer(
                cpu, observed_memory, trace, steps
            )
            runtime_callback_list_repair.observer(
                cpu, observed_memory, trace, steps
            )
        frontend_crt_initializer_audit.observer(
            cpu, observed_memory, trace, steps
        )
        scheduler_loop_detector.observer(cpu, observed_memory, trace, steps)
        title_d3d_primitive_draw_audit.observer(
            cpu, observed_memory, trace, steps
        )
        title_immediate_draw_audit.observer(cpu, observed_memory, trace, steps)

    try:
        if native_guest_loop:
            from tools.recomp.native_executor import NativeResumableExecutor

            observer_addresses = {
                *subsystem_initializer_audit.observer_addresses,
                *frontend_registry_audit.observer_addresses,
                *title_d3d_primitive_draw_audit.observer_addresses,
                *title_immediate_draw_audit.observer_addresses,
                TITLE_FRONTEND_COMPARE_SEARCH_ADDRESS,
                TITLE_FRONTEND_RECORD_TABLE_SCAN_ADDRESS,
                TITLE_FRONTEND_POST_AUDIO_LIST_ADVANCE_ADDRESS,
                TITLE_FRONTEND_STATIC_SINGLETON_USE_ADDRESS,
                TITLE_FRONTEND_CRT_VTABLE_INITIALIZER_ADDRESS,
                TITLE_FRONTEND_DYNAMIC_OBJECT_RESET_USE_ADDRESS,
                TITLE_RUNTIME_OBJECT_TABLE_USE_ADDRESS,
                *TITLE_RUNTIME_CALLBACK_DISPATCH_ADDRESSES,
                *(
                    TITLE_MAIN_LOOP_AUDIT_ADDRESSES
                    if audit_title_main_loop_exit
                    else set()
                ),
            }
            native_steps = 0
            returned_to = state.eip
            while max_steps == 0 or native_steps < max_steps:
                if native_frontier_functions:
                    frame = _merge_lifted_functions(
                        entry_function,
                        [
                            *frame_functions,
                            *native_branch_functions,
                            *native_frontier_functions,
                        ],
                        symbol=f"guest_thread_{thread_index:02d}_{start_address:08X}",
                        base_address=start_address,
                    )
                    native_frontier_frame = _merge_lifted_functions(
                        native_frontier_functions[0],
                        native_frontier_functions[1:],
                        symbol=f"guest_thread_{thread_index:02d}_frontier",
                        base_address=native_frontier_functions[0].base_address,
                    )
                else:
                    native_frontier_frame = None
                native = NativeResumableExecutor(
                    frame,
                    build_dir=native_build_dir,
                    # Keep the already-compiled base frame and the recovered
                    # frontier batch in independent native modules. Otherwise
                    # appending one block changes the final large module's
                    # digest and recompiles it synchronously.
                    module_functions=[
                        native_base_frame,
                        *([native_frontier_frame] if native_frontier_frame else []),
                    ],
                    observer_addresses=observer_addresses,
                    callback_addresses=handlers.keys(),
                    memory_callback_addresses={
                        TITLE_AUDIO_DSP_CONTROL_ADDRESS,
                        TITLE_AUDIO_DSP_STATUS_ADDRESS,
                        *TITLE_AUDIO_DSP_VOICE_COMMAND_ADDRESSES,
                        TITLE_MCPX_FRAME_COUNTER_ADDRESS,
                        TITLE_GPU_COMMAND_KICK_ADDRESS,
                        TITLE_GPU_PROGRESS_COUNTER_ADDRESS,
                        TITLE_GPU_SOFTWARE_COMPLETION_FLAG_ADDRESS,
                        TITLE_D3D_CONTEXT_GET_POINTER_ADDRESS,
                        TITLE_GPU_SUBMISSION_BASE_ADDRESS,
                        TITLE_GPU_SUBMISSION_LIMIT_ADDRESS,
                        TITLE_D3D_CONTEXT_GLOBAL_ADDRESS,
                        TITLE_CLEANUP_LIST_SENTINEL_ADDRESS,
                        TITLE_FRONTEND_RESOURCE_CACHE_SENTINEL_ADDRESS,
                        TITLE_FRONTEND_REGISTRY_LIST_SENTINEL_ADDRESS,
                        TITLE_FRONTEND_INITIALIZER_LIST_SENTINEL_ADDRESS,
                        *(
                            {TITLE_MAIN_LOOP_EXIT_FLAG_ADDRESS}
                            if audit_title_main_loop_exit
                            else set()
                        ),
                    },
                    synchronize_eip_for_callbacks=audit_title_main_loop_exit,
                )
                try:
                    returned_to = native.run(
                        state,
                        memory,
                        call_handlers=handlers,
                        step_observer=observe_guest_thread_step,
                        memory_write_observer=(
                            observe_native_memory_write
                            if audit_title_main_loop_exit
                            else None
                        ),
                        max_steps=max_steps - native_steps if max_steps else 0,
                        slice_steps=native_slice_steps if live_host_bridge is not None else 0,
                        yield_handler=live_host_bridge.on_slice if live_host_bridge is not None else None,
                        yield_predicate=(
                            live_host_bridge.should_yield_for_completed_flip
                            if live_host_bridge is not None
                            else None
                        ),
                    )
                finally:
                    native_run_summary = native.last_run_summary
                if native_run_summary is None:
                    break
                native_run_summaries.append(native_run_summary)
                native_steps += int(native_run_summary["steps"])
                if (
                    native_run_summary["reason"] != "unhandled_target"
                    or not _is_executable_address(loaded, returned_to)
                    # Post-title menu initialization legitimately fans out
                    # through well over 64 small recovered blocks. The old
                    # ceiling ended the guest runner at that boundary and
                    # left the presenter showing its last title frame.
                    or len(native_frontier_functions) >= max_dynamic_blocks
                ):
                    break
                recovered = block_loader(returned_to)
                if recovered is None:
                    break
                known_native_addresses = {
                    instruction.address
                    for function in (
                        entry_function,
                        *frame_functions,
                        *native_branch_functions,
                        *native_frontier_functions,
                    )
                    for instruction in function.instructions
                }
                remaining_frontiers = max_dynamic_blocks - len(
                    native_frontier_functions
                )
                recovered_batch = _recover_native_frontier_batch(
                    recovered,
                    block_loader=block_loader,
                    external_targets=set(handlers),
                    covered_addresses=known_native_addresses,
                    max_depth=DEFAULT_INTERNAL_DEPTH,
                    max_blocks=min(
                        DEFAULT_MAX_RECOVERED_BLOCKS,
                        remaining_frontiers,
                    ),
                )
                native_frontier_functions.extend(recovered_batch)
            if live_host_bridge is not None:
                live_host_bridge.on_slice(state, memory, native_steps, True)
            result = ExecutionResult(
                state=state,
                memory=memory,
                trace=ExecutionTrace(enabled=False),
                return_address=returned_to,
                steps=native_steps,
            )
        else:
            result = execute_lifted_function(
                frame,
                state=state,
                memory=memory,
                call_handlers=handlers,
                unhandled_call_handler=_stop_at_internal_call,
                block_loader=block_loader,
                step_observer=observe_guest_thread_step,
                max_steps=max_steps,
                record_instruction_trace=False,
                record_trace=False,
                trace_max_events=4096,
            )
    except RenderWatchpointStop as exc:
        return {
            **summary,
            "status": "render_watchpoint_stop",
            "trace_event_count": 0,
            "trace_tail": [],
            "render_command_stream": _snapshot_render_texture_resources(
                exc.stream,
                render_watchpoint.history_stream(),
                memory,
            ),
            "runtime_abi_invocation_count": bridge.invocation_count
            - invocation_start,
            "executed_internal_targets": [],
            "scheduler_loop_convergence_detector": scheduler_loop_detector.summary(),
            "title_frontend_compare_search_sentinel_repair": (
                frontend_compare_search_repair.summary()
            ),
            "title_frontend_registry_audit": frontend_registry_audit.summary(),
            "title_frontend_record_table_count_repair": frontend_record_table_count_repair.summary(),
            "title_frontend_post_audio_list_repair": frontend_post_audio_list_repair.summary(),
            "title_frontend_static_singleton_repair": frontend_static_singleton_repair.summary(),
            "title_runtime_object_table_constructor_repair": runtime_object_table_repair.summary(),
            "title_runtime_callback_list_repair": runtime_callback_list_repair.summary(),
        }
    except BootProbeStop as exc:
        trace_events = exc.trace.to_list()
        return {
            **summary,
            "status": "blocked_internal_call",
            "target": exc.target,
            "target_hex": _hex32(exc.target),
            "stack_args": [_hex32(argument) for argument in exc.stack_args],
            "state": exc.state.to_dict(),
            "trace_event_count": len(trace_events),
            "trace_tail": _trace_tail(trace_events),
            "render_command_stream": _snapshot_render_texture_resources(
                render_watchpoint.to_stream(),
                render_watchpoint.history_stream(),
                memory,
            ),
            "runtime_abi_invocation_count": bridge.invocation_count
            - invocation_start,
            "runtime_abi_invocations": [
                invocation.to_dict()
                for invocation in bridge.invocations[
                    -min(
                        bridge.invocation_count - invocation_start,
                        len(bridge.invocations),
                    ) :
                ]
            ],
            "native_run": native_run_summary,
            "native_runs": native_run_summaries,
            "native_frontier_recovery": {
                "recovered_block_count": len(native_frontier_functions),
                "recovered_targets": [
                    _hex32(function.base_address)
                    for function in native_frontier_functions
                ],
            },
            "title_subsystem_initializer_audit": subsystem_initializer_audit.summary(),
            "title_frontend_registry_audit": frontend_registry_audit.summary(),
            "scheduler_loop_convergence_detector": scheduler_loop_detector.summary(),
            "title_frontend_compare_search_sentinel_repair": (
                frontend_compare_search_repair.summary()
            ),
            "title_frontend_record_table_count_repair": frontend_record_table_count_repair.summary(),
            "title_frontend_post_audio_list_repair": frontend_post_audio_list_repair.summary(),
            "title_frontend_static_singleton_repair": frontend_static_singleton_repair.summary(),
            "title_runtime_object_table_constructor_repair": runtime_object_table_repair.summary(),
            "title_runtime_callback_list_repair": runtime_callback_list_repair.summary(),
            "executed_internal_targets": _executed_internal_targets(
                trace_events,
                {
                    function.base_address
                    for function in [*internal_functions, *dynamic_functions]
                },
            ),
        }
    except (X86ExecutionError, RuntimeAbiBridgeError, NativeExecutorError) as exc:
        trace_events = exc.trace.to_list() if isinstance(exc, X86ExecutionError) and exc.trace is not None else []
        step_limit_boundary = None
        if _should_classify_execution_stop(exc):
            step_limit_boundary = (
                _gpu_idle_pump_boundary_from_step_limit(
                    trace_events, exc.state, exc.steps
                )
                or _title_render_loop_boundary_from_step_limit(
                    trace_events, exc.state, exc.steps
                )
                or _frontend_resource_boundary_from_step_limit(
                    trace_events, exc.state, exc.steps
                )
                or _scheduler_boundary_from_step_limit(trace_events, exc.state, exc.steps)
                or _heap_free_list_boundary_from_step_limit(
                    trace_events, exc.state, exc.steps
                )
            )
        if step_limit_boundary is not None:
            return {
                **summary,
                **step_limit_boundary,
                "trace_event_count": len(trace_events),
                "trace_tail": _trace_tail(trace_events),
                "render_command_stream": _snapshot_render_texture_resources(
                    render_watchpoint.to_stream(),
                    render_watchpoint.history_stream(),
                    memory,
                ),
                "runtime_abi_invocation_count": bridge.invocation_count
                - invocation_start,
                "scheduler_loop_convergence_detector": (
                    scheduler_loop_detector.summary()
                ),
                "title_frontend_compare_search_sentinel_repair": (
                    frontend_compare_search_repair.summary()
                ),
                "title_frontend_registry_audit": frontend_registry_audit.summary(),
                "title_frontend_record_table_count_repair": frontend_record_table_count_repair.summary(),
                "title_frontend_post_audio_list_repair": frontend_post_audio_list_repair.summary(),
                "title_frontend_static_singleton_repair": frontend_static_singleton_repair.summary(),
                "title_runtime_object_table_constructor_repair": runtime_object_table_repair.summary(),
                "title_runtime_callback_list_repair": runtime_callback_list_repair.summary(),
                "executed_internal_targets": _executed_internal_targets(
                    trace_events,
                    {
                        function.base_address
                        for function in [*internal_functions, *dynamic_functions]
                    },
                ),
            }
        failure: dict[str, Any] = {
            **summary,
            "status": "execution_failed",
            "error": str(exc),
            "runtime_abi_invocation_count": bridge.invocation_count
            - invocation_start,
            "scheduler_loop_convergence_detector": scheduler_loop_detector.summary(),
            "title_frontend_compare_search_sentinel_repair": (
                frontend_compare_search_repair.summary()
            ),
            "title_frontend_registry_audit": frontend_registry_audit.summary(),
            "title_frontend_record_table_count_repair": frontend_record_table_count_repair.summary(),
            "title_frontend_post_audio_list_repair": frontend_post_audio_list_repair.summary(),
            "title_frontend_static_singleton_repair": frontend_static_singleton_repair.summary(),
            "title_runtime_object_table_constructor_repair": runtime_object_table_repair.summary(),
            "title_runtime_callback_list_repair": runtime_callback_list_repair.summary(),
            "native_run": native_run_summary,
            "native_runs": native_run_summaries,
        }
        if isinstance(exc, NativeExecutorError):
            failure["state"] = state.to_dict()
            failure["steps"] = (
                native_run_summary.get("steps", native_steps)
                if native_run_summary is not None
                else native_steps
            )
            failure["render_command_stream"] = _snapshot_render_texture_resources(
                render_watchpoint.to_stream(),
                render_watchpoint.history_stream(),
                memory,
            )
        if isinstance(exc, X86ExecutionError) and exc.trace is not None:
            trace_events = exc.trace.to_list()
            failure["trace_event_count"] = len(trace_events)
            failure["trace_tail"] = _trace_tail(trace_events)
            failure["render_command_stream"] = _snapshot_render_texture_resources(
                render_watchpoint.to_stream(),
                render_watchpoint.history_stream(),
                memory,
            )
            missing_instruction = _missing_instruction_summary(trace_events)
            if missing_instruction is not None:
                failure["invalid_control_target"] = missing_instruction
            if exc.state is not None:
                failure["state"] = exc.state.to_dict()
            failure["steps"] = exc.steps
        return failure

    trace_events = result.trace.to_list()
    returned_to_probe = result.return_address == return_sentinel
    completed = result.return_address == 0
    reached_step_budget = (
        native_run_summary is not None
        and native_run_summary.get("reason") == "step_budget"
    )
    reached_live_stop = (
        native_run_summary is not None
        and native_run_summary.get("reason") == "yield_handler_stop"
    )
    return {
        **summary,
        "status": (
            "live_stop"
            if reached_live_stop
            else "step_budget"
            if reached_step_budget
            else "returned"
            if returned_to_probe
            else "completed"
            if completed
            else "returned_to_guest"
        ),
        "return_address": result.return_address,
        "return_address_hex": _hex32(result.return_address)
        if result.return_address is not None
        else None,
        "steps": result.steps,
        "state": result.state.to_dict(),
        "trace_event_count": len(trace_events),
        "trace_tail": _trace_tail(trace_events),
        "render_command_stream": _snapshot_render_texture_resources(
            render_watchpoint.to_stream(),
            render_watchpoint.history_stream(),
            memory,
        ),
        "runtime_abi_invocation_count": bridge.invocation_count - invocation_start,
        "native_branch_recovery": {
            "recovered_block_count": len(native_branch_functions),
            "recovered_targets": [
                _hex32(function.base_address) for function in native_branch_functions
            ],
        },
        "native_run": native_run_summary,
        "native_runs": native_run_summaries,
        "native_frontier_recovery": {
            "recovered_block_count": len(native_frontier_functions),
            "recovered_targets": [
                _hex32(function.base_address) for function in native_frontier_functions
            ],
        },
        "live_host_bridge": live_host_bridge.summary() if live_host_bridge is not None else None,
        "title_subsystem_initializer_audit": subsystem_initializer_audit.summary(),
        "title_frontend_registry_audit": frontend_registry_audit.summary(),
        "scheduler_loop_convergence_detector": scheduler_loop_detector.summary(),
        "title_frontend_compare_search_sentinel_repair": (
            frontend_compare_search_repair.summary()
        ),
        "title_frontend_record_table_count_repair": frontend_record_table_count_repair.summary(),
        "title_frontend_post_audio_list_repair": frontend_post_audio_list_repair.summary(),
        "title_frontend_static_singleton_repair": frontend_static_singleton_repair.summary(),
        "title_runtime_object_table_constructor_repair": runtime_object_table_repair.summary(),
        "title_runtime_callback_list_repair": runtime_callback_list_repair.summary(),
        "title_main_loop_exit_audit": title_main_loop_exit_audit_summary(),
        "runtime_abi_invocations": [
            invocation.to_dict()
            for invocation in bridge.invocations[
                -min(bridge.invocation_count - invocation_start, len(bridge.invocations)) :
            ]
        ],
        "executed_internal_targets": _executed_internal_targets(
            trace_events,
            {
                function.base_address
                for function in [*internal_functions, *dynamic_functions]
            },
        ),
    }


def _recovered_render_command_stream(
    trace_events: list[dict[str, Any]],
    *,
    max_writes: int = DEFAULT_RENDER_STREAM_MAX_WRITES,
) -> dict[str, Any]:
    writes: list[dict[str, Any]] = []
    mmio_count = 0
    push_buffer_count = 0
    for event in trace_events:
        if event.get("operation") != "memory_write":
            continue
        details = event.get("details", {})
        address = details.get("memory_address")
        value = details.get("value")
        if not isinstance(address, int) or not isinstance(value, int):
            continue
        kind: str | None = None
        offset: int | None = None
        if 0xFED00000 <= address <= 0xFED0FFFF:
            kind = "d3d_mmio"
            offset = address - 0xFED00000
            mmio_count += 1
        elif 0x80000000 <= address <= 0x8000FFFF:
            kind = "d3d_push_buffer"
            offset = address - 0x80000000
            push_buffer_count += 1
        if kind is None:
            continue
        if len(writes) < max_writes:
            size = int(details.get("size", 4))
            clamped_size = max(1, min(size, 8))
            payload = value.to_bytes(clamped_size, "little", signed=False)
            writes.append(
                {
                    "sequence": event.get("sequence"),
                    "instruction_address_hex": event.get("address_hex"),
                    "kind": kind,
                    "address": address,
                    "address_hex": _hex32(address),
                    "offset": offset,
                    "offset_hex": _hex32(offset) if offset is not None else None,
                    "value": value,
                    "value_hex": _hex32(value),
                    "size": size,
                    "bytes_hex": payload.hex().upper(),
                }
            )
    return {
        "write_count": mmio_count + push_buffer_count,
        "mmio_write_count": mmio_count,
        "push_buffer_write_count": push_buffer_count,
        "captured_write_count": len(writes),
        "truncated": mmio_count + push_buffer_count > len(writes),
        "writes": writes,
    }


class SchedulerLoopConvergenceDetector:
    """Stop the current startup scheduler scan after its state has converged."""

    def __init__(
        self,
        *,
        loop_entry: int = TITLE_STARTUP_WORK_QUEUE_LOOP_ENTRY,
        loop_branch: int = TITLE_STARTUP_WORK_QUEUE_LOOP_BRANCH,
        repetitions: int = SCHEDULER_LOOP_CONVERGENCE_REPETITIONS,
    ) -> None:
        self.loop_entry = loop_entry
        self.loop_branch = loop_branch
        self.repetitions = repetitions
        self._last_signature: tuple[int, int, int, int, int] | None = None
        self._repeat_count = 0
        self.detection_count = 0
        self.last_detection: dict[str, Any] | None = None

    def observer(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        trace: ExecutionTrace,
        steps: int,
    ) -> None:
        event = trace.last_event()
        if event is None or event.operation != "branch":
            return
        details = event.details
        if (
            event.address != self.loop_branch
            or not details.get("taken")
            or details.get("target") != self.loop_entry
            or cpu.eip != self.loop_entry
        ):
            self._last_signature = None
            self._repeat_count = 0
            return

        esp = cpu.get_register("esp")
        ecx = cpu.get_register("ecx")
        signature = (
            esp,
            memory.read_u32(_u32(esp + 0x24)),
            ecx,
            cpu.get_register("ebx"),
            memory.read_u32(_u32(ecx - TITLE_STARTUP_WORK_QUEUE_LINK_PAYLOAD_BACK_OFFSET)),
        )
        if signature == self._last_signature:
            self._repeat_count += 1
        else:
            self._last_signature = signature
            self._repeat_count = 1
        if self._repeat_count < self.repetitions:
            return

        detection = {
            "loop_entry": self.loop_entry,
            "loop_entry_hex": _hex32(self.loop_entry),
            "loop_branch": self.loop_branch,
            "loop_branch_hex": _hex32(self.loop_branch),
            "steps": steps,
            "repeat_count": self._repeat_count,
            "required_repetitions": self.repetitions,
            "esp_hex": _hex32(signature[0]),
            "scan_key_hex": _hex32(signature[1]),
            "current_link_hex": _hex32(signature[2]),
            "sentinel_hex": _hex32(signature[3]),
            "current_node_key_hex": _hex32(signature[4]),
        }
        self.detection_count += 1
        self.last_detection = detection
        trace.add(
            self.loop_branch,
            "scheduler_loop_converged",
            detection_count=self.detection_count,
            **detection,
        )
        raise X86ExecutionError(
            SCHEDULER_LOOP_CONVERGENCE_ERROR,
            state=cpu,
            trace=trace,
            steps=steps,
        )

    def summary(self) -> dict[str, Any]:
        return {
            "loop_entry": self.loop_entry,
            "loop_entry_hex": _hex32(self.loop_entry),
            "loop_branch": self.loop_branch,
            "loop_branch_hex": _hex32(self.loop_branch),
            "required_repetitions": self.repetitions,
            "repeat_count": self._repeat_count,
            "detection_count": self.detection_count,
            "last_detection": self.last_detection,
        }


def _scheduler_boundary_from_step_limit(
    trace_events: list[dict[str, Any]],
    state: CpuState | None,
    steps: int | None,
) -> dict[str, Any] | None:
    if not trace_events or state is None or steps is None:
        return None
    for event in reversed(trace_events[-256:]):
        if event.get("operation") != "branch":
            continue
        details = event.get("details", {})
        if not details.get("taken"):
            continue
        target = details.get("target")
        address = event.get("address")
        if not isinstance(target, int) or not isinstance(address, int):
            continue
        if target > address:
            continue
        queue_scan = _scheduler_queue_scan_summary(
            trace_events,
            loop_entry=target,
            state=state,
        )
        if queue_scan["observed_read_count"] < 2:
            continue
        boundary = {
            "status": "scheduler_boundary",
            "boundary_kind": "startup_work_queue_scan",
            "owner": "guest_thread_scheduler",
            "loop_entry": target,
            "loop_entry_hex": _hex32(target),
            "loop_branch": address,
            "loop_branch_hex": _hex32(address),
            "steps": steps,
            "state": state.to_dict(),
            "scheduler_queue_scan": queue_scan,
        }
        convergence = _latest_scheduler_loop_convergence(trace_events)
        if convergence is not None:
            boundary["early_convergence"] = convergence
        return boundary
    return None


def _latest_scheduler_loop_convergence(
    trace_events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for event in reversed(trace_events[-64:]):
        if event.get("operation") != "scheduler_loop_converged":
            continue
        details = event.get("details", {})
        return {
            "sequence": event.get("sequence"),
            "instruction_address_hex": event.get("address_hex"),
            "steps": details.get("steps"),
            "repeat_count": details.get("repeat_count"),
            "required_repetitions": details.get("required_repetitions"),
            "scan_key_hex": details.get("scan_key_hex"),
            "current_link_hex": details.get("current_link_hex"),
            "sentinel_hex": details.get("sentinel_hex"),
            "current_node_key_hex": details.get("current_node_key_hex"),
        }
    return None


def _should_classify_execution_stop(exc: Exception) -> bool:
    return isinstance(exc, X86ExecutionError) and str(exc) in {
        "execution step limit reached",
        SCHEDULER_LOOP_CONVERGENCE_ERROR,
    }


def _title_render_loop_boundary_from_step_limit(
    trace_events: list[dict[str, Any]],
    state: CpuState | None,
    steps: int | None,
) -> dict[str, Any] | None:
    if not trace_events or state is None or steps is None:
        return None
    eip = state.eip
    recent_events = trace_events[-1024:]
    in_quad_submitter = TITLE_QUAD_SUBMIT_ADDRESS <= eip <= TITLE_QUAD_SUBMIT_END_ADDRESS
    if not in_quad_submitter:
        in_quad_submitter = any(
            TITLE_QUAD_SUBMIT_ADDRESS <= int(event.get("address") or 0) <= TITLE_QUAD_SUBMIT_END_ADDRESS
            for event in recent_events
        )
    if not in_quad_submitter:
        return None

    append_events = [
        event
        for event in trace_events
        if event.get("operation") == "title_vertex_append_fast_path"
    ]
    if len(append_events) < 256:
        return None
    stream = _recovered_render_command_stream(trace_events)
    return {
        "status": "render_boundary",
        "boundary_kind": "title_quad_submit_loop",
        "owner": "guest_thread_title_renderer",
        "loop_entry": TITLE_QUAD_SUBMIT_ADDRESS,
        "loop_entry_hex": _hex32(TITLE_QUAD_SUBMIT_ADDRESS),
        "loop_end": TITLE_QUAD_SUBMIT_END_ADDRESS,
        "loop_end_hex": _hex32(TITLE_QUAD_SUBMIT_END_ADDRESS),
        "steps": steps,
        "state": state.to_dict(),
        "title_render_loop": _title_render_loop_summary(
            trace_events,
            append_events=append_events,
            stream=stream,
            state=state,
        ),
    }


def _title_render_loop_summary(
    trace_events: list[dict[str, Any]],
    *,
    append_events: list[dict[str, Any]],
    stream: dict[str, Any],
    state: CpuState,
) -> dict[str, Any]:
    recent_appends = [
        {
            "sequence": event.get("sequence"),
            "record_address_hex": event.get("details", {}).get("record_address_hex"),
            "vertex_index": event.get("details", {}).get("vertex_index"),
            "packed_color_hex": event.get("details", {}).get("packed_color_hex"),
        }
        for event in append_events[-8:]
    ]
    max_vertex_index = max(
        (
            int(event.get("details", {}).get("vertex_index"))
            for event in append_events
            if isinstance(event.get("details", {}).get("vertex_index"), int)
        ),
        default=None,
    )
    return {
        "semantic": "title_screen_quad_render_batch",
        "diagnosis": "recovered renderer is producing a stable title-screen quad stream until the probe step budget expires",
        "quad_submit_address_hex": _hex32(TITLE_QUAD_SUBMIT_ADDRESS),
        "vertex_append_address_hex": _hex32(TITLE_VERTEX_APPEND_ADDRESS),
        "vertex_object_address_hex": _hex32(TITLE_VERTEX_APPEND_OBJECT_ADDRESS),
        "vertex_count_address_hex": _hex32(TITLE_VERTEX_APPEND_COUNT_ADDRESS),
        "quad_submit_invocation_count": sum(
            1
            for event in trace_events
            if event.get("operation") == "instruction"
            and event.get("address") == TITLE_QUAD_SUBMIT_ADDRESS
        ),
        "vertex_append_invocation_count": len(append_events),
        "max_vertex_index": max_vertex_index,
        "current_eip_hex": _hex32(state.eip),
        "render_write_count": stream.get("write_count", 0),
        "d3d_mmio_write_count": stream.get("mmio_write_count", 0),
        "d3d_push_buffer_write_count": stream.get("push_buffer_write_count", 0),
        "recent_appends": recent_appends,
    }


def _gpu_idle_pump_boundary_from_step_limit(
    trace_events: list[dict[str, Any]],
    state: CpuState | None,
    steps: int | None,
) -> dict[str, Any] | None:
    if not trace_events or state is None or steps is None:
        return None
    for event in reversed(trace_events[-512:]):
        if event.get("operation") != "branch":
            continue
        details = event.get("details", {})
        if not details.get("taken"):
            continue
        if (
            event.get("address") != TITLE_GPU_IDLE_PUMP_LOOP_BRANCH
            or details.get("target") != TITLE_GPU_IDLE_PUMP_LOOP_ENTRY
        ):
            continue
        return {
            "status": "hardware_boundary",
            "boundary_kind": "title_gpu_idle_pump",
            "owner": "guest_thread_gpu_service",
            "loop_entry": TITLE_GPU_IDLE_PUMP_LOOP_ENTRY,
            "loop_entry_hex": _hex32(TITLE_GPU_IDLE_PUMP_LOOP_ENTRY),
            "loop_branch": TITLE_GPU_IDLE_PUMP_LOOP_BRANCH,
            "loop_branch_hex": _hex32(TITLE_GPU_IDLE_PUMP_LOOP_BRANCH),
            "steps": steps,
            "state": state.to_dict(),
            "gpu_idle_pump": _gpu_idle_pump_summary(trace_events, state),
        }
    return None


def _gpu_idle_pump_summary(
    trace_events: list[dict[str, Any]],
    state: CpuState,
) -> dict[str, Any]:
    watched_reads = [
        TITLE_GPU_PFIFO_INTERRUPT_STATUS_ADDRESS,
        TITLE_GPU_INTERRUPT_STATUS_ADDRESS,
        0xFD000100,
        0xFD002080,
        0xFD003214,
        0xFD003220,
    ]
    watched_writes = [
        TITLE_GPU_PFIFO_INTERRUPT_STATUS_ADDRESS,
        TITLE_GPU_INTERRUPT_STATUS_ADDRESS,
        0xFD002500,
        0xFD003250,
    ]
    registers = state.to_dict()["registers"]
    return {
        "semantic": "gpu_service_thread_idle_poll",
        "idle_condition": "no modeled PFIFO, CRTC, or master GPU interrupt bit pending",
        "service_call_hex": "0x0021DD9D",
        "pfifo_handler_hex": "0x0021D6E0",
        "loop_esi_hex": registers.get("esi"),
        "loop_edi_hex": registers.get("edi"),
        "loop_ecx_hex": registers.get("ecx"),
        "latest_status_reads": _latest_memory_reads(
            trace_events, watched_reads, limit=12
        ),
        "latest_status_writes": _latest_memory_writes(
            trace_events, watched_writes, limit=12
        ),
    }


def _heap_free_list_boundary_from_step_limit(
    trace_events: list[dict[str, Any]],
    state: CpuState | None,
    steps: int | None,
) -> dict[str, Any] | None:
    if not trace_events or state is None or steps is None:
        return None
    for event in reversed(trace_events[-256:]):
        if event.get("operation") != "jump":
            continue
        details = event.get("details", {})
        target = details.get("target")
        address = event.get("address")
        if (
            target != TITLE_HEAP_FREE_LIST_LOOP_ENTRY
            or address != TITLE_HEAP_FREE_LIST_LOOP_BRANCH
        ):
            continue
        return {
            "status": "heap_free_list_boundary",
            "boundary_kind": "title_heap_free_list_scan",
            "owner": "guest_thread_title_heap",
            "loop_entry": target,
            "loop_entry_hex": _hex32(target),
            "loop_branch": address,
            "loop_branch_hex": _hex32(address),
            "steps": steps,
            "state": state.to_dict(),
            "heap_free_list_scan": _heap_free_list_scan_summary(
                trace_events,
                state=state,
            ),
        }
    return None


def _frontend_resource_boundary_from_step_limit(
    trace_events: list[dict[str, Any]],
    state: CpuState | None,
    steps: int | None,
) -> dict[str, Any] | None:
    if not trace_events or state is None or steps is None:
        return None
    for event in reversed(trace_events[-512:]):
        if event.get("operation") != "branch":
            continue
        details = event.get("details", {})
        if not details.get("taken"):
            continue
        if (
            event.get("address") != TITLE_FRONTEND_RESOURCE_LIST_FIND_LOOP_BRANCH
            or details.get("target") != TITLE_FRONTEND_RESOURCE_LIST_FIND_LOOP_ENTRY
        ):
            continue
        return {
            "status": "asset_boundary",
            "boundary_kind": "frontend_global_dictionary_resource_list",
            "owner": "guest_thread_frontend_assets",
            "loop_entry": TITLE_FRONTEND_RESOURCE_LIST_FIND_LOOP_ENTRY,
            "loop_entry_hex": _hex32(TITLE_FRONTEND_RESOURCE_LIST_FIND_LOOP_ENTRY),
            "loop_branch": TITLE_FRONTEND_RESOURCE_LIST_FIND_LOOP_BRANCH,
            "loop_branch_hex": _hex32(TITLE_FRONTEND_RESOURCE_LIST_FIND_LOOP_BRANCH),
            "steps": steps,
            "state": state.to_dict(),
            "frontend_resource_scan": _frontend_resource_scan_summary(
                trace_events,
                state=state,
            ),
        }
    return None


def _frontend_resource_scan_summary(
    trace_events: list[dict[str, Any]],
    *,
    state: CpuState,
) -> dict[str, Any]:
    sentinel = state.get_register("ebx")
    list_argument = _u32(sentinel - 8)
    current_link = state.get_register("esi")
    candidate_base = state.get_register("edi")
    lookup_key = state.get_register("ebp")
    null_link_underflow = candidate_base == _u32(current_link - 8)
    global_addresses = [
        TITLE_FRONTEND_GLOBAL_RESOURCE_LIST_ADDRESS,
        TITLE_FRONTEND_GLOBAL_RESOURCE_LIST_ADDRESS + 4,
        TITLE_FRONTEND_GLOBAL_RESOURCE_LIST_ADDRESS + 8,
    ]
    reads = _latest_instruction_reads(
        trace_events,
        {
            0x00028B11: "frontend_resource_list_global",
            0x000EB4D1: "list_argument",
            0x000EB4DA: "list_head_next",
            0x000EB4FA: "next_link",
            0x000EB1A3: "candidate_name_first_byte",
            0x000EB1DA: "lookup_key_first_byte",
        },
        limit=8,
        search_window=None,
    )
    global_writes = _latest_memory_writes(trace_events, global_addresses, limit=8)
    return {
        "semantic": "frontend_global_dictionary_lookup_list_walk",
        "resource_path": "Frontend/global.dic",
        "resource_path_address_hex": _hex32(TITLE_FRONTEND_GLOBAL_DIC_PATH_ADDRESS),
        "lookup_key_address": lookup_key,
        "lookup_key_address_hex": _hex32(lookup_key),
        "lookup_key_hint": "impact2"
        if lookup_key == TITLE_FRONTEND_GLOBAL_DIC_IMPACT2_KEY_ADDRESS
        else None,
        "global_resource_list_address_hex": _hex32(
            TITLE_FRONTEND_GLOBAL_RESOURCE_LIST_ADDRESS
        ),
        "list_argument": list_argument,
        "list_argument_hex": _hex32(list_argument),
        "sentinel": sentinel,
        "sentinel_hex": _hex32(sentinel),
        "current_link": current_link,
        "current_link_hex": _hex32(current_link),
        "candidate_base": candidate_base,
        "candidate_base_hex": _hex32(candidate_base),
        "current_link_is_null": current_link == 0,
        "candidate_base_underflow": null_link_underflow,
        "diagnosis": "frontend_resource_list_pointer_is_null"
        if list_argument == 0 and current_link == 0 and null_link_underflow
        else "frontend_resource_list_walk_did_not_converge",
        "reads": reads,
        "global_write_count": len(global_writes),
        "latest_global_writes": global_writes,
    }


def _heap_free_list_scan_summary(
    trace_events: list[dict[str, Any]],
    *,
    state: CpuState,
) -> dict[str, Any]:
    heap_base = state.get_register("ebx")
    list_head = state.get_register("esi")
    current_link = state.get_register("ecx")
    candidate_header = state.get_register("edx")
    free_chunk_units = state.get_register("edi")
    reads = _latest_instruction_reads(
        trace_events,
        {
            0x000E506D: "list_head_next",
            0x000E507C: "free_chunk_units",
            0x000E5080: "candidate_header_size",
            0x000E5085: "next_free_link",
        },
        limit=4,
        search_window=None,
    )
    head_writes = _latest_memory_writes(
        trace_events,
        [list_head, _u32(list_head + 4)],
        limit=8,
    )
    current_link_is_null = current_link == 0
    candidate_header_underflow = candidate_header == _u32(current_link - 8)
    return {
        "semantic": "title_heap_free_insert_sorted_list_walk",
        "heap_base": heap_base,
        "heap_base_hex": _hex32(heap_base),
        "list_head_address": list_head,
        "list_head_address_hex": _hex32(list_head),
        "list_head_offset_hex": _hex32(_u32(list_head - heap_base)),
        "current_link": current_link,
        "current_link_hex": _hex32(current_link),
        "candidate_header": candidate_header,
        "candidate_header_hex": _hex32(candidate_header),
        "free_chunk_units": free_chunk_units,
        "free_chunk_units_hex": _hex32(free_chunk_units),
        "current_link_is_null": current_link_is_null,
        "candidate_header_underflow": candidate_header_underflow,
        "diagnosis": "free_list_head_resolved_to_null"
        if current_link_is_null and candidate_header_underflow
        else "free_list_walk_did_not_converge",
        "expected_empty_list_head_value_hex": _hex32(list_head),
        "head_write_count": len(head_writes),
        "latest_head_writes": head_writes,
        "reads": reads,
    }


def _latest_instruction_reads(
    trace_events: list[dict[str, Any]],
    labels: dict[int, str],
    *,
    limit: int,
    search_window: int | None = 512,
) -> list[dict[str, Any]]:
    reads: list[dict[str, Any]] = []
    seen: set[int] = set()
    events = trace_events if search_window is None else trace_events[-search_window:]
    for event in reversed(events):
        if event.get("operation") != "memory_read":
            continue
        instruction_address = event.get("address")
        if instruction_address not in labels or instruction_address in seen:
            continue
        details = event.get("details", {})
        value = details.get("value")
        memory_address = details.get("memory_address")
        reads.append(
            {
                "label": labels[instruction_address],
                "sequence": event.get("sequence"),
                "instruction_address": instruction_address,
                "instruction_address_hex": _hex32(instruction_address),
                "memory_address": memory_address,
                "memory_address_hex": _hex32(memory_address)
                if isinstance(memory_address, int)
                else None,
                "value": value,
                "value_hex": _hex32(value) if isinstance(value, int) else None,
            }
        )
        seen.add(instruction_address)
        if len(reads) >= limit:
            break
    reads.reverse()
    return reads


def _latest_memory_writes(
    trace_events: list[dict[str, Any]],
    addresses: list[int],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    watched = set(addresses)
    writes: list[dict[str, Any]] = []
    for event in reversed(trace_events):
        if event.get("operation") != "memory_write":
            continue
        details = event.get("details", {})
        address = details.get("memory_address")
        if address not in watched:
            continue
        value = details.get("value")
        writes.append(
            {
                "sequence": event.get("sequence"),
                "instruction_address": event.get("address"),
                "instruction_address_hex": event.get("address_hex"),
                "memory_address": address,
                "memory_address_hex": _hex32(address),
                "value": value,
                "value_hex": _hex32(value) if isinstance(value, int) else None,
                "size": details.get("size", 4),
            }
        )
        if len(writes) >= limit:
            break
    writes.reverse()
    return writes


def _latest_memory_reads(
    trace_events: list[dict[str, Any]],
    addresses: list[int],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    watched = set(addresses)
    reads: list[dict[str, Any]] = []
    for event in reversed(trace_events):
        if event.get("operation") != "memory_read":
            continue
        details = event.get("details", {})
        address = details.get("memory_address")
        if address not in watched:
            continue
        value = details.get("value")
        reads.append(
            {
                "sequence": event.get("sequence"),
                "instruction_address": event.get("address"),
                "instruction_address_hex": event.get("address_hex"),
                "memory_address": address,
                "memory_address_hex": _hex32(address),
                "value": value,
                "value_hex": _hex32(value) if isinstance(value, int) else None,
            }
        )
        if len(reads) >= limit:
            break
    reads.reverse()
    return reads


def _scheduler_queue_scan_summary(
    trace_events: list[dict[str, Any]],
    *,
    loop_entry: int,
    state: CpuState | None = None,
) -> dict[str, Any]:
    labels = {
        loop_entry: "scan_key",
        loop_entry + 0x04: "current_node_key",
        loop_entry + 0x0D: "next_node",
    }
    reads: list[dict[str, Any]] = []
    seen: set[int] = set()
    for event in reversed(trace_events[-512:]):
        if event.get("operation") != "memory_read":
            continue
        instruction_address = event.get("address")
        if instruction_address not in labels or instruction_address in seen:
            continue
        details = event.get("details", {})
        value = details.get("value")
        memory_address = details.get("memory_address")
        reads.append(
            {
                "label": labels[instruction_address],
                "instruction_address": instruction_address,
                "instruction_address_hex": _hex32(instruction_address),
                "memory_address": memory_address,
                "memory_address_hex": _hex32(memory_address)
                if isinstance(memory_address, int)
                else None,
                "value": value,
                "value_hex": _hex32(value) if isinstance(value, int) else None,
            }
        )
        seen.add(instruction_address)
    label_order = {"scan_key": 0, "current_node_key": 1, "next_node": 2}
    reads.sort(key=lambda read: label_order.get(read["label"], 99))
    by_label = {read["label"]: read for read in reads}
    scan_key = by_label.get("scan_key", {}).get("value")
    current_node_key = by_label.get("current_node_key", {}).get("value")
    current_node_key_address = by_label.get("current_node_key", {}).get("memory_address")
    next_node = by_label.get("next_node", {}).get("value")
    next_node_address = by_label.get("next_node", {}).get("memory_address")
    current_node_base = (
        current_node_key_address - 0x08
        if isinstance(current_node_key_address, int)
        else None
    )
    next_pointer_matches_current_node = (
        isinstance(next_node, int)
        and isinstance(current_node_base, int)
        and next_node == current_node_base
    )
    scan_key_matched_current_node = (
        isinstance(scan_key, int)
        and isinstance(current_node_key, int)
        and scan_key == current_node_key
    )
    producer_candidates = _scheduler_producer_write_candidates(
        trace_events,
        scan_key=scan_key if isinstance(scan_key, int) else None,
        watched_addresses=[
            value
            for value in (
                by_label.get("scan_key", {}).get("memory_address"),
                current_node_key_address,
                next_node_address,
            )
            if isinstance(value, int)
        ],
    )
    producer_trace = _scheduler_producer_trace_summary(
        producer_candidates,
        scan_key=scan_key if isinstance(scan_key, int) else None,
        scan_key_source_address=by_label.get("scan_key", {}).get("memory_address"),
        current_node_key_address=current_node_key_address,
        next_node_address=next_node_address,
    )
    node_key_write_values = _unique_hex_values(
        candidate["value"]
        for candidate in producer_candidates
        if candidate.get("memory_address") == current_node_key_address
        and isinstance(candidate.get("value"), int)
    )
    next_node_write_values = _unique_hex_values(
        candidate["value"]
        for candidate in producer_candidates
        if candidate.get("memory_address") == next_node_address
        and isinstance(candidate.get("value"), int)
    )
    state_registers = state.to_dict()["registers"] if state is not None else {}
    return {
        "producer_consumer_hint": "awaiting_work_item_matching_scan_key",
        "observed_read_count": len(reads),
        "scan_key_hex": by_label.get("scan_key", {}).get("value_hex"),
        "scan_key_source_address_hex": by_label.get("scan_key", {}).get(
            "memory_address_hex"
        ),
        "current_node_key_hex": by_label.get("current_node_key", {}).get("value_hex"),
        "next_node_hex": by_label.get("next_node", {}).get("value_hex"),
        "current_node_base_hex": _hex32(current_node_base)
        if current_node_base is not None
        else None,
        "next_pointer_matches_current_node": next_pointer_matches_current_node,
        "scan_key_matched_current_node": scan_key_matched_current_node,
        "awaited_work_item": {
            "key_hex": by_label.get("scan_key", {}).get("value_hex"),
            "status": "not_present_in_observed_scan"
            if not scan_key_matched_current_node
            else "present",
            "observed_node_key_hex": by_label.get("current_node_key", {}).get("value_hex"),
            "observed_node_base_hex": _hex32(current_node_base)
            if current_node_base is not None
            else None,
            "observed_node_self_loop": next_pointer_matches_current_node,
            "loop_esi_hex": state_registers.get("esi"),
            "loop_ebp_hex": state_registers.get("ebp"),
            "loop_edx_hex": state_registers.get("edx"),
            "loop_edi_hex": state_registers.get("edi"),
        },
        "observed_node_key_write_values": node_key_write_values,
        "observed_next_node_write_values": next_node_write_values,
        "producer_candidate_write_count": len(producer_candidates),
        "producer_candidate_writes": producer_candidates,
        "producer_trace": producer_trace,
        "reads": reads,
    }


def _unique_hex_values(values: Any) -> list[str]:
    result: list[str] = []
    for value in values:
        value_hex = _hex32(value)
        if value_hex not in result:
            result.append(value_hex)
    return result


def _scheduler_producer_write_candidates(
    trace_events: list[dict[str, Any]],
    *,
    scan_key: int | None,
    watched_addresses: list[int],
    limit: int = 8,
) -> list[dict[str, Any]]:
    watched = set(watched_addresses)
    candidates: list[dict[str, Any]] = []
    for event in reversed(trace_events):
        if event.get("operation") != "memory_write":
            continue
        details = event.get("details", {})
        address = details.get("memory_address")
        value = details.get("value")
        if not isinstance(address, int) or not isinstance(value, int):
            continue
        reasons: list[str] = []
        if address in watched:
            reasons.append("watched_scheduler_address")
        if scan_key is not None and value == scan_key:
            reasons.append("wrote_scan_key_value")
        if not reasons:
            continue
        candidates.append(
            {
                "sequence": event.get("sequence"),
                "instruction_address": event.get("address"),
                "instruction_address_hex": event.get("address_hex"),
                "memory_address": address,
                "memory_address_hex": _hex32(address),
                "value": value,
                "value_hex": _hex32(value),
                "size": details.get("size", 4),
                "reasons": reasons,
            }
        )
        if len(candidates) >= limit:
            break
    candidates.reverse()
    return candidates


def _scheduler_producer_trace_summary(
    candidates: list[dict[str, Any]],
    *,
    scan_key: int | None,
    scan_key_source_address: Any,
    current_node_key_address: Any,
    next_node_address: Any,
) -> dict[str, Any]:
    scan_key_source = (
        scan_key_source_address if isinstance(scan_key_source_address, int) else None
    )
    current_node_key = (
        current_node_key_address if isinstance(current_node_key_address, int) else None
    )
    next_node = next_node_address if isinstance(next_node_address, int) else None
    grouped: dict[int, dict[str, Any]] = {}
    watched_transitions: dict[int, dict[str, Any]] = {}
    latest_current_node_writer: dict[str, Any] | None = None
    latest_next_node_writer: dict[str, Any] | None = None
    latest_scan_key_source_writer: dict[str, Any] | None = None
    latest_scan_key_value_writer: dict[str, Any] | None = None

    for candidate in candidates:
        address = candidate.get("memory_address")
        instruction = candidate.get("instruction_address")
        value = candidate.get("value")
        if not isinstance(address, int) or not isinstance(instruction, int):
            continue
        roles = _scheduler_candidate_roles(
            candidate,
            scan_key=scan_key,
            scan_key_source_address=scan_key_source,
            current_node_key_address=current_node_key,
            next_node_address=next_node,
        )
        metadata = _scheduler_anchor_metadata(instruction)
        candidate["producer_roles"] = roles
        if metadata:
            candidate["producer_semantic"] = metadata["semantic"]
            candidate["producer_anchor"] = metadata
        transition = watched_transitions.setdefault(
            address,
            {
                "memory_address": address,
                "memory_address_hex": _hex32(address),
                "roles": [],
                "semantics": [],
                "write_count": 0,
                "values_hex": [],
                "latest_sequence": None,
                "latest_instruction_address_hex": None,
            },
        )
        transition["write_count"] += 1
        transition["latest_sequence"] = candidate.get("sequence")
        transition["latest_instruction_address_hex"] = candidate.get(
            "instruction_address_hex"
        )
        for role in roles:
            if role not in transition["roles"]:
                transition["roles"].append(role)
        if metadata and metadata["semantic"] not in transition["semantics"]:
            transition["semantics"].append(metadata["semantic"])
        if isinstance(value, int):
            value_hex = _hex32(value)
            if value_hex not in transition["values_hex"]:
                transition["values_hex"].append(value_hex)

        group = grouped.setdefault(
            instruction,
            {
                "instruction_address": instruction,
                "instruction_address_hex": candidate.get("instruction_address_hex"),
                "write_count": 0,
                "latest_sequence": None,
                "roles": [],
                "semantic": metadata["semantic"] if metadata else "unknown_scheduler_writer",
                "anchor": metadata,
                "watched_addresses_hex": [],
                "values_hex": [],
            },
        )
        group["write_count"] += 1
        group["latest_sequence"] = candidate.get("sequence")
        address_hex = _hex32(address)
        if address_hex not in group["watched_addresses_hex"]:
            group["watched_addresses_hex"].append(address_hex)
        for role in roles:
            if role not in group["roles"]:
                group["roles"].append(role)
        if metadata and group.get("anchor") is None:
            group["anchor"] = metadata
            group["semantic"] = metadata["semantic"]
        if isinstance(value, int):
            value_hex = _hex32(value)
            if value_hex not in group["values_hex"]:
                group["values_hex"].append(value_hex)

        if address == current_node_key:
            latest_current_node_writer = candidate
        if address == next_node:
            latest_next_node_writer = candidate
        if address == scan_key_source:
            latest_scan_key_source_writer = candidate
        if "awaited_key_value_writer" in roles:
            latest_scan_key_value_writer = candidate

    instruction_groups = sorted(
        grouped.values(),
        key=lambda group: (
            int(group["latest_sequence"])
            if isinstance(group.get("latest_sequence"), int)
            else -1
        ),
    )
    transitions = sorted(
        watched_transitions.values(),
        key=lambda transition: transition["memory_address"],
    )
    scan_key_write_count = sum(
        1
        for candidate in candidates
        if "awaited_key_value_writer" in candidate.get("producer_roles", [])
    )
    observed_work_item_key_values = _unique_hex_values(
        candidate["value"]
        for candidate in candidates
        if candidate.get("producer_semantic") == "work_item_key_writer"
        and isinstance(candidate.get("value"), int)
    )
    awaited_key_enqueued = (
        _hex32(scan_key) in observed_work_item_key_values
        if scan_key is not None
        else False
    )
    semantic_counts = Counter(
        str(candidate.get("producer_semantic", "unknown_scheduler_writer"))
        for candidate in candidates
    )
    if latest_scan_key_value_writer is not None:
        status = "awaited_key_seen_in_candidate_writes"
        anchor = _scheduler_recovery_anchor(
            latest_scan_key_value_writer,
            "latest write of awaited scheduler key",
        )
    else:
        status = "awaited_key_not_seen_in_candidate_writes"
        fallback = latest_current_node_writer or latest_next_node_writer or (
            candidates[-1] if candidates else None
        )
        anchor = _scheduler_recovery_anchor(
            fallback,
            "latest watched scheduler slot writer before the wait",
        )
    return {
        "status": status,
        "awaited_key_hex": _hex32(scan_key) if scan_key is not None else None,
        "scan_key_value_write_count": scan_key_write_count,
        "awaited_key_enqueue_observed": awaited_key_enqueued,
        "observed_work_item_key_values": observed_work_item_key_values,
        "key_flow": {
            "consumer_requested_key": {
                "instruction_address_hex": "0x000F2A10",
                "instruction_text": "mov edx, [esp + 0x24]",
                "stack_offset_hex": "0x00000024",
                "register": "edx",
                "value_hex": _hex32(scan_key) if scan_key is not None else None,
            },
            "producer_enqueued_key": {
                "source_instruction_address_hex": "0x000F2A5B",
                "source_instruction_text": "mov ecx, [esp + 0x24]",
                "write_instruction_address_hex": "0x000F2A63",
                "write_instruction_text": "mov [eax + 0x8], ecx",
                "stack_offset_hex": "0x00000024",
                "register": "ecx",
                "observed_values_hex": observed_work_item_key_values,
            },
            "producer_key_source_matches_consumer_offset": True,
            "awaited_key_enqueued_in_observed_trace": awaited_key_enqueued,
            "missing_key_hypothesis": "producer path for awaited key not reached before current scheduler wait"
            if not awaited_key_enqueued
            else "awaited key was produced in the observed bounded trace",
        },
        "producer_semantic_counts": dict(sorted(semantic_counts.items())),
        "queue_node_layout": {
            "key_offset_hex": "0x00000008",
            "next_offset_hex": "0x00000030",
            "inferred_from": [
                "scheduler scan compares [node + 0x8] with requested key",
                "scheduler scan follows [node + 0x30] as next pointer",
                "producer anchor 0x000F2A63 writes [eax + 0x8]",
                "producer anchor 0x000F2AF9 writes [edx + 0x30]",
            ],
        },
        "candidate_instruction_group_count": len(instruction_groups),
        "watched_address_transition_count": len(transitions),
        "latest_scan_key_source_writer": _scheduler_recovery_anchor(
            latest_scan_key_source_writer,
            "latest write to scan-key source address",
        ),
        "latest_current_node_key_writer": _scheduler_recovery_anchor(
            latest_current_node_writer,
            "latest write to observed node key",
        ),
        "latest_next_node_pointer_writer": _scheduler_recovery_anchor(
            latest_next_node_writer,
            "latest write to observed next-node pointer",
        ),
        "next_recovery_anchor": anchor,
        "instruction_groups": instruction_groups,
        "watched_address_transitions": transitions,
    }


def _scheduler_candidate_roles(
    candidate: dict[str, Any],
    *,
    scan_key: int | None,
    scan_key_source_address: int | None,
    current_node_key_address: int | None,
    next_node_address: int | None,
) -> list[str]:
    roles: list[str] = []
    address = candidate.get("memory_address")
    value = candidate.get("value")
    if address == scan_key_source_address:
        roles.append("scan_key_source_writer")
    if address == current_node_key_address:
        roles.append("current_node_key_writer")
    if address == next_node_address:
        roles.append("next_node_pointer_writer")
    if scan_key is not None and value == scan_key:
        roles.append("awaited_key_value_writer")
    return roles


def _scheduler_anchor_metadata(instruction_address: int) -> dict[str, Any] | None:
    metadata = SCHEDULER_PRODUCER_ANCHORS.get(instruction_address)
    if metadata is None:
        return None
    return {
        "instruction_address": instruction_address,
        "instruction_address_hex": _hex32(instruction_address),
        **metadata,
    }


def _scheduler_recovery_anchor(
    candidate: dict[str, Any] | None,
    rationale: str,
) -> dict[str, Any] | None:
    if candidate is None:
        return None
    return {
        "instruction_address": candidate.get("instruction_address"),
        "instruction_address_hex": candidate.get("instruction_address_hex"),
        "sequence": candidate.get("sequence"),
        "memory_address": candidate.get("memory_address"),
        "memory_address_hex": candidate.get("memory_address_hex"),
        "value": candidate.get("value"),
        "value_hex": candidate.get("value_hex"),
        "roles": candidate.get("producer_roles", []),
        "semantic": candidate.get("producer_semantic"),
        "anchor": candidate.get("producer_anchor"),
        "rationale": rationale,
    }


def _trace_tail(trace_events: list[dict[str, Any]], limit: int = 1024) -> list[dict[str, Any]]:
    return trace_events[-limit:]


def _missing_instruction_summary(
    trace_events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for index in range(len(trace_events) - 1, -1, -1):
        event = trace_events[index]
        if event.get("operation") != "missing_instruction":
            continue
        details = event.get("details", {})
        target = details.get("eip")
        summary: dict[str, Any] = {
            "target": target,
            "target_hex": _hex32(target) if isinstance(target, int) else None,
            "sequence": event.get("sequence"),
        }
        for prior in reversed(trace_events[:index]):
            if prior.get("operation") not in {"call", "jump", "return", "branch"}:
                continue
            prior_details = prior.get("details", {})
            control_target = prior_details.get("target")
            if prior.get("operation") == "return":
                control_target = prior_details.get("return_address")
            summary["control_transfer"] = {
                "sequence": prior.get("sequence"),
                "operation": prior.get("operation"),
                "instruction_address": prior.get("address"),
                "instruction_address_hex": prior.get("address_hex"),
                "target": control_target,
                "target_hex": _hex32(control_target)
                if isinstance(control_target, int)
                else prior_details.get("target_hex")
                or prior_details.get("return_address_hex"),
            }
            break
        for prior in reversed(trace_events[:index]):
            if prior.get("operation") != "memory_read":
                continue
            prior_details = prior.get("details", {})
            value = prior_details.get("value")
            if value != target:
                continue
            memory_address = prior_details.get("memory_address")
            summary["target_source_read"] = {
                "sequence": prior.get("sequence"),
                "instruction_address": prior.get("address"),
                "instruction_address_hex": prior.get("address_hex"),
                "memory_address": memory_address,
                "memory_address_hex": _hex32(memory_address)
                if isinstance(memory_address, int)
                else prior_details.get("memory_address_hex"),
                "value": value,
                "value_hex": _hex32(value) if isinstance(value, int) else None,
            }
            break
        return summary
    return None


def _dynamic_recovery_summary(
    dynamic_functions: list[LiftedFunction],
    dynamic_summaries: list[dict[str, Any]],
    dynamic_frontiers: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "dynamic_block_count": len(dynamic_functions),
        "dynamic_block_targets": [
            _hex32(function.base_address) for function in dynamic_functions
        ],
        "dynamic_recovered_blocks": dynamic_summaries,
        "dynamic_frontier_count": len(dynamic_frontiers),
        "dynamic_frontier_status_counts": dict(
            sorted(
                Counter(frontier["status"] for frontier in dynamic_frontiers).items()
            )
        ),
        "dynamic_frontiers": dynamic_frontiers,
    }


def _is_executable_address(loaded: LoadedXbeImage, address: int) -> bool:
    return any(
        region.contains(address) and "execute" in region.permissions
        for region in loaded.arena.regions
    )


def _merge_lifted_functions(
    entry_function: LiftedFunction,
    internal_functions: list[LiftedFunction],
    *,
    symbol: str = "recovered_control_flow_frame",
    base_address: int | None = None,
) -> LiftedFunction:
    instructions = []
    seen_addresses: set[int] = set()
    for function in [entry_function, *internal_functions]:
        for instruction in function.instructions:
            if instruction.address in seen_addresses:
                continue
            seen_addresses.add(instruction.address)
            instructions.append(instruction)
    instructions.sort(key=lambda instruction: instruction.address)
    if instructions:
        code_size = max(instruction.next_address for instruction in instructions) - min(
            instruction.address for instruction in instructions
        )
    else:
        code_size = 0
    return LiftedFunction(
        symbol=symbol,
        base_address=entry_function.base_address
        if base_address is None
        else base_address,
        code_size=code_size,
        instructions=tuple(instructions),
    )


def _recover_missing_branch_targets(
    functions: list[LiftedFunction],
    block_loader: Callable[[int], LiftedFunction | None],
    *,
    start_address: int,
    end_address: int,
) -> list[LiftedFunction]:
    """Close direct branch gaps in a bounded title routine before native emission."""

    known_addresses = {
        instruction.address
        for function in functions
        for instruction in function.instructions
    }
    pending = deque(
        target
        for function in functions
        for target in function.branch_targets
        if start_address <= target < end_address and target not in known_addresses
    )
    recovered: list[LiftedFunction] = []
    attempted: set[int] = set()
    while pending:
        target = pending.popleft()
        if target in attempted or target in known_addresses:
            continue
        attempted.add(target)
        function = block_loader(target)
        if function is None:
            continue
        recovered.append(function)
        known_addresses.update(
            instruction.address for instruction in function.instructions
        )
        pending.extend(
            branch_target
            for branch_target in function.branch_targets
            if start_address <= branch_target < end_address
            and branch_target not in known_addresses
        )
    return recovered


def _lifted_function_summary(
    function: LiftedFunction,
    bridge: RuntimeAbiBridge,
) -> dict[str, Any]:
    return {
        "instruction_count": function.instruction_count,
        "code_size": function.code_size,
        "call_targets": [_hex32(target) for target in function.call_targets],
        "branch_targets": [_hex32(target) for target in function.branch_targets],
        "runtime_call_targets": [
            _hex32(target) for target in function.call_targets if bridge.has_target(target)
        ],
        "internal_call_targets": [
            _hex32(target)
            for target in _internal_call_targets(function, bridge)
        ],
        "first_instruction": function.instructions[0].text()
        if function.instructions
        else None,
        "last_instruction": function.instructions[-1].text()
        if function.instructions
        else None,
    }


def _internal_call_targets(
    function: LiftedFunction,
    bridge: RuntimeAbiBridge,
) -> list[int]:
    targets: list[int] = []
    for target in function.call_targets:
        if bridge.has_target(target) or target in targets:
            continue
        targets.append(target)
    return targets


def _executed_internal_targets(
    trace_events: list[dict[str, Any]],
    internal_function_targets: set[int],
) -> list[str]:
    targets: list[str] = []
    for event in trace_events:
        if event.get("operation") != "call":
            continue
        target = event.get("details", {}).get("target")
        if target not in internal_function_targets:
            continue
        target_hex = _hex32(target)
        if target_hex not in targets:
                targets.append(target_hex)
    return targets


def _asset_io_summary_from_invocations(
    invocations: list[dict[str, Any]],
) -> dict[str, Any]:
    edges: list[dict[str, Any]] = []
    file_handles: dict[int, dict[str, Any]] = {}
    symbolic_link_handles: dict[int, dict[str, Any]] = {}

    for index, invocation in enumerate(invocations):
        shim_name = invocation.get("shim_name")
        result = invocation.get("result")
        if shim_name in {"NtCreateFile", "NtOpenFile"} and isinstance(result, dict):
            status = _coerce_int(result.get("status"))
            handle = _coerce_int(result.get("handle"))
            edge = _asset_edge_base(index, invocation, result, "file_open")
            edge.update(
                {
                    "operation": "file_open"
                    if status == XboxStatus.SUCCESS
                    else "file_probe",
                    "guest_path": result.get("guest_path"),
                    "root_kind": result.get("root_kind"),
                    "mode": result.get("mode"),
                    "handle": handle,
                    "handle_hex": _hex32(handle) if handle is not None else None,
                    "create_disposition": result.get("create_disposition"),
                    "create_options": result.get("create_options"),
                    "desired_access_hex": _hex32(_coerce_int(result.get("desired_access")) or 0),
                }
            )
            if handle is not None and status == XboxStatus.SUCCESS:
                file_handles[handle] = edge
            edges.append(edge)
        elif shim_name == "NtReadFile" and isinstance(result, dict):
            handle = _coerce_int(result.get("handle"))
            opened = file_handles.get(handle, {})
            edge = _asset_edge_base(index, invocation, result, "file_read")
            edge.update(
                {
                    "operation": "file_read",
                    "guest_path": opened.get("guest_path"),
                    "root_kind": opened.get("root_kind"),
                    "handle": handle,
                    "handle_hex": _hex32(handle) if handle is not None else None,
                    "requested_length": result.get("requested_length"),
                    "bytes_read": result.get("bytes_read", 0),
                    "byte_offset": result.get("byte_offset"),
                    "data_elided": bool(result.get("data_elided", False)),
                }
            )
            edges.append(edge)
        elif shim_name == "NtQueryInformationFile" and isinstance(result, dict):
            handle = _coerce_int(result.get("handle"))
            opened = file_handles.get(handle, {})
            edge = _asset_edge_base(index, invocation, result, "file_query_information")
            edge.update(
                {
                    "operation": "file_query_information",
                    "guest_path": result.get("guest_path") or opened.get("guest_path"),
                    "root_kind": opened.get("root_kind"),
                    "handle": handle,
                    "handle_hex": _hex32(handle) if handle is not None else None,
                    "file_information_class": result.get("file_information_class"),
                    "size": result.get("size"),
                    "position": result.get("position"),
                }
            )
            edges.append(edge)
        elif shim_name == "NtSetInformationFile" and isinstance(result, dict):
            handle = _coerce_int(result.get("handle"))
            opened = file_handles.get(handle, {})
            edge = _asset_edge_base(index, invocation, result, "file_set_information")
            edge.update(
                {
                    "operation": "file_set_position"
                    if result.get("file_information_class") == 14
                    else "file_set_information",
                    "guest_path": result.get("guest_path") or opened.get("guest_path"),
                    "root_kind": opened.get("root_kind"),
                    "handle": handle,
                    "handle_hex": _hex32(handle) if handle is not None else None,
                    "file_information_class": result.get("file_information_class"),
                    "position": result.get("position"),
                    "bytes_consumed": result.get("bytes_consumed", 0),
                }
            )
            edges.append(edge)
        elif shim_name == "NtOpenSymbolicLinkObject" and isinstance(result, dict):
            status = _coerce_int(result.get("status"))
            handle = _coerce_int(result.get("handle"))
            edge = _asset_edge_base(index, invocation, result, "symbolic_link_open")
            edge.update(
                {
                    "operation": "symbolic_link_open",
                    "guest_path": result.get("guest_path"),
                    "handle": handle,
                    "handle_hex": _hex32(handle) if handle is not None else None,
                    "target": result.get("target"),
                }
            )
            if handle is not None and status == XboxStatus.SUCCESS:
                symbolic_link_handles[handle] = edge
            edges.append(edge)
        elif shim_name == "NtQuerySymbolicLinkObject" and isinstance(result, dict):
            handle = _coerce_int(result.get("handle"))
            opened = symbolic_link_handles.get(handle, {})
            edge = _asset_edge_base(index, invocation, result, "symbolic_link_query")
            edge.update(
                {
                    "operation": "symbolic_link_query",
                    "guest_path": result.get("name") or opened.get("guest_path"),
                    "handle": handle,
                    "handle_hex": _hex32(handle) if handle is not None else None,
                    "target": result.get("target"),
                }
            )
            edges.append(edge)
        elif shim_name == "NtClose":
            handle = _first_invocation_argument(invocation)
            if handle in file_handles:
                opened = file_handles.pop(handle)
                status = _coerce_int(invocation.get("result"))
                edge = _asset_edge_base(index, invocation, {"status": status}, "file_close")
                edge.update(
                    {
                        "operation": "file_close",
                        "guest_path": opened.get("guest_path"),
                        "root_kind": opened.get("root_kind"),
                        "handle": handle,
                        "handle_hex": _hex32(handle),
                    }
                )
                edges.append(edge)
            elif handle in symbolic_link_handles:
                opened = symbolic_link_handles.pop(handle)
                status = _coerce_int(invocation.get("result"))
                edge = _asset_edge_base(
                    index,
                    invocation,
                    {"status": status},
                    "symbolic_link_close",
                )
                edge.update(
                    {
                        "operation": "symbolic_link_close",
                        "guest_path": opened.get("guest_path"),
                        "handle": handle,
                        "handle_hex": _hex32(handle),
                        "target": opened.get("target"),
                    }
                )
                edges.append(edge)

    status_counts = Counter(edge["status_label"] for edge in edges)
    root_kind_counts = Counter(
        edge.get("root_kind") for edge in edges if edge.get("root_kind")
    )
    operation_counts = Counter(edge["operation"] for edge in edges)
    observed_paths: list[str] = []
    for edge in edges:
        path = edge.get("guest_path")
        if isinstance(path, str) and path not in observed_paths:
            observed_paths.append(path)

    dashupdate_sequence = _dashupdate_asset_sequence(edges)
    symbolic_link_sequence = _symbolic_link_sequence(edges, "\\??\\D:")
    next_edge = _next_asset_edge_after_sequences(
        edges,
        dashupdate_sequence,
        symbolic_link_sequence,
    )
    dashboard_cache_assessment = _dashboard_cache_probe_assessment(edges, next_edge)
    return {
        "format": "b2-recomp-asset-io-summary",
        "public_safe": False,
        "edge_count": len(edges),
        "operation_counts": dict(sorted(operation_counts.items())),
        "status_counts": dict(sorted(status_counts.items())),
        "root_kind_counts": dict(sorted(root_kind_counts.items())),
        "observed_paths": observed_paths,
        "dashupdate_sequence": dashupdate_sequence,
        "symbolic_link_sequence": symbolic_link_sequence,
        "next_edge_after_dashupdate_and_d_link": next_edge,
        "dashboard_cache_probe_assessment": dashboard_cache_assessment,
        "edges": edges,
    }


def _asset_edge_base(
    index: int,
    invocation: dict[str, Any],
    result: dict[str, Any],
    edge_kind: str,
) -> dict[str, Any]:
    status = _coerce_int(result.get("status"))
    return {
        "invocation_index": index,
        "shim_name": invocation.get("shim_name"),
        "edge_kind": edge_kind,
        "status": status,
        "status_hex": _hex32(status) if status is not None else None,
        "status_label": _ntstatus_label(status),
    }


def _dashupdate_asset_sequence(edges: list[dict[str, Any]]) -> dict[str, Any]:
    path = "d:\\dashupdate.xbe"
    sequence = [
        edge
        for edge in edges
        if isinstance(edge.get("guest_path"), str)
        and edge["guest_path"].casefold() == path
    ]
    operations = [edge["operation"] for edge in sequence]
    read_edges = [edge for edge in sequence if edge["operation"] == "file_read"]
    close_edges = [edge for edge in sequence if edge["operation"] == "file_close"]
    complete = (
        "file_open" in operations
        and len(read_edges) >= 2
        and "file_query_information" in operations
        and "file_set_position" in operations
        and bool(close_edges)
        and all(edge.get("status") == XboxStatus.SUCCESS for edge in sequence)
    )
    return {
        "guest_path": path,
        "complete": complete,
        "edge_count": len(sequence),
        "read_count": len(read_edges),
        "bytes_read_total": sum(int(edge.get("bytes_read") or 0) for edge in read_edges),
        "operations": operations,
        "first_invocation_index": sequence[0]["invocation_index"] if sequence else None,
        "close_invocation_index": close_edges[-1]["invocation_index"]
        if close_edges
        else None,
    }


def _symbolic_link_sequence(
    edges: list[dict[str, Any]],
    guest_path: str,
) -> dict[str, Any]:
    sequence = [
        edge
        for edge in edges
        if isinstance(edge.get("guest_path"), str)
        and edge["guest_path"].casefold() == guest_path.casefold()
    ]
    operations = [edge["operation"] for edge in sequence]
    query_edges = [
        edge for edge in sequence if edge["operation"] == "symbolic_link_query"
    ]
    close_edges = [
        edge for edge in sequence if edge["operation"] == "symbolic_link_close"
    ]
    target = query_edges[-1].get("target") if query_edges else None
    complete = (
        "symbolic_link_open" in operations
        and bool(query_edges)
        and bool(close_edges)
        and target == "\\Device\\Cdrom0"
        and all(edge.get("status") == XboxStatus.SUCCESS for edge in sequence)
    )
    return {
        "guest_path": guest_path,
        "complete": complete,
        "edge_count": len(sequence),
        "operations": operations,
        "target": target,
        "query_invocation_index": query_edges[-1]["invocation_index"]
        if query_edges
        else None,
        "close_invocation_index": close_edges[-1]["invocation_index"]
        if close_edges
        else None,
    }


def _next_asset_edge_after_sequences(
    edges: list[dict[str, Any]],
    dashupdate_sequence: dict[str, Any],
    symbolic_link_sequence: dict[str, Any],
) -> dict[str, Any] | None:
    completed_indices = [
        index
        for index in (
            dashupdate_sequence.get("close_invocation_index"),
            symbolic_link_sequence.get("close_invocation_index")
            or symbolic_link_sequence.get("query_invocation_index"),
        )
        if isinstance(index, int)
    ]
    if (
        not dashupdate_sequence.get("complete")
        or not symbolic_link_sequence.get("complete")
        or not completed_indices
    ):
        return None
    boundary = max(completed_indices)
    for edge in edges:
        if edge["invocation_index"] > boundary:
            return dict(edge)
    return None


def _dashboard_cache_probe_assessment(
    edges: list[dict[str, Any]],
    next_edge: dict[str, Any] | None,
) -> dict[str, Any]:
    relevant = [
        edge
        for edge in edges
        if edge.get("root_kind") in {"dashboard", "cache"}
    ]
    clean_not_found = [
        edge
        for edge in relevant
        if edge.get("status_label") == "not_found"
        and edge.get("operation") == "file_probe"
    ]
    unexpected = [
        edge
        for edge in relevant
        if edge.get("status_label") not in {"not_found", "success"}
    ]
    successful = [
        edge for edge in relevant if edge.get("status_label") == "success"
    ]
    observed_paths: list[str] = []
    for edge in relevant:
        path = edge.get("guest_path")
        if isinstance(path, str) and path not in observed_paths:
            observed_paths.append(path)
    if unexpected:
        status = "unexpected_dashboard_or_cache_status"
    elif successful:
        status = "configured_dashboard_or_cache_root_used"
    elif relevant and len(relevant) == len(clean_not_found):
        status = "deterministic_not_found_preserved_for_observed_probes"
    else:
        status = "no_dashboard_or_cache_probe_observed"
    return {
        "status": status,
        "dashboard_probe_count": sum(
            1 for edge in relevant if edge.get("root_kind") == "dashboard"
        ),
        "cache_probe_count": sum(
            1 for edge in relevant if edge.get("root_kind") == "cache"
        ),
        "clean_not_found_probe_count": len(clean_not_found),
        "successful_probe_count": len(successful),
        "unexpected_status_count": len(unexpected),
        "observed_paths": observed_paths,
        "next_post_link_edge_root_kind": next_edge.get("root_kind")
        if isinstance(next_edge, dict)
        else None,
        "next_post_link_edge_status": next_edge.get("status_label")
        if isinstance(next_edge, dict)
        else None,
        "requires_configured_roots_for_observed_edges": bool(successful),
        "later_gameplay_requirement": "unproven_until_execution_advances_past_current_scheduler_boundary",
    }


def _deterministic_service_validation_summary(
    runtime_summary: dict[str, Any],
) -> dict[str, Any]:
    trace_events = runtime_summary.get("trace", [])
    filesystem_reads = [
        event
        for event in trace_events
        if event.get("subsystem") == "filesystem"
        and event.get("operation") == "read_file"
    ]
    filesystem_writes = [
        event
        for event in trace_events
        if event.get("subsystem") == "filesystem"
        and event.get("operation") == "write_file"
    ]
    input_ports = runtime_summary.get("input", {}).get("ports", {})
    audio_streams = runtime_summary.get("audio_streams", [])
    return {
        "streaming": {
            "read_event_count": len(filesystem_reads),
            "bytes_read_total": sum(
                int(event.get("details", {}).get("bytes_read") or 0)
                for event in filesystem_reads
            ),
            "deterministic_latency_100ns_total": sum(
                int(event.get("details", {}).get("deterministic_latency_100ns") or 0)
                for event in filesystem_reads
            ),
            "open_stream_count": sum(
                1 for file in runtime_summary.get("open_files", []) if file.get("streaming")
            ),
        },
        "save_data": {
            "write_event_count": len(filesystem_writes),
            "bytes_written_total": sum(
                int(event.get("details", {}).get("bytes_written") or 0)
                for event in filesystem_writes
            ),
            "open_save_file_count": sum(
                1 for file in runtime_summary.get("open_files", []) if file.get("save_data")
            ),
            "open_cache_file_count": sum(
                1 for file in runtime_summary.get("open_files", []) if file.get("cache_data")
            ),
        },
        "audio": {
            "initialized": bool(runtime_summary.get("audio_initialized", False)),
            "stream_count": len(audio_streams),
            "submitted_buffer_count": sum(
                int(stream.get("submitted_buffer_count") or 0)
                for stream in audio_streams
            ),
            "queued_bytes": sum(int(stream.get("queued_bytes") or 0) for stream in audio_streams),
            "played_bytes": sum(int(stream.get("played_bytes") or 0) for stream in audio_streams),
        },
        "input_latency": {
            "sequence": runtime_summary.get("input", {}).get("sequence", 0),
            "poll_count": sum(
                int(port.get("poll_count") or 0)
                for port in input_ports.values()
                if isinstance(port, dict)
            ),
            "max_last_latency_samples": max(
                (
                    int(port.get("last_latency_samples") or 0)
                    for port in input_ports.values()
                    if isinstance(port, dict)
                ),
                default=0,
            ),
        },
        "clock": runtime_summary.get("clock", {}),
        "determinism": runtime_summary.get("determinism", {}),
    }


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value:
        return int(value, 0)
    return None


def _first_invocation_argument(invocation: dict[str, Any]) -> int | None:
    arguments = invocation.get("arguments")
    if not isinstance(arguments, list) or not arguments:
        return None
    return _coerce_int(arguments[0])


def _ntstatus_label(status: int | None) -> str:
    labels = {
        XboxStatus.SUCCESS: "success",
        XboxStatus.NO_SUCH_FILE: "not_found",
        XboxStatus.OBJECT_NAME_NOT_FOUND: "not_found",
        XboxStatus.END_OF_FILE: "end_of_file",
        XboxStatus.INVALID_HANDLE: "invalid_handle",
        XboxStatus.INVALID_PARAMETER: "invalid_parameter",
        XboxStatus.ACCESS_DENIED: "access_denied",
    }
    return labels.get(status, "unknown")


def _stop_at_internal_call(
    cpu: CpuState,
    memory: SparseMemory,
    target: int,
    trace: ExecutionTrace,
) -> None:
    stack_args = _read_stack_arguments(memory, cpu.get_register("esp"), 8)
    trace.add(
        None,
        "boot_internal_call_gap",
        target=target,
        target_hex=_hex32(target),
        stack_args=[_hex32(argument) for argument in stack_args],
    )
    raise BootProbeStop(target=target, stack_args=stack_args, state=cpu, trace=trace)


def _playability_gaps(
    entry_summary: dict[str, Any],
    unresolved_imports: list[Any],
) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    if unresolved_imports:
        gaps.append(
            {
                "area": "imports",
                "status": "unresolved_imports",
                "count": len(unresolved_imports),
            }
        )
    if entry_summary.get("status") == "decode_failed":
        gaps.append(
            {
                "area": "instruction_coverage",
                "status": "entry_decode_failed",
                "error": entry_summary.get("error"),
            }
        )
    execution = entry_summary.get("execution", {})
    if execution.get("status") == "blocked_internal_call":
        gaps.append(
            {
                "area": "control_flow",
                "status": "internal_call_needs_recovery",
                "target_hex": execution.get("target_hex"),
            }
        )
    elif execution.get("status") == "execution_failed":
        gaps.append(
            {
                "area": "control_flow",
                "status": "execution_failed",
                "error": execution.get("error"),
            }
        )
    for thread in execution.get("guest_thread_executions", []):
        status = thread.get("status")
        if status == "blocked_internal_call":
            gaps.append(
                {
                    "area": "control_flow",
                    "status": "thread_internal_call_needs_recovery",
                    "thread_index": thread.get("thread_index"),
                    "target_hex": thread.get("target_hex"),
                }
            )
        elif status == "execution_failed":
            gaps.append(
                {
                    "area": "control_flow",
                    "status": "thread_execution_failed",
                    "thread_index": thread.get("thread_index"),
                    "start_address_hex": thread.get("start_address_hex"),
                    "error": thread.get("error"),
                }
            )
        elif status == "returned_to_guest":
            gaps.append(
                {
                    "area": "control_flow",
                    "status": "thread_returned_to_guest",
                    "thread_index": thread.get("thread_index"),
                    "start_address_hex": thread.get("start_address_hex"),
                    "return_address_hex": thread.get("return_address_hex"),
                }
            )
        elif status in {
            "scheduler_boundary",
            "heap_free_list_boundary",
            "asset_boundary",
            "render_boundary",
        }:
            gaps.append(
                {
                    "area": "control_flow",
                    "status": f"thread_{status}",
                    "thread_index": thread.get("thread_index"),
                    "start_address_hex": thread.get("start_address_hex"),
                    "boundary_kind": thread.get("boundary_kind"),
                    "loop_entry_hex": thread.get("loop_entry_hex"),
                }
            )
    for frontier in execution.get("dynamic_frontiers", []):
        status = frontier.get("status")
        if status == "decode_failed":
            gaps.append(
                {
                    "area": "instruction_coverage",
                    "status": "dynamic_block_decode_failed",
                    "target_hex": frontier.get("target_hex"),
                    "error": frontier.get("error"),
                }
            )
        elif status == "recovery_limit":
            gaps.append(
                {
                    "area": "control_flow",
                    "status": "dynamic_recovery_limit",
                    "target_hex": frontier.get("target_hex"),
                }
            )
    static_frontiers_block_playability = execution.get("status") != "returned"
    for frontier in entry_summary.get("frontier_batch", []):
        if not static_frontiers_block_playability:
            continue
        status = frontier.get("status")
        if status == "decode_failed":
            gaps.append(
                {
                    "area": "instruction_coverage",
                    "status": "block_decode_failed",
                    "target_hex": frontier.get("target_hex"),
                    "error": frontier.get("error"),
                }
            )
        elif status in {"depth_limit", "recovery_limit"}:
            gaps.append(
                {
                    "area": "control_flow",
                    "status": status,
                    "target_hex": frontier.get("target_hex"),
                    "caller_hex": frontier.get("caller_hex"),
                }
            )
    return gaps


def _read_stack_arguments(
    memory: SparseMemory,
    esp: int,
    count: int,
) -> tuple[int, ...]:
    return tuple(memory.read_u32(_u32(esp + 4 + index * 4)) for index in range(count))


def _read_guest_byte_offset(memory: SparseMemory, address: int) -> int | None:
    if not address:
        return None
    low = memory.read_u32(address)
    high = memory.read_u32(_u32(address + 4))
    value = (high << 32) | low
    if value & (1 << 63):
        value -= 1 << 64
    if value < 0:
        return None
    return min(value, 0x7FFFFFFF)


def _guest_page_protection(protection: int) -> str:
    if protection & 0x40:
        return "rwx"
    if protection & 0x20:
        return "rx"
    if protection & 0x04:
        return "rw"
    return "r"


def _guest_file_mode(desired_access: int, create_disposition: int | None) -> str:
    writable = bool(desired_access & (GENERIC_WRITE | FILE_WRITE_DATA | FILE_APPEND_DATA))
    if create_disposition in {
        FILE_SUPERSEDE,
        FILE_CREATE,
        FILE_OPEN_IF,
        FILE_OVERWRITE,
        FILE_OVERWRITE_IF,
    }:
        writable = True
    if not writable:
        return "rb"
    if create_disposition in {FILE_SUPERSEDE, FILE_OVERWRITE, FILE_OVERWRITE_IF}:
        return "wb"
    return "ab"


def _guest_file_information_payload(
    file_info: dict[str, Any],
    requested_length: int,
) -> bytes:
    if not isinstance(requested_length, int) or requested_length <= 0:
        return b""
    size = max(int(file_info.get("size", 0) or 0), 0)
    position = max(int(file_info.get("position", 0) or 0), 0)
    allocation_size = (size + 0x7FF) & ~0x7FF if size else 0
    is_directory = 1 if file_info.get("is_directory") else 0
    attributes = 0x10 if is_directory else 0x80
    information_class = file_info.get("file_information_class")
    basic_information = (
        struct.pack("<QQQQI", 0, 0, 0, 0, attributes)
        + b"\x00" * 4
    )
    standard_information = struct.pack(
        "<QQIBB2x",
        allocation_size,
        size,
        1,
        0,
        is_directory,
    )
    if information_class == 4:
        payload = basic_information
    elif information_class == 5:
        payload = standard_information
    elif information_class == 14:
        payload = struct.pack("<Q", position)
    elif information_class == 19:
        payload = struct.pack("<Q", allocation_size)
    elif information_class == 20:
        payload = struct.pack("<Q", size)
    elif information_class == 18:
        payload = (
            basic_information
            + standard_information
            + struct.pack("<Q", 0)
            + struct.pack("<I", 0)
        )
    else:
        payload = standard_information
    return payload[: min(requested_length, len(payload))]


def _runtime_abi_result_for_summary(returned_value: Any) -> Any:
    if not isinstance(returned_value, dict):
        return returned_value
    result = dict(returned_value)
    data = result.pop("data", None)
    if isinstance(data, bytes):
        result.setdefault("bytes_read", len(data))
        result["data_elided"] = True
    directory_record = result.pop("directory_record", None)
    if isinstance(directory_record, bytes):
        result["directory_record_bytes"] = len(directory_record)
    volume_information = result.pop("volume_information", None)
    if isinstance(volume_information, bytes):
        result["volume_information_bytes"] = len(volume_information)
    return result


def _prepare_stdcall_return(
    cpu: CpuState,
    memory: SparseMemory,
    stack_cleanup_bytes: int,
) -> None:
    esp = cpu.get_register("esp")
    return_address = memory.read_u32(esp)
    adjusted_esp = _u32(esp + stack_cleanup_bytes)
    memory.write_u32(adjusted_esp, return_address)
    cpu.set_register("esp", adjusted_esp)


def _apply_return_value(cpu: CpuState, returned_value: Any) -> tuple[str, int | None]:
    if returned_value is None:
        return "none", None
    if isinstance(returned_value, bool):
        eax = 1 if returned_value else 0
        cpu.set_register("eax", eax)
        return "bool", eax
    if isinstance(returned_value, int):
        eax = _u32(returned_value)
        cpu.set_register("eax", eax)
        return "int", eax
    if isinstance(returned_value, dict):
        status = returned_value.get("status")
        if isinstance(status, int):
            eax = _u32(status)
            cpu.set_register("eax", eax)
            return "status_dict", eax
        handle = returned_value.get("handle")
        if isinstance(handle, int):
            eax = _u32(handle)
            cpu.set_register("eax", eax)
            return "handle_dict", eax
    return type(returned_value).__name__, None


def _write_runtime_out_u32(
    memory: SparseMemory,
    trace: ExecutionTrace,
    shim_name: str,
    label: str,
    address: int,
    value: int,
) -> dict[str, Any]:
    memory.write_u32(address, value)
    write = {
        "shim_name": shim_name,
        "label": label,
        "address": address,
        "address_hex": _hex32(address),
        "value": _u32(value),
        "value_hex": _hex32(value),
    }
    trace.add(
        None,
        "runtime_abi_memory_write",
        shim_name=shim_name,
        label=label,
        memory_address=address,
        memory_address_hex=_hex32(address),
        value=_u32(value),
        value_hex=_hex32(value),
    )
    return write


def _read_guest_c_string(
    memory: SparseMemory,
    address: int,
    *,
    max_bytes: int = 4096,
) -> bytes:
    payload = bytearray()
    for offset in range(max_bytes):
        byte = memory.read(_u32(address + offset), 1)[0]
        if byte == 0:
            break
        payload.append(byte)
    return bytes(payload)


def _decode_guest_object_path(
    memory: SparseMemory,
    object_attributes_address: int,
) -> dict[str, Any] | None:
    if not object_attributes_address:
        return None
    candidates = [object_attributes_address]
    for offset in (0, 4, 8, 12, 16):
        candidate = memory.read_u32(_u32(object_attributes_address + offset))
        if candidate:
            candidates.append(candidate)
    seen: set[int] = set()
    for ansi_string_address in candidates:
        if ansi_string_address in seen:
            continue
        seen.add(ansi_string_address)
        decoded = _read_guest_ansi_string(memory, ansi_string_address)
        if decoded is not None:
            guest_path, length, maximum_length, buffer = decoded
            return {
                "guest_path": guest_path,
                "object_name_address": ansi_string_address,
                "object_name_address_hex": _hex32(ansi_string_address),
                "object_name_length": length,
                "object_name_maximum_length": maximum_length,
                "object_name_buffer": buffer,
                "object_name_buffer_hex": _hex32(buffer),
            }
    return None


def _read_guest_ansi_string(
    memory: SparseMemory,
    ansi_string_address: int,
    *,
    max_bytes: int = 4096,
    require_path: bool = True,
) -> tuple[str, int, int, int] | None:
    if not ansi_string_address:
        return None
    length = int.from_bytes(memory.read(ansi_string_address, 2), "little")
    maximum_length = int.from_bytes(memory.read(_u32(ansi_string_address + 2), 2), "little")
    buffer = memory.read_u32(_u32(ansi_string_address + 4))
    if length <= 0 or maximum_length < length or maximum_length > max_bytes or not buffer:
        return None
    payload = memory.read(buffer, length)
    try:
        guest_path = payload.decode("ascii")
    except UnicodeDecodeError:
        return None
    if (
        require_path
        and "\\" not in guest_path
        and ":" not in guest_path
        and "/" not in guest_path
    ):
        return None
    return guest_path, length, maximum_length, buffer


def _write_guest_ansi_string_payload(
    memory: SparseMemory,
    trace: ExecutionTrace,
    shim_name: str,
    label: str,
    ansi_string_address: int,
    payload: bytes,
) -> dict[str, Any] | None:
    if not ansi_string_address:
        return None
    maximum_length = int.from_bytes(memory.read(_u32(ansi_string_address + 2), 2), "little")
    buffer = memory.read_u32(_u32(ansi_string_address + 4))
    if maximum_length <= 0 or not buffer:
        return None
    truncated = payload[:maximum_length]
    memory.write(buffer, truncated)
    memory.write(ansi_string_address, len(truncated).to_bytes(2, "little"))
    write = {
        "shim_name": shim_name,
        "label": label,
        "address": buffer,
        "address_hex": _hex32(buffer),
        "descriptor_address": ansi_string_address,
        "descriptor_address_hex": _hex32(ansi_string_address),
        "length": len(truncated),
        "maximum_length": maximum_length,
        "truncated": len(truncated) < len(payload),
    }
    trace.add(
        None,
        "runtime_abi_memory_write",
        shim_name=shim_name,
        label=label,
        memory_address=buffer,
        memory_address_hex=_hex32(buffer),
        descriptor_address=ansi_string_address,
        descriptor_address_hex=_hex32(ansi_string_address),
        length=len(truncated),
        maximum_length=maximum_length,
        truncated=len(truncated) < len(payload),
    )
    return write


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, bytes):
        return {"bytes": len(value), "hex": value.hex().upper()}
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, Path):
        return str(value)
    return value


def _hex32(value: int) -> str:
    return f"0x{value & 0xFFFFFFFF:08X}"


def _u32(value: int) -> int:
    return value & 0xFFFFFFFF


def _align_up_u32(value: int, alignment: int) -> int:
    if alignment <= 0:
        return _u32(value)
    return _u32((value + alignment - 1) // alignment * alignment)


def _parse_int(value: str) -> int:
    return int(value, 0)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe recovered boot/control-flow against runtime shim ABI bindings."
    )
    parser.add_argument("xbe", type=Path, help="Path to the local XBE file to probe.")
    parser.add_argument(
        "--extracted-root",
        type=Path,
        help="Optional extracted disc root used by filesystem shims.",
    )
    parser.add_argument(
        "--save-data-root",
        type=Path,
        help="Optional writable save-data root used by filesystem shims.",
    )
    parser.add_argument(
        "--dashboard-root",
        type=Path,
        help="Optional dashboard volume root for C: / Harddisk0 Partition2 probes.",
    )
    parser.add_argument(
        "--cache-root",
        type=Path,
        help="Optional cache volume root for X:, Y:, and Z: probes.",
    )
    parser.add_argument(
        "--entry-bytes",
        type=_parse_int,
        default=DEFAULT_ENTRY_BYTES,
        help="Maximum bytes to read from the entry point for prefix recovery.",
    )
    parser.add_argument(
        "--max-instructions",
        type=int,
        default=DEFAULT_MAX_INSTRUCTIONS,
        help="Maximum entry instructions to lift before failing.",
    )
    parser.add_argument(
        "--max-block-instructions",
        type=int,
        default=DEFAULT_MAX_BLOCK_INSTRUCTIONS,
        help="Maximum instructions to decode per recovered basic block.",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=DEFAULT_MAX_STEPS,
        help="Maximum lifted entry steps to execute; 0 runs without a step limit.",
    )
    parser.add_argument(
        "--internal-depth",
        type=int,
        default=DEFAULT_INTERNAL_DEPTH,
        help="Maximum direct internal call depth to recover from the entry frame.",
    )
    parser.add_argument(
        "--max-blocks",
        type=int,
        default=DEFAULT_MAX_RECOVERED_BLOCKS,
        help="Maximum recovered basic blocks to batch before reporting frontiers.",
    )
    parser.add_argument(
        "--max-dynamic-blocks",
        type=int,
        default=DEFAULT_MAX_DYNAMIC_BLOCKS,
        help="Maximum executable blocks to decode on demand during execution.",
    )
    parser.add_argument(
        "--dynamic-block-cache",
        type=Path,
        help="Optional ignored JSON cache for dynamically decoded blocks.",
    )
    parser.add_argument(
        "--native-guest-loop",
        action="store_true",
        help="Compile cached guest blocks and execute the title thread as a resumable native loop.",
    )
    parser.add_argument(
        "--native-build-dir",
        type=Path,
        default=Path("build/native-guest-loop"),
        help="Directory for cached native guest-loop sources and DLLs.",
    )
    parser.add_argument(
        "--live-render-stream",
        type=Path,
        help="Atomically publish resumable guest render snapshots for a live Vulkan presenter.",
    )
    parser.add_argument(
        "--live-controller-state",
        type=Path,
        help="Read controller snapshots published by the live Vulkan presenter.",
    )
    parser.add_argument(
        "--live-flip-audit-ack",
        type=Path,
        help=(
            "Yield at every guest flip and wait for this Vulkan acknowledgement; "
            "requires the live render/controller pair."
        ),
    )
    parser.add_argument(
        "--live-flip-audit-health-interval",
        type=int,
        default=30,
        help=(
            "Publish and synchronize every Nth flip in addition to detected "
            "resource/command/flip-value candidates; 0 uses candidates only."
        ),
    )
    parser.add_argument(
        "--live-flip-audit-max-flips",
        type=int,
        default=0,
        help="Ensure this final flip is selected for bounded audit shutdown.",
    )
    parser.add_argument(
        "--native-slice-steps",
        type=int,
        default=2500,
        help="Guest instructions per live render/input exchange.",
    )
    parser.add_argument(
        "--audit-title-main-loop-exit",
        action="store_true",
        help=(
            "Record exact native guest writes overlapping the title main-loop "
            "exit flag; uses an opt-in instrumented native build."
        ),
    )
    parser.add_argument(
        "--render-watchpoint-limit",
        type=int,
        help="Stop execution after retaining this many D3D writes after the capture start.",
    )
    parser.add_argument(
        "--render-watchpoint-start",
        type=int,
        default=0,
        help="Skip this many initial D3D writes before retaining the capture window.",
    )
    parser.add_argument(
        "--no-execute-entry",
        action="store_true",
        help="Only decode the entry prefix; skip bounded execution.",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        help="Optional path for the JSON probe summary.",
    )
    parser.add_argument(
        "--render-stream-output",
        type=Path,
        help="Optional path for the first normalized recovered render stream.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    args = parser.parse_args()
    if (args.live_render_stream is None) != (args.live_controller_state is None):
        parser.error("--live-render-stream and --live-controller-state must be used together")
    if args.live_flip_audit_ack is not None and args.live_render_stream is None:
        parser.error("--live-flip-audit-ack requires the live render/controller pair")
    if args.max_steps < 0:
        parser.error("--max-steps must not be negative")
    if args.native_slice_steps <= 0:
        parser.error("--native-slice-steps must be greater than zero")
    if args.live_flip_audit_health_interval < 0:
        parser.error("--live-flip-audit-health-interval must not be negative")
    if args.live_flip_audit_max_flips < 0:
        parser.error("--live-flip-audit-max-flips must not be negative")

    summary = build_playability_probe_summary(
        args.xbe,
        extracted_root=args.extracted_root,
        entry_bytes=args.entry_bytes,
        max_instructions=args.max_instructions,
        max_block_instructions=args.max_block_instructions,
        execute_entry=not args.no_execute_entry,
        max_steps=args.max_steps,
        internal_depth=args.internal_depth,
        max_blocks=args.max_blocks,
        max_dynamic_blocks=args.max_dynamic_blocks,
        dynamic_block_cache_path=args.dynamic_block_cache,
        render_watchpoint_limit=args.render_watchpoint_limit,
        render_watchpoint_start=args.render_watchpoint_start,
        save_data_root=args.save_data_root,
        dashboard_data_root=args.dashboard_root,
        cache_data_root=args.cache_root,
        native_guest_loop=args.native_guest_loop,
        native_build_dir=args.native_build_dir,
        live_render_stream_path=args.live_render_stream,
        live_controller_state_path=args.live_controller_state,
        live_flip_audit_ack_path=args.live_flip_audit_ack,
        live_flip_audit_health_interval=args.live_flip_audit_health_interval,
        live_flip_audit_max_flips=args.live_flip_audit_max_flips,
        native_slice_steps=args.native_slice_steps,
        audit_title_main_loop_exit=args.audit_title_main_loop_exit,
    )
    output = summary_json(summary, pretty=args.pretty)
    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(output + "\n", encoding="utf-8", newline="\n")
    if args.render_stream_output is not None:
        streams = extract_render_streams_from_probe_summary(summary)
        if not streams:
            raise RuntimeError("probe summary did not contain a recovered render stream")
        write_json(args.render_stream_output, streams[0], pretty=args.pretty)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
