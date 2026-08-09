from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.recomp.ia32_native_backend import (
    DETACHED_GUEST_RESERVATION_END,
    DETACHED_GUEST_RESERVATION_START,
    IA32_VBLANK_RETURN_SENTINEL,
    PHASE6_COVERAGE_GROWTH_CONTRACT_ID,
    PHASE7_ALIGNED_COPY_TAIL_PROOF_BYTES,
    PHASE7_ALIGNED_COPY_TAIL_PROOF_START,
    PHASE7_ALIGNED_COPY_TAIL_SITE,
    PHASE7_ALIGNED_COPY_TAIL_TABLE,
    PHASE7_ALIGNED_COPY_TAIL_TARGETS,
    PHASE7_ASSET_STREAM_CALLBACK_BINDINGS,
    PHASE7_ASSET_STREAM_CALLBACK_RECORD_SLOTS,
    PHASE7_ASSET_STREAM_ENTRY_REFERENCES,
    PHASE7_BASE_MANAGER_OBJECT_BINDINGS,
    PHASE7_BASE_MANAGER_OBJECT_INSTRUCTION_BYTES,
    PHASE7_BASE_MANAGER_OBJECT_VTABLE,
    PHASE7_BASE_MANAGER_OBJECT_VTABLE_ENTRIES,
    PHASE7_CAPTURED_CALLBACK_CELLS,
    PHASE7_CAPTURED_INDIRECT_TARGETS,
    PHASE7_CAPTURED_REGISTER_IMPORT_BINDINGS,
    PHASE7_CAPTURED_SERVICE_IMPORT_CELLS,
    PHASE7_CAPTURED_VTABLE_SLOT_BINDINGS,
    PHASE7_COLLISION_DISPATCH_CALLERS,
    PHASE7_COLLISION_DISPATCH_HELPER_START,
    PHASE7_COLLISION_DISPATCH_SITE,
    PHASE7_COLLISION_DISPATCH_TABLE,
    PHASE7_COLLISION_DISPATCH_TABLE_BASE,
    PHASE7_BB0_CALLBACK_CALLER,
    PHASE7_BB0_CALLBACK_INSTALL,
    PHASE7_BB0_CALLBACK_SITE,
    PHASE7_BB0_CALLBACK_TARGET,
    PHASE7_D150_CALLBACK_CALLERS,
    PHASE7_D150_CALLBACK_TARGETS,
    PHASE7_DIRECTSOUND_REFCOUNT_BINDINGS,
    PHASE7_DIRECTSOUND_REFCOUNT_INSTRUCTION_BYTES,
    PHASE7_DIRECTSOUND_REFCOUNT_VTABLE,
    PHASE7_DIRECTSOUND_VOICE_BINDINGS,
    PHASE7_DIRECTSOUND_VOICE_INSTRUCTION_BYTES,
    PHASE7_DIRECTSOUND_VOICE_VTABLE,
    PHASE7_DIRECTSOUND_VOICE_VTABLE_ENTRIES,
    PHASE7_FUNCTION_PAIR_HELPER_TARGETS,
    PHASE7_FUNCTION_PAIR_TABLE,
    PHASE7_FUNCTION_PAIR_TABLE_BASE,
    PHASE7_GAME_STATE_DERIVED_SITE_COUNT,
    PHASE7_GAME_STATE_DERIVED_SITE_SHA256,
    PHASE7_GAME_STATE_HELPER_BINDINGS,
    PHASE7_GAME_STATE_HELPER_CALLERS,
    PHASE7_GAME_STATE_HELPER_END,
    PHASE7_GAME_STATE_HELPER_SHA256,
    PHASE7_GAME_STATE_HELPER_START,
    PHASE7_GAME_STATE_HELPER_STATE_INDEXES,
    PHASE7_GAME_STATE_OBJECTS,
    PHASE7_GAME_STATE_OBSERVED_BOUNDARY,
    PHASE7_GAME_STATE_REFERENCE_COUNT,
    PHASE7_GAME_STATE_REFERENCE_SHA256,
    PHASE7_GAME_STATE_SITE_COUNT,
    PHASE7_GAME_STATE_SITE_SHA256,
    PHASE7_GAME_STATE_SPECIAL_BINDINGS,
    PHASE7_GAME_STATE_TRANSITION_CODE_END,
    PHASE7_GAME_STATE_TRANSITION_CODE_SHA256,
    PHASE7_GAME_STATE_TRANSITION_CODE_START,
    PHASE7_LEVEL_SELECT_OBJECT,
    PHASE7_LEVEL_SELECT_OBJECT_BINDINGS,
    PHASE7_LEVEL_SELECT_OBJECT_CALLER_BYTES,
    PHASE7_LEVEL_SELECT_OBJECT_POINTER_CELL,
    PHASE7_LEVEL_SELECT_OBJECT_VTABLE,
    PHASE7_LEVEL_SELECT_OBJECT_VTABLE_ENTRIES,
    PHASE7_LEVEL_LOAD_STRATEGY_BINDINGS,
    PHASE7_LEVEL_LOAD_STRATEGY_CALLER_BYTES,
    PHASE7_LEVEL_LOAD_STRATEGY_INSTALLER_BYTES,
    PHASE7_LEVEL_LOAD_STRATEGY_OBJECTS,
    PHASE7_LEVEL_LOAD_STRATEGY_REFERENCE_BYTES,
    PHASE7_LEVEL_LOAD_STRATEGY_SITE_BYTES,
    PHASE7_LEVEL_LOAD_MANAGER_BINDINGS,
    PHASE7_LEVEL_LOAD_MANAGER_CALLER_BYTES,
    PHASE7_LEVEL_LOAD_MANAGER_INSTALLER_BYTES,
    PHASE7_LEVEL_LOAD_MANAGER_OBJECT_BINDINGS,
    PHASE7_LEVEL_LOAD_MANAGER_REFERENCE_BYTES,
    PHASE7_LEVEL_LOAD_MANAGER_SITE_BYTES,
    PHASE7_LEVEL_LOAD_MANAGER_SWEEP_DIRECT_CALLERS,
    PHASE7_LEVEL_LOAD_MANAGER_SWEEP_OBSERVED_BOUNDARY,
    PHASE7_LEVEL_LOAD_MANAGER_SWEEP_PROOF_BYTES,
    PHASE7_LEVEL_LOAD_MANAGER_VTABLES,
    PHASE7_LEVEL_QUERY_BINDINGS,
    PHASE7_LEVEL_QUERY_DIRECT_CALLS,
    PHASE7_LEVEL_QUERY_OBSERVED_BOUNDARY,
    PHASE7_LEVEL_QUERY_OWNER,
    PHASE7_LEVEL_QUERY_PREDICTED_BOUNDARY,
    PHASE7_LEVEL_QUERY_PROOF_BYTES,
    PHASE7_LEVEL_QUERY_POOL_BASE,
    PHASE7_LEVEL_QUERY_POOL_BASE_CELL,
    PHASE7_LEVEL_QUERY_POOL_CAPACITY,
    PHASE7_LEVEL_QUERY_POOL_STRIDE,
    PHASE7_LEVEL_QUERY_POOL_VTABLE,
    PHASE7_LEVEL_QUERY_POOL_VTABLE_ENTRIES,
    PHASE7_LEVEL_QUERY_RECORD_BASE,
    PHASE7_LEVEL_QUERY_RECORD_BASE_CELL,
    PHASE7_LEVEL_QUERY_RECORD_CAPACITY,
    PHASE7_LEVEL_QUERY_RECORD_COUNT_CELL,
    PHASE7_LEVEL_QUERY_RECORD_STRIDE,
    PHASE7_LEVEL_QUERY_RECORD_VTABLE,
    PHASE7_LEVEL_QUERY_RECORD_VTABLE_ENTRIES,
    PHASE7_LEVEL_SAMPLE_DIRECT_CALLS,
    PHASE7_LEVEL_SAMPLE_OBSERVED_BOUNDARY,
    PHASE7_LEVEL_SAMPLE_PRIMARY_BINDINGS,
    PHASE7_LEVEL_SAMPLE_PROOF_BYTES,
    PHASE7_LEVEL_UPDATE_HELPER_CALLS,
    PHASE7_LEVEL_UPDATE_JUMP_TABLE,
    PHASE7_LEVEL_UPDATE_JUMP_TABLE_ADDRESS,
    PHASE7_LEVEL_UPDATE_OBSERVED_BOUNDARIES,
    PHASE7_LEVEL_UPDATE_PRIMARY_BINDINGS,
    PHASE7_LEVEL_UPDATE_PRIMARY_CALLER_BYTES,
    PHASE7_LEVEL_UPDATE_PRIMARY_CELL,
    PHASE7_LEVEL_UPDATE_PRIMARY_OBJECTS,
    PHASE7_LEVEL_UPDATE_PRIMARY_VTABLE,
    PHASE7_LEVEL_UPDATE_SECONDARY_GUARDED_SITES,
    PHASE7_LEVEL_UPDATE_SELECTOR_ADDRESS,
    PHASE7_LEVEL_UPDATE_SELECTOR_BYTES,
    PHASE7_LEVEL_UPDATE_SWITCH_INSTRUCTION_BYTES,
    PHASE7_LEVEL_PARAMETER_BANK_CODE_END,
    PHASE7_LEVEL_PARAMETER_BANK_CODE_SHA256,
    PHASE7_LEVEL_PARAMETER_BANK_CODE_START,
    PHASE7_LEVEL_PARAMETER_BANK_GENERIC_BINDINGS,
    PHASE7_LEVEL_PARAMETER_BANK_OBSERVED_BOUNDARY,
    PHASE7_LEVEL_PARAMETER_BANK_OBJECTS,
    PHASE7_LEVEL_PARAMETER_BANK_SETTER_SITE_COUNT,
    PHASE7_LEVEL_PARAMETER_BANK_SETTER_SITE_SHA256,
    PHASE7_MEMMOVE_JUMP_BINDINGS,
    PHASE7_MEMMOVE_JUMP_CONTROL_BYTES,
    PHASE7_MEMMOVE_JUMP_SITE_BYTES,
    PHASE7_MEMMOVE_JUMP_TABLES,
    PHASE7_RUNTIME_CALLBACK_BINDINGS,
    PHASE7_RUNTIME_CALLBACK_CALLER_BYTES,
    PHASE7_RUNTIME_CALLBACK_INSTALLER_BYTES,
    PHASE7_RUNTIME_CALLBACK_REFERENCE_BYTES,
    PHASE7_RUNTIME_CALLBACK_VECTOR,
    PHASE7_NEW_SAVE_COMPANION_OBJECT,
    PHASE7_NEW_SAVE_COMPANION_OBJECT_INITIAL_POINTER,
    PHASE7_NEW_SAVE_COMPANION_OBJECT_POINTER_CELL,
    PHASE7_NEW_SAVE_COMPANION_OBJECT_VTABLE,
    PHASE7_NEW_SAVE_COMPANION_OBJECT_VTABLE_ENTRIES,
    PHASE7_NEW_SAVE_OBJECT_BINDINGS,
    PHASE7_NEW_SAVE_OBJECT_INSTRUCTION_BYTES,
    PHASE7_NATIVE_AUDIO_LIFETIME_SERVICE_TARGETS,
    PHASE7_NATIVE_BUFFER_CONFIGURATION_SERVICE_TARGETS,
    PHASE7_NATIVE_XINPUT_CONTROL_SERVICE_TARGETS,
    PHASE7_PROFILED_BOOT_SERVICE_SPECS,
    PHASE7_POST_START_OBJECT,
    PHASE7_POST_START_OBJECT_BINDINGS,
    PHASE7_POST_START_OBJECT_INITIAL_POINTER,
    PHASE7_POST_START_OBJECT_INSTRUCTION_BYTES,
    PHASE7_POST_START_OBJECT_POINTER_CELL,
    PHASE7_POST_START_OBJECT_VTABLE,
    PHASE7_POST_START_OBJECT_VTABLE_ENTRIES,
    PHASE7_SAVE_SLOT_OBJECT,
    PHASE7_SAVE_SLOT_OBJECT_BINDINGS,
    PHASE7_SAVE_SLOT_OBJECT_INITIAL_POINTER,
    PHASE7_SAVE_SLOT_OBJECT_INSTRUCTION_BYTES,
    PHASE7_SAVE_SLOT_OBJECT_POINTER_CELL,
    PHASE7_SAVE_SLOT_OBJECT_VTABLE,
    PHASE7_SAVE_SLOT_OBJECT_VTABLE_ENTRIES,
    PHASE7_SAVE_SLOT_OWNER_VTABLE,
    PHASE7_SAVE_SLOT_OWNER_VTABLE_ENTRIES,
    PHASE7_RESOURCE_SELECTION_OBJECT,
    PHASE7_RESOURCE_SELECTION_OBJECT_BINDINGS,
    PHASE7_RESOURCE_SELECTION_OBJECT_CALLER_BYTES,
    PHASE7_RESOURCE_SELECTION_OBJECT_INITIAL_POINTER,
    PHASE7_RESOURCE_SELECTION_OBJECT_POINTER_CELL,
    PHASE7_RESOURCE_SELECTION_OBJECT_VTABLE,
    PHASE7_RESOURCE_SELECTION_OBJECT_VTABLE_ENTRIES,
    PHASE7_RESOURCE_CONVERTER_CALLBACK_BINDINGS,
    PHASE7_RESOURCE_CONVERTER_CALLBACK_VECTOR,
    PHASE7_RESOURCE_CONVERTER_DESCRIPTOR,
    PHASE7_RESOURCE_CONVERTER_DESCRIPTOR_VALUES,
    PHASE7_RESOURCE_CONVERTER_INSTALLER_BINDINGS,
    PHASE7_RESOURCE_CONVERTER_INSTALLER_BYTES,
    PHASE7_RESOURCE_CONVERTER_INSTRUCTION_BYTES,
    PHASE7_RESOURCE_CONVERTER_OBSERVED_BOUNDARY,
    PHASE7_TITLE_INPUT_OBJECT,
    PHASE7_TITLE_INPUT_OBJECT_BINDINGS,
    PHASE7_TITLE_INPUT_OBJECT_INSTRUCTION_BYTES,
    PHASE7_TITLE_INPUT_OBJECT_POINTER_CELL,
    PHASE7_TITLE_INPUT_OBJECT_VTABLE,
    PHASE7_TITLE_INPUT_OBJECT_VTABLE_ENTRIES,
    SUPPORTED_WORKLOAD_SERVICE_SPECS,
    Ia32BackendError,
    Ia32CoverageProfile,
    Ia32DecodedStorePlan,
    _detached_guest_reservation_assembly,
    _normal_live_input_audio_c_source,
    _persistent_c_source,
    _phase6_profile_scheduler_boundaries,
    _phase6_static_jump_table_targets,
    _phase7_base_manager_object_targets,
    _phase7_asset_stream_callback_targets,
    _phase7_aligned_copy_tail_targets,
    _phase7_bb0_callback_targets,
    _phase7_collision_dispatch_targets,
    _phase7_d150_callback_targets,
    _phase7_directsound_refcount_targets,
    _phase7_directsound_voice_targets,
    _phase7_function_pair_helper_targets,
    _phase7_game_state_object_targets,
    _phase7_level_select_object_targets,
    _phase7_level_load_strategy_targets,
    _phase7_level_load_manager_targets,
    _phase7_level_query_targets,
    _phase7_level_sample_primary_targets,
    _phase7_level_update_primary_targets,
    _phase7_level_parameter_bank_targets,
    _phase7_memmove_jump_table_targets,
    _phase7_runtime_callback_vector_targets,
    _phase7_new_save_object_targets,
    _phase7_post_start_object_targets,
    _phase7_save_slot_object_targets,
    _phase7_resource_selection_object_targets,
    _phase7_resource_converter_callback_targets,
    _phase7_title_input_object_targets,
    coverage_profile_from_performance_debug_report,
    ia32_coverage_growth_contract_id,
    load_ia32_coverage_profile,
    plan_ia32_coverage_growth,
)
from tools.recomp.x86_lifter import SparseMemory, X86Instruction, lift_x86_function


def _function_pair_helper_instructions() -> dict[int, X86Instruction]:
    encoded = {
        0x0010A211: "8B6C240C",
        0x0010A21D: "8B7C2410",
        0x0010A22D: "FFD3",
        0x0010A24B: "8B442410",
        0x0010A24F: "8D7CF0F8",
        0x0010A253: "FF5704",
        0x0010A256: "8B442418",
        0x0010A263: "FFD0",
        0x0010A281: "8BF9",
        0x0010A288: "8D74F8F8",
        0x0010A290: "FF5604",
        0x0010A29C: "FFD3",
        0x0010A4A0: "68F0A21000",
        0x0010A4A5: "6A10",
        0x0010A4A7: "6898533400",
        0x0010A4AC: "BBB0A21000",
        0x0010A4BC: "E84FFDFFFF",
        0x0010A50A: "BBF0A21000",
        0x0010A50F: "B910000000",
        0x0010A514: "B898533400",
        0x0010A519: "E862FDFFFF",
    }
    return {
        address: lift_x86_function(
            bytes.fromhex(payload),
            base_address=address,
            symbol=f"function_pair_{address:08x}",
        ).instructions[0]
        for address, payload in encoded.items()
    }


def _base_manager_object_instructions() -> dict[int, X86Instruction]:
    return {
        address: lift_x86_function(
            bytes.fromhex(encoded),
            base_address=address,
            symbol=f"base_manager_object_{address:08x}",
        ).instructions[0]
        for address, encoded in PHASE7_BASE_MANAGER_OBJECT_INSTRUCTION_BYTES.items()
    }


def _base_manager_object_memory(*, corrupt_slot: int | None = None) -> SparseMemory:
    return SparseMemory(
        {
            PHASE7_BASE_MANAGER_OBJECT_VTABLE + index * 4: (
                target ^ 1 if index == corrupt_slot else target
            )
            for index, target in enumerate(PHASE7_BASE_MANAGER_OBJECT_VTABLE_ENTRIES)
        }
    )


def _level_load_strategy_instructions() -> dict[int, X86Instruction]:
    encoded = {
        **PHASE7_LEVEL_LOAD_STRATEGY_REFERENCE_BYTES,
        **PHASE7_LEVEL_LOAD_STRATEGY_SITE_BYTES,
    }
    return {
        address: lift_x86_function(
            bytes.fromhex(payload),
            base_address=address,
            symbol=f"level_load_strategy_{address:08x}",
        ).instructions[0]
        for address, payload in encoded.items()
    }


def _level_load_strategy_memory(
    *,
    corrupt_object: int | None = None,
    corrupt_vtable: int | None = None,
    corrupt_slot: int | None = None,
    corrupt_caller: int | None = None,
    corrupt_installer: int | None = None,
) -> SparseMemory:
    values: dict[int, bytes | int] = {}
    for object_address, vtable, entries in PHASE7_LEVEL_LOAD_STRATEGY_OBJECTS:
        values[object_address] = vtable ^ (1 if object_address == corrupt_object else 0)
        for index, target in enumerate(entries):
            values[vtable + index * 4] = (
                target ^ 1
                if vtable == corrupt_vtable and index == corrupt_slot
                else target
            )
    for address, encoded in PHASE7_LEVEL_LOAD_STRATEGY_CALLER_BYTES.items():
        payload = bytearray.fromhex(encoded)
        if address == corrupt_caller:
            payload[0] ^= 1
        values[address] = bytes(payload)
    for address, encoded in PHASE7_LEVEL_LOAD_STRATEGY_INSTALLER_BYTES.items():
        payload = bytearray.fromhex(encoded)
        if address == corrupt_installer:
            payload[0] ^= 1
        values[address] = bytes(payload)
    return SparseMemory(values)


def _level_load_manager_instructions() -> dict[int, X86Instruction]:
    encoded = {
        **PHASE7_LEVEL_LOAD_MANAGER_REFERENCE_BYTES,
        **PHASE7_LEVEL_LOAD_MANAGER_SITE_BYTES,
    }
    for call_site, (_helper, prefix_start, payload) in (
        PHASE7_LEVEL_LOAD_MANAGER_SWEEP_DIRECT_CALLERS.items()
    ):
        caller = bytes.fromhex(payload)
        offset = call_site - prefix_start
        encoded[call_site] = caller[offset : offset + 5].hex().upper()
    return {
        address: lift_x86_function(
            bytes.fromhex(payload),
            base_address=address,
            symbol=f"level_load_manager_{address:08x}",
        ).instructions[0]
        for address, payload in encoded.items()
    }


def _level_load_manager_memory(
    *,
    corrupt_object: int | None = None,
    corrupt_vtable: int | None = None,
    corrupt_slot: int | None = None,
    corrupt_caller: int | None = None,
    corrupt_installer: int | None = None,
) -> SparseMemory:
    values: dict[int, bytes | int] = {}
    for vtable, entries in PHASE7_LEVEL_LOAD_MANAGER_VTABLES:
        for index, target in enumerate(entries):
            values[vtable + index * 4] = (
                target ^ 1
                if vtable == corrupt_vtable and index == corrupt_slot
                else target
            )
    for object_address, vtable in PHASE7_LEVEL_LOAD_MANAGER_OBJECT_BINDINGS:
        values[object_address] = vtable ^ (1 if object_address == corrupt_object else 0)
    for address, encoded in PHASE7_LEVEL_LOAD_MANAGER_CALLER_BYTES.items():
        payload = bytearray.fromhex(encoded)
        if address == corrupt_caller:
            payload[0] ^= 1
        values[address] = bytes(payload)
    for address, encoded in PHASE7_LEVEL_LOAD_MANAGER_SWEEP_PROOF_BYTES.items():
        values[address] = bytes.fromhex(encoded)
    for _call_site, (_helper, prefix_start, encoded) in (
        PHASE7_LEVEL_LOAD_MANAGER_SWEEP_DIRECT_CALLERS.items()
    ):
        values[prefix_start] = bytes.fromhex(encoded)
    for address, encoded in PHASE7_LEVEL_LOAD_MANAGER_INSTALLER_BYTES.items():
        payload = bytearray.fromhex(encoded)
        if address == corrupt_installer:
            payload[0] ^= 1
        values[address] = bytes(payload)
    return SparseMemory(values)


def _level_update_primary_instructions() -> dict[int, X86Instruction]:
    encoded = dict(PHASE7_LEVEL_UPDATE_SWITCH_INSTRUCTION_BYTES)
    for site, (_helper, caller_start) in PHASE7_LEVEL_UPDATE_PRIMARY_BINDINGS.items():
        caller = bytes.fromhex(PHASE7_LEVEL_UPDATE_PRIMARY_CALLER_BYTES[caller_start])
        offset = site - caller_start
        encoded[site] = caller[offset : offset + 3].hex().upper()
    return {
        address: lift_x86_function(
            bytes.fromhex(payload),
            base_address=address,
            symbol=f"level_update_primary_{address:08x}",
        ).instructions[0]
        for address, payload in encoded.items()
    }


def _level_update_primary_memory(
    *,
    corrupt_cell: bool = False,
    corrupt_selector: bool = False,
    corrupt_jump_table: bool = False,
    corrupt_caller: int | None = None,
) -> SparseMemory:
    memory = _level_load_manager_memory()
    memory.write_u32(
        PHASE7_LEVEL_UPDATE_PRIMARY_CELL,
        0 if corrupt_cell else PHASE7_LEVEL_UPDATE_PRIMARY_OBJECTS[0],
    )
    selector = bytearray.fromhex(PHASE7_LEVEL_UPDATE_SELECTOR_BYTES)
    if corrupt_selector:
        selector[0] ^= 1
    memory.write(PHASE7_LEVEL_UPDATE_SELECTOR_ADDRESS, bytes(selector))
    for index, case_entry in enumerate(PHASE7_LEVEL_UPDATE_JUMP_TABLE):
        memory.write_u32(
            PHASE7_LEVEL_UPDATE_JUMP_TABLE_ADDRESS + index * 4,
            case_entry ^ (1 if corrupt_jump_table and index == 0 else 0),
        )
    for caller_start, encoded in PHASE7_LEVEL_UPDATE_PRIMARY_CALLER_BYTES.items():
        payload = bytearray.fromhex(encoded)
        if caller_start == corrupt_caller:
            payload[0] ^= 1
        memory.write(caller_start, bytes(payload))
    return memory


def _level_sample_primary_instructions() -> dict[int, X86Instruction]:
    encoded = {
        0x00097AB4: "E8A7560000",
        0x0009D679: "FF5218",
        0x0009D697: "FF5018",
        0x0009D6B5: "FF5218",
    }
    return {
        address: lift_x86_function(
            bytes.fromhex(payload),
            base_address=address,
            symbol=f"level_sample_primary_{address:08x}",
        ).instructions[0]
        for address, payload in encoded.items()
    }


def _level_sample_primary_memory(
    *,
    corrupt_cell: int | None = None,
    corrupt_proof: int | None = None,
    corrupt_vtable_slot: int | None = None,
) -> SparseMemory:
    memory = _level_load_manager_memory(
        corrupt_vtable=PHASE7_LEVEL_UPDATE_PRIMARY_VTABLE
        if corrupt_vtable_slot is not None
        else None,
        corrupt_slot=corrupt_vtable_slot,
    )
    for index, object_address in enumerate(PHASE7_LEVEL_UPDATE_PRIMARY_OBJECTS):
        memory.write_u32(
            PHASE7_LEVEL_UPDATE_PRIMARY_CELL + index * 4,
            object_address ^ (1 if corrupt_cell == index else 0),
        )
    for address, encoded in PHASE7_LEVEL_SAMPLE_PROOF_BYTES.items():
        payload = bytearray.fromhex(encoded)
        if address == corrupt_proof:
            payload[0] ^= 1
        memory.write(address, bytes(payload))
    return memory


def _level_query_instructions() -> dict[int, X86Instruction]:
    encoded = {
        0x0001395E: "E84DE90600",
        0x00082741: "E86AD20100",
        0x0009FA60: "E81B9EFDFF",
        0x000798B5: "FF5218",
        0x0007994D: "FF5204",
        0x0009F562: "FF5004",
        0x0009F650: "FF5004",
        0x0009F6AC: "FF5004",
        0x0007A20C: "C70564844B0090D85000",
        0x0007A2D1: "C7056C844B00C0DA5000",
        0x001F4910: "B8C0DA5000",
        0x001F4920: "C700ECF32B00",
        0x001F4950: "B890D85000",
        0x001F4960: "C700A0F32B00",
    }
    return {
        address: lift_x86_function(
            bytes.fromhex(payload),
            base_address=address,
            symbol=f"level_query_{address:08x}",
        ).instructions[0]
        for address, payload in encoded.items()
    }


def _level_query_memory() -> SparseMemory:
    values: dict[int, bytes | int] = {
        address: bytes.fromhex(encoded)
        for address, encoded in PHASE7_LEVEL_QUERY_PROOF_BYTES.items()
    }
    primary_vtable = dict(PHASE7_LEVEL_LOAD_MANAGER_VTABLES)[
        PHASE7_LEVEL_UPDATE_PRIMARY_VTABLE
    ]
    for index, target in enumerate(primary_vtable):
        values[PHASE7_LEVEL_UPDATE_PRIMARY_VTABLE + index * 4] = target
    for index, object_address in enumerate(PHASE7_LEVEL_UPDATE_PRIMARY_OBJECTS):
        values[object_address] = PHASE7_LEVEL_UPDATE_PRIMARY_VTABLE
        values[PHASE7_LEVEL_UPDATE_PRIMARY_CELL + index * 4] = (
            object_address if index == 0 else 0
        )
    values[PHASE7_LEVEL_QUERY_OWNER] = PHASE7_LEVEL_UPDATE_PRIMARY_CELL
    values[PHASE7_LEVEL_QUERY_RECORD_BASE_CELL] = PHASE7_LEVEL_QUERY_RECORD_BASE
    values[PHASE7_LEVEL_QUERY_RECORD_COUNT_CELL] = 2
    for index, target in enumerate(PHASE7_LEVEL_QUERY_RECORD_VTABLE_ENTRIES):
        values[PHASE7_LEVEL_QUERY_RECORD_VTABLE + index * 4] = target
    for index in range(PHASE7_LEVEL_QUERY_RECORD_CAPACITY):
        values[
            PHASE7_LEVEL_QUERY_RECORD_BASE
            + index * PHASE7_LEVEL_QUERY_RECORD_STRIDE
        ] = PHASE7_LEVEL_QUERY_RECORD_VTABLE
    values[PHASE7_LEVEL_QUERY_POOL_BASE_CELL] = PHASE7_LEVEL_QUERY_POOL_BASE
    for index, target in enumerate(PHASE7_LEVEL_QUERY_POOL_VTABLE_ENTRIES):
        values[PHASE7_LEVEL_QUERY_POOL_VTABLE + index * 4] = target
    for index in range(PHASE7_LEVEL_QUERY_POOL_CAPACITY):
        values[
            PHASE7_LEVEL_QUERY_POOL_BASE + index * PHASE7_LEVEL_QUERY_POOL_STRIDE
        ] = PHASE7_LEVEL_QUERY_POOL_VTABLE
    return SparseMemory(values)


def _runtime_callback_vector_instructions() -> dict[int, X86Instruction]:
    return {
        address: lift_x86_function(
            bytes.fromhex(encoded),
            base_address=address,
            symbol=f"runtime_callback_vector_{address:08x}",
        ).instructions[0]
        for address, encoded in PHASE7_RUNTIME_CALLBACK_REFERENCE_BYTES.items()
    }


def _runtime_callback_vector_memory(
    *,
    corrupt_cell: int | None = None,
    corrupt_caller: int | None = None,
    corrupt_installer: int | None = None,
) -> SparseMemory:
    values: dict[int, bytes | int] = {
        cell: target ^ (1 if cell == corrupt_cell else 0)
        for cell, target in PHASE7_RUNTIME_CALLBACK_VECTOR
    }
    for address, encoded in PHASE7_RUNTIME_CALLBACK_CALLER_BYTES.items():
        payload = bytearray.fromhex(encoded)
        if address == corrupt_caller:
            payload[0] ^= 1
        values[address] = bytes(payload)
    for address, encoded in PHASE7_RUNTIME_CALLBACK_INSTALLER_BYTES.items():
        payload = bytearray.fromhex(encoded)
        if address == corrupt_installer:
            payload[0] ^= 1
        values[address] = bytes(payload)
    return SparseMemory(values)


def _memmove_jump_table_instructions() -> dict[int, X86Instruction]:
    return {
        address: lift_x86_function(
            bytes.fromhex(encoded),
            base_address=address,
            symbol=f"memmove_jump_table_{address:08x}",
        ).instructions[0]
        for address, encoded in PHASE7_MEMMOVE_JUMP_SITE_BYTES.items()
    }


def _memmove_jump_table_memory(
    *,
    corrupt_control: int | None = None,
    corrupt_entry: tuple[int, int] | None = None,
) -> SparseMemory:
    values: dict[int, bytes | int] = {}
    for address, encoded in PHASE7_MEMMOVE_JUMP_CONTROL_BYTES.items():
        payload = bytearray.fromhex(encoded)
        if address == corrupt_control:
            payload[0] ^= 1
        values[address] = bytes(payload)
    physical_entries: dict[int, int] = {}
    for table_base, entries in PHASE7_MEMMOVE_JUMP_TABLES.items():
        for index, target in entries:
            physical_entries[table_base + index * 4] = target
    if corrupt_entry is not None:
        table_base, index = corrupt_entry
        physical_entries[table_base + index * 4] ^= 1
    values.update(physical_entries)
    return SparseMemory(values)


def _title_input_object_instructions() -> dict[int, X86Instruction]:
    return {
        address: lift_x86_function(
            bytes.fromhex(encoded),
            base_address=address,
            symbol=f"title_input_object_{address:08x}",
        ).instructions[0]
        for address, encoded in PHASE7_TITLE_INPUT_OBJECT_INSTRUCTION_BYTES.items()
    }


def _title_input_object_memory(*, corrupt_slot: int | None = None) -> SparseMemory:
    values = {
        PHASE7_TITLE_INPUT_OBJECT_POINTER_CELL: PHASE7_TITLE_INPUT_OBJECT,
        PHASE7_TITLE_INPUT_OBJECT: PHASE7_TITLE_INPUT_OBJECT_VTABLE,
    }
    values.update(
        {
            PHASE7_TITLE_INPUT_OBJECT_VTABLE + index * 4: (
                target ^ 1 if index == corrupt_slot else target
            )
            for index, target in enumerate(PHASE7_TITLE_INPUT_OBJECT_VTABLE_ENTRIES)
        }
    )
    return SparseMemory(values)


def _post_start_object_instructions() -> dict[int, X86Instruction]:
    return {
        address: lift_x86_function(
            bytes.fromhex(encoded),
            base_address=address,
            symbol=f"post_start_object_{address:08x}",
        ).instructions[0]
        for address, encoded in PHASE7_POST_START_OBJECT_INSTRUCTION_BYTES.items()
    }


def _post_start_object_memory(*, corrupt_slot: int | None = None) -> SparseMemory:
    values = {
        PHASE7_POST_START_OBJECT_POINTER_CELL: PHASE7_POST_START_OBJECT_INITIAL_POINTER,
        PHASE7_POST_START_OBJECT: PHASE7_POST_START_OBJECT_VTABLE,
    }
    values.update(
        {
            PHASE7_POST_START_OBJECT_VTABLE + index * 4: (
                target ^ 1 if index == corrupt_slot else target
            )
            for index, target in enumerate(PHASE7_POST_START_OBJECT_VTABLE_ENTRIES)
        }
    )
    return SparseMemory(values)


def _new_save_object_instructions() -> dict[int, X86Instruction]:
    return {
        address: lift_x86_function(
            bytes.fromhex(encoded),
            base_address=address,
            symbol=f"new_save_object_{address:08x}",
        ).instructions[0]
        for address, encoded in PHASE7_NEW_SAVE_OBJECT_INSTRUCTION_BYTES.items()
    }


def _new_save_object_memory(
    *,
    corrupt_vtable: int | None = None,
    corrupt_slot: int | None = None,
) -> SparseMemory:
    values = {
        PHASE7_POST_START_OBJECT_POINTER_CELL: PHASE7_POST_START_OBJECT_INITIAL_POINTER,
        PHASE7_POST_START_OBJECT: PHASE7_POST_START_OBJECT_VTABLE,
        PHASE7_NEW_SAVE_COMPANION_OBJECT_POINTER_CELL: (
            PHASE7_NEW_SAVE_COMPANION_OBJECT_INITIAL_POINTER
        ),
        PHASE7_NEW_SAVE_COMPANION_OBJECT: PHASE7_NEW_SAVE_COMPANION_OBJECT_VTABLE,
    }
    for vtable, entries in (
        (PHASE7_POST_START_OBJECT_VTABLE, PHASE7_POST_START_OBJECT_VTABLE_ENTRIES),
        (
            PHASE7_NEW_SAVE_COMPANION_OBJECT_VTABLE,
            PHASE7_NEW_SAVE_COMPANION_OBJECT_VTABLE_ENTRIES,
        ),
    ):
        values.update(
            {
                vtable + index * 4: (
                    target ^ 1
                    if vtable == corrupt_vtable and index == corrupt_slot
                    else target
                )
                for index, target in enumerate(entries)
            }
        )
    return SparseMemory(values)


def _save_slot_object_instructions() -> dict[int, X86Instruction]:
    return {
        address: lift_x86_function(
            bytes.fromhex(encoded),
            base_address=address,
            symbol=f"save_slot_object_{address:08x}",
        ).instructions[0]
        for address, encoded in PHASE7_SAVE_SLOT_OBJECT_INSTRUCTION_BYTES.items()
    }


def _save_slot_object_memory(
    *,
    corrupt_slot: int | None = None,
    corrupt_owner_slot: int | None = None,
) -> SparseMemory:
    values = {
        PHASE7_SAVE_SLOT_OBJECT_POINTER_CELL: PHASE7_SAVE_SLOT_OBJECT_INITIAL_POINTER,
        PHASE7_SAVE_SLOT_OBJECT: PHASE7_SAVE_SLOT_OBJECT_VTABLE,
    }
    values.update(
        {
            PHASE7_SAVE_SLOT_OBJECT_VTABLE + index * 4: (
                target ^ 1 if index == corrupt_slot else target
            )
            for index, target in enumerate(PHASE7_SAVE_SLOT_OBJECT_VTABLE_ENTRIES)
        }
    )
    values.update(
        {
            PHASE7_SAVE_SLOT_OWNER_VTABLE + index * 4: (
                target ^ 1 if index == corrupt_owner_slot else target
            )
            for index, target in enumerate(PHASE7_SAVE_SLOT_OWNER_VTABLE_ENTRIES)
        }
    )
    return SparseMemory(values)


def _resource_selection_object_instructions() -> dict[int, X86Instruction]:
    return {
        site: lift_x86_function(
            bytes.fromhex(PHASE7_RESOURCE_SELECTION_OBJECT_CALLER_BYTES[caller_start])[-3:],
            base_address=site,
            symbol=f"resource_selection_object_{site:08x}",
        ).instructions[0]
        for site, (caller_start, _slot, _target) in (
            PHASE7_RESOURCE_SELECTION_OBJECT_BINDINGS.items()
        )
    }


def _resource_selection_object_memory(
    *,
    corrupt_vtable_slot: int | None = None,
    corrupt_caller: int | None = None,
) -> SparseMemory:
    values: dict[int, bytes | int] = {
        PHASE7_RESOURCE_SELECTION_OBJECT_POINTER_CELL: (
            PHASE7_RESOURCE_SELECTION_OBJECT_INITIAL_POINTER
        ),
        PHASE7_RESOURCE_SELECTION_OBJECT: PHASE7_RESOURCE_SELECTION_OBJECT_VTABLE,
    }
    values.update(
        {
            PHASE7_RESOURCE_SELECTION_OBJECT_VTABLE + index * 4: (
                target ^ 1 if index == corrupt_vtable_slot else target
            )
            for index, target in enumerate(
                PHASE7_RESOURCE_SELECTION_OBJECT_VTABLE_ENTRIES
            )
        }
    )
    for address, encoded in PHASE7_RESOURCE_SELECTION_OBJECT_CALLER_BYTES.items():
        payload = bytearray.fromhex(encoded)
        if address == corrupt_caller:
            payload[0] ^= 1
        values[address] = bytes(payload)
    return SparseMemory(values)


def _level_select_object_instructions() -> dict[int, X86Instruction]:
    return {
        site: lift_x86_function(
            bytes.fromhex(PHASE7_LEVEL_SELECT_OBJECT_CALLER_BYTES[caller_start])[-3:],
            base_address=site,
            symbol=f"level_select_object_{site:08x}",
        ).instructions[0]
        for site, (caller_start, _slot, _target) in PHASE7_LEVEL_SELECT_OBJECT_BINDINGS.items()
    }


def _level_select_object_memory(
    *,
    corrupt_vtable_slot: int | None = None,
    corrupt_caller: int | None = None,
) -> SparseMemory:
    values: dict[int, bytes | int] = {
        PHASE7_LEVEL_SELECT_OBJECT_POINTER_CELL: PHASE7_LEVEL_SELECT_OBJECT,
        PHASE7_LEVEL_SELECT_OBJECT: PHASE7_LEVEL_SELECT_OBJECT_VTABLE,
    }
    values.update(
        {
            PHASE7_LEVEL_SELECT_OBJECT_VTABLE + index * 4: (
                target ^ 1 if index == corrupt_vtable_slot else target
            )
            for index, target in enumerate(PHASE7_LEVEL_SELECT_OBJECT_VTABLE_ENTRIES)
        }
    )
    for address, encoded in PHASE7_LEVEL_SELECT_OBJECT_CALLER_BYTES.items():
        payload = bytearray.fromhex(encoded)
        if address == corrupt_caller:
            payload[0] ^= 1
        values[address] = bytes(payload)
    return SparseMemory(values)


def _directsound_refcount_instructions() -> dict[int, X86Instruction]:
    return {
        address: lift_x86_function(
            bytes.fromhex(encoded),
            base_address=address,
            symbol=f"directsound_refcount_{address:08x}",
        ).instructions[0]
        for address, encoded in PHASE7_DIRECTSOUND_REFCOUNT_INSTRUCTION_BYTES.items()
    }


def _directsound_voice_instructions() -> dict[int, X86Instruction]:
    return {
        address: lift_x86_function(
            bytes.fromhex(encoded),
            base_address=address,
            symbol=f"directsound_voice_{address:08x}",
        ).instructions[0]
        for address, encoded in PHASE7_DIRECTSOUND_VOICE_INSTRUCTION_BYTES.items()
    }


def _directsound_voice_memory(*, corrupt_slot: int | None = None) -> SparseMemory:
    return SparseMemory(
        {
            PHASE7_DIRECTSOUND_VOICE_VTABLE + index * 4: (
                target ^ 1 if index == corrupt_slot else target
            )
            for index, target in enumerate(PHASE7_DIRECTSOUND_VOICE_VTABLE_ENTRIES)
        }
    )


def _function_pair_table_memory(*, corrupt_first_entry: bool = False) -> SparseMemory:
    values = [target for pair in PHASE7_FUNCTION_PAIR_TABLE for target in pair]
    if corrupt_first_entry:
        values[0] ^= 1
    payload = b"".join(value.to_bytes(4, "little") for value in values)
    return SparseMemory(
        {
            PHASE7_FUNCTION_PAIR_TABLE_BASE & ~0xFFF: bytes(PHASE7_FUNCTION_PAIR_TABLE_BASE & 0xFFF)
            + payload
        }
    )


def _d150_callback_instructions() -> dict[int, X86Instruction]:
    encoded = {
        0x0010D16A: "8B6C2418",
        0x0010D177: "FFD5",
        0x0010D1AA: "FFD5",
        0x0010D452: "6800D21000",
        0x0010D4B2: "6860D21000",
        0x0010FF2E: "68F0FA1000",
        0x00110254: "6860FE1000",
        0x001132FD: "68F0F91000",
    }
    instructions = {
        address: lift_x86_function(
            bytes.fromhex(payload),
            base_address=address,
            symbol=f"d150_callback_{address:08x}",
        ).instructions[0]
        for address, payload in encoded.items()
    }
    for address in (0x0010D1BE, *PHASE7_D150_CALLBACK_CALLERS):
        displacement = (0x0010D150 - (address + 5)) & 0xFFFFFFFF
        instructions[address] = lift_x86_function(
            b"\xe8" + displacement.to_bytes(4, "little"),
            base_address=address,
            symbol=f"d150_caller_{address:08x}",
        ).instructions[0]
    return instructions


def _bb0_callback_instructions() -> dict[int, X86Instruction]:
    encoded = {
        0x00109BD5: "68B09B1000",
        0x00112BC0: "8B7C2418",
        0x00112BCE: "FFD7",
    }
    instructions = {
        address: lift_x86_function(
            bytes.fromhex(payload),
            base_address=address,
            symbol=f"bb0_callback_{address:08x}",
        ).instructions[0]
        for address, payload in encoded.items()
    }
    displacement = (0x00112BB0 - (PHASE7_BB0_CALLBACK_CALLER + 5)) & 0xFFFFFFFF
    instructions[PHASE7_BB0_CALLBACK_CALLER] = lift_x86_function(
        b"\xe8" + displacement.to_bytes(4, "little"),
        base_address=PHASE7_BB0_CALLBACK_CALLER,
        symbol="bb0_callback_caller",
    ).instructions[0]
    return instructions


def _asset_stream_callback_instructions() -> dict[int, X86Instruction]:
    encoded = {
        0x0011052D: "C7460C80031100",
        0x00110534: "C7461040041100",
        0x0011053B: "C7461450041100",
        0x00110542: "C7461890041100",
        0x00112E45: "8B460C",
        0x00112E48: "8B4010",
        0x00112E50: "FFD0",
        0x001130AB: "8B450C",
        0x001130B9: "8B400C",
        0x001130C6: "FFD0",
        0x00113109: "8B450C",
        0x0011310C: "8B4014",
        0x0011311B: "89442414",
        0x001131BD: "FF542424",
    }
    instructions = {
        address: lift_x86_function(
            bytes.fromhex(payload),
            base_address=address,
            symbol=f"asset_stream_callback_{address:08x}",
        ).instructions[0]
        for address, payload in encoded.items()
    }
    for entry, references in PHASE7_ASSET_STREAM_ENTRY_REFERENCES.items():
        for address in references:
            opcode = b"\xe9" if address == 0x001102B0 else b"\xe8"
            displacement = (entry - (address + 5)) & 0xFFFFFFFF
            instructions[address] = lift_x86_function(
                opcode + displacement.to_bytes(4, "little"),
                base_address=address,
                symbol=f"asset_stream_entry_reference_{address:08x}",
            ).instructions[0]
    return instructions


def _resource_converter_callback_memory() -> SparseMemory:
    memory = SparseMemory(
        {
            PHASE7_RESOURCE_CONVERTER_DESCRIPTOR + index * 4: value
            for index, value in enumerate(
                PHASE7_RESOURCE_CONVERTER_DESCRIPTOR_VALUES
            )
        }
    )
    memory.write(
        0x00105BE0,
        bytes.fromhex(PHASE7_RESOURCE_CONVERTER_INSTALLER_BYTES),
    )
    return memory


def _resource_converter_callback_instructions() -> dict[int, X86Instruction]:
    encoded = dict(PHASE7_RESOURCE_CONVERTER_INSTRUCTION_BYTES)
    encoded.update(
        {
            0x00105BE7: "C70030581000",
            0x00105BED: "C74004C0581000",
            0x00105BF4: "C7400850541000",
            0x00105BFB: "C7400CA0611000",
        }
    )
    return {
        address: lift_x86_function(
            bytes.fromhex(payload),
            base_address=address,
            symbol=f"resource_converter_callback_{address:08x}",
        ).instructions[0]
        for address, payload in encoded.items()
    }


def _collision_dispatch_memory(
    *, corrupt_helper: bool = False, corrupt_table: bool = False
) -> SparseMemory:
    helper = bytearray.fromhex(
        "8B4424088B4C24103BC18D04818B0485486E3300"
        "C70590224F0001000000C70594224F0001000000"
        "7D0E85C074278B4C240C8B542404EB0C85C07419"
        "8B4C24048B54240C5152FFD083C40885C07406B8"
        "01000000C333C0C3"
    )
    table = bytearray(
        b"".join(
            target.to_bytes(4, "little")
            for row in PHASE7_COLLISION_DISPATCH_TABLE
            for target in row
        )
    )
    if corrupt_helper:
        helper[0] ^= 1
    if corrupt_table:
        table[0] ^= 1
    return SparseMemory(
        {
            PHASE7_COLLISION_DISPATCH_HELPER_START: bytes(helper),
            PHASE7_COLLISION_DISPATCH_TABLE_BASE: bytes(table),
        }
    )


def _aligned_copy_tail_memory(
    *, corrupt_proof: bool = False, corrupt_table: bool = False
) -> SparseMemory:
    proof = bytearray.fromhex(PHASE7_ALIGNED_COPY_TAIL_PROOF_BYTES)
    table = bytearray(
        b"\0\0\0\0"
        + b"".join(
            target.to_bytes(4, "little")
            for target in PHASE7_ALIGNED_COPY_TAIL_TARGETS
        )
    )
    if corrupt_proof:
        proof[0] ^= 1
    if corrupt_table:
        table[4] ^= 1
    return SparseMemory(
        {
            PHASE7_ALIGNED_COPY_TAIL_PROOF_START: bytes(proof),
            PHASE7_ALIGNED_COPY_TAIL_TABLE: bytes(table),
        }
    )


def _aligned_copy_tail_instructions() -> dict[int, X86Instruction]:
    return {
        PHASE7_ALIGNED_COPY_TAIL_SITE: lift_x86_function(
            bytes.fromhex("FF249DD4B62800"),
            base_address=PHASE7_ALIGNED_COPY_TAIL_SITE,
            symbol="aligned_copy_tail_site",
        ).instructions[0]
    }


def _collision_dispatch_instructions() -> dict[int, X86Instruction]:
    instructions = {
        PHASE7_COLLISION_DISPATCH_SITE: lift_x86_function(
            bytes.fromhex("FFD0"),
            base_address=PHASE7_COLLISION_DISPATCH_SITE,
            symbol="collision_dispatch_site",
        ).instructions[0]
    }
    for caller in PHASE7_COLLISION_DISPATCH_CALLERS:
        displacement = (PHASE7_COLLISION_DISPATCH_HELPER_START - (caller + 5)) & 0xFFFFFFFF
        instructions[caller] = lift_x86_function(
            b"\xe8" + displacement.to_bytes(4, "little"),
            base_address=caller,
            symbol=f"collision_dispatch_caller_{caller:08x}",
        ).instructions[0]
    return instructions


class Ia32CoverageGrowthTests(unittest.TestCase):
    def test_normal_live_audio_lane_uses_sample_clock_pacing(self) -> None:
        source = _normal_live_input_audio_c_source()

        self.assertIn("QueryPerformanceFrequency(&frequency)", source)
        self.assertIn("interval = (frequency + 10u) / 20u", source)
        self.assertIn("next_deadline += interval", source)
        self.assertNotIn("Sleep(45u)", source)

    def setUp(self) -> None:
        functions = tuple(
            lift_x86_function(b"\x90\xc3", base_address=address, symbol=f"target_{index}")
            for index, address in enumerate((0x000C0000, 0x000C0100, 0x000C0200, 0x000C0300))
        )
        self.plan = Ia32DecodedStorePlan(
            xbe_sha256="xbe",
            store_snapshot_id="store",
            inputs={
                "coverage_map_id": "coverage",
                "rewrite_manifest_id": "rewrites",
            },
            functions=functions,
            coverage_map={"complete": True, "unsupported_sites": []},
            section_map={},
            direct_edge_relocations={},
            indirect_target_table={},
            rewrite_manifest={},
        )

    def profile(self, *, mode: str = "normal-validation") -> Ia32CoverageProfile:
        return Ia32CoverageProfile(
            workload="unit-lesson-one",
            mode=mode,
            targets=(
                {
                    "address": 0x000C0100,
                    "slice": "lesson-one-hot-scc",
                    "lane": "primary",
                    "estimated_native_time_ns": 1000,
                    "guest_steps": 500,
                    "module_calls": 100,
                },
                {
                    "address": 0x000C0200,
                    "slice": "lesson-one-hot-scc",
                    "lane": "primary",
                    "estimated_native_time_ns": 1000,
                    "guest_steps": 500,
                    "module_calls": 100,
                },
                {
                    "address": 0x000C0000,
                    "slice": "boot-frontend-continuity",
                    "lane": "primary",
                    "estimated_native_time_ns": 1,
                    "guest_steps": 1,
                    "module_calls": 1,
                },
                {
                    "address": 0x000C0300,
                    "slice": "worker-vblank-cold",
                    "lane": "worker",
                    "estimated_native_time_ns": 1,
                    "guest_steps": 1,
                    "module_calls": 1,
                },
            ),
            transitions=(
                {"source": 0x000C0100, "target": 0x000C0200, "count": 9},
                {"source": 0x000C0200, "target": 0x000C0100, "count": 8},
                {"source": 0x000C0000, "target": 0x000C0100, "count": 1},
                {"source": 0x000C0200, "target": 0x000C0300, "count": 1},
            ),
            boundary_exits=(
                {
                    "source": 0x000C0100,
                    "target": 0xE0001000,
                    "boundary": "service",
                    "count": 9,
                },
                {"source": 0x000C0200, "boundary": "render", "count": 9},
            ),
        )

    def test_contract_identity_is_frozen(self) -> None:
        self.assertEqual(ia32_coverage_growth_contract_id(), PHASE6_COVERAGE_GROWTH_CONTRACT_ID)

    def test_static_indexed_jump_table_recovers_decoded_targets_as_one_batch(self) -> None:
        instruction = lift_x86_function(
            bytes.fromhex("FF248DF00D0F00"),
            base_address=0x000F0B04,
            symbol="indexed_jump_table",
        ).instructions[0]
        targets = (0x000F0C4E, 0x000F0BB0, 0x000F0BBD, 0x000F0C01)
        memory = SparseMemory(
            {
                0x000F0000: bytes(0xDF0)
                + b"".join(target.to_bytes(4, "little") for target in targets)
                + bytes.fromhex("8B442404"),
            }
        )

        recovered = _phase6_static_jump_table_targets(
            memory,
            instruction,
            set(targets),
        )

        self.assertEqual(recovered, tuple(sorted(targets)))

    def test_aligned_copy_tail_recovers_all_nonzero_masked_targets(self) -> None:
        recovered = _phase7_aligned_copy_tail_targets(
            _aligned_copy_tail_memory(),
            _aligned_copy_tail_instructions(),
            {PHASE7_ALIGNED_COPY_TAIL_SITE},
            set(PHASE7_ALIGNED_COPY_TAIL_TARGETS),
        )

        self.assertEqual(
            recovered,
            {
                PHASE7_ALIGNED_COPY_TAIL_SITE: tuple(
                    sorted(PHASE7_ALIGNED_COPY_TAIL_TARGETS)
                )
            },
        )
        self.assertEqual(len(recovered[PHASE7_ALIGNED_COPY_TAIL_SITE]), 15)

    def test_aligned_copy_tail_fails_closed_on_evidence_drift(self) -> None:
        instructions = _aligned_copy_tail_instructions()
        targets = set(PHASE7_ALIGNED_COPY_TAIL_TARGETS)
        wrong_site = {
            PHASE7_ALIGNED_COPY_TAIL_SITE: lift_x86_function(
                bytes.fromhex("FF2495D4B62800"),
                base_address=PHASE7_ALIGNED_COPY_TAIL_SITE,
                symbol="aligned_copy_tail_wrong_index",
            ).instructions[0]
        }
        cases = (
            (_aligned_copy_tail_memory(corrupt_proof=True), instructions, targets),
            (_aligned_copy_tail_memory(corrupt_table=True), instructions, targets),
            (_aligned_copy_tail_memory(), wrong_site, targets),
            (_aligned_copy_tail_memory(), instructions, targets - {min(targets)}),
        )
        for memory, candidate_instructions, allowed_targets in cases:
            with self.subTest(target_count=len(allowed_targets)):
                self.assertEqual(
                    _phase7_aligned_copy_tail_targets(
                        memory,
                        candidate_instructions,
                        {PHASE7_ALIGNED_COPY_TAIL_SITE},
                        allowed_targets,
                    ),
                    {},
                )

    def test_collision_dispatch_recovers_complete_finite_target_family(self) -> None:
        targets = {
            target
            for row in PHASE7_COLLISION_DISPATCH_TABLE
            for target in row
            if target
        }

        recovered = _phase7_collision_dispatch_targets(
            _collision_dispatch_memory(),
            _collision_dispatch_instructions(),
            {PHASE7_COLLISION_DISPATCH_SITE},
            targets,
        )

        self.assertEqual(
            recovered,
            {PHASE7_COLLISION_DISPATCH_SITE: tuple(sorted(targets))},
        )
        self.assertEqual(len(recovered[PHASE7_COLLISION_DISPATCH_SITE]), 7)

    def test_collision_dispatch_fails_closed_on_evidence_drift(self) -> None:
        targets = {
            target
            for row in PHASE7_COLLISION_DISPATCH_TABLE
            for target in row
            if target
        }
        instructions = _collision_dispatch_instructions()
        wrong_register = dict(instructions)
        wrong_register[PHASE7_COLLISION_DISPATCH_SITE] = lift_x86_function(
            bytes.fromhex("FFD1"),
            base_address=PHASE7_COLLISION_DISPATCH_SITE,
            symbol="collision_dispatch_wrong_register",
        ).instructions[0]
        missing_caller = dict(instructions)
        del missing_caller[next(iter(PHASE7_COLLISION_DISPATCH_CALLERS))]
        cases = (
            (
                _collision_dispatch_memory(corrupt_helper=True),
                instructions,
                {PHASE7_COLLISION_DISPATCH_SITE},
                targets,
            ),
            (
                _collision_dispatch_memory(corrupt_table=True),
                instructions,
                {PHASE7_COLLISION_DISPATCH_SITE},
                targets,
            ),
            (
                _collision_dispatch_memory(),
                wrong_register,
                {PHASE7_COLLISION_DISPATCH_SITE},
                targets,
            ),
            (
                _collision_dispatch_memory(),
                missing_caller,
                {PHASE7_COLLISION_DISPATCH_SITE},
                targets,
            ),
            (
                _collision_dispatch_memory(),
                instructions,
                set(),
                targets,
            ),
            (
                _collision_dispatch_memory(),
                instructions,
                {PHASE7_COLLISION_DISPATCH_SITE},
                targets - {min(targets)},
            ),
        )
        for memory, candidate_instructions, sites, allowed_targets in cases:
            with self.subTest(site_count=len(sites), target_count=len(allowed_targets)):
                self.assertEqual(
                    _phase7_collision_dispatch_targets(
                        memory,
                        candidate_instructions,
                        sites,
                        allowed_targets,
                    ),
                    {},
                )

    def test_function_pair_helper_family_recovers_all_five_sites_as_one_batch(
        self,
    ) -> None:
        allowed_targets = {target for pair in PHASE7_FUNCTION_PAIR_TABLE for target in pair} | {
            target for targets in PHASE7_FUNCTION_PAIR_HELPER_TARGETS.values() for target in targets
        }

        recovered = _phase7_function_pair_helper_targets(
            _function_pair_table_memory(),
            _function_pair_helper_instructions(),
            set(PHASE7_FUNCTION_PAIR_HELPER_TARGETS),
            allowed_targets,
        )

        self.assertEqual(recovered, PHASE7_FUNCTION_PAIR_HELPER_TARGETS)
        self.assertEqual(recovered[0x0010A22D], (0x0010A2B0,))
        self.assertEqual(recovered[0x0010A263], (0x0010A2F0,))
        self.assertEqual(recovered[0x0010A29C], (0x0010A2F0,))
        self.assertEqual(len(recovered[0x0010A253]), 16)
        self.assertEqual(recovered[0x0010A253], recovered[0x0010A290])

    def test_function_pair_helper_family_fails_closed_on_evidence_drift(self) -> None:
        instructions = _function_pair_helper_instructions()
        allowed_targets = {target for pair in PHASE7_FUNCTION_PAIR_TABLE for target in pair} | {
            target for targets in PHASE7_FUNCTION_PAIR_HELPER_TARGETS.values() for target in targets
        }
        wrong_slot = dict(instructions)
        wrong_slot[0x0010A253] = lift_x86_function(
            bytes.fromhex("FF5708"),
            base_address=0x0010A253,
            symbol="wrong_function_pair_slot",
        ).instructions[0]
        wrong_cleanup = dict(instructions)
        wrong_cleanup[0x0010A50A] = lift_x86_function(
            bytes.fromhex("BBB0A21000"),
            base_address=0x0010A50A,
            symbol="wrong_function_pair_cleanup",
        ).instructions[0]

        cases = (
            (
                _function_pair_table_memory(corrupt_first_entry=True),
                instructions,
                set(PHASE7_FUNCTION_PAIR_HELPER_TARGETS),
                allowed_targets,
            ),
            (
                _function_pair_table_memory(),
                wrong_slot,
                set(PHASE7_FUNCTION_PAIR_HELPER_TARGETS),
                allowed_targets,
            ),
            (
                _function_pair_table_memory(),
                wrong_cleanup,
                set(PHASE7_FUNCTION_PAIR_HELPER_TARGETS),
                allowed_targets,
            ),
            (
                _function_pair_table_memory(),
                instructions,
                set(PHASE7_FUNCTION_PAIR_HELPER_TARGETS) - {0x0010A29C},
                allowed_targets,
            ),
            (
                _function_pair_table_memory(),
                instructions,
                set(PHASE7_FUNCTION_PAIR_HELPER_TARGETS),
                allowed_targets - {PHASE7_FUNCTION_PAIR_TABLE[0][1]},
            ),
        )
        for memory, candidate_instructions, sites, targets in cases:
            with self.subTest(
                corrupt_table=memory is cases[0][0],
                site_count=len(sites),
                target_count=len(targets),
            ):
                self.assertEqual(
                    _phase7_function_pair_helper_targets(
                        memory,
                        candidate_instructions,
                        sites,
                        targets,
                    ),
                    {},
                )

    def test_d150_callback_family_recovers_both_sites_from_all_direct_callers(
        self,
    ) -> None:
        targets = {
            target
            for callback_targets in PHASE7_D150_CALLBACK_TARGETS.values()
            for target in callback_targets
        }

        recovered = _phase7_d150_callback_targets(
            _d150_callback_instructions(),
            set(PHASE7_D150_CALLBACK_TARGETS),
            targets,
        )

        self.assertEqual(recovered, PHASE7_D150_CALLBACK_TARGETS)
        self.assertEqual(recovered[0x0010D177], recovered[0x0010D1AA])
        self.assertEqual(
            recovered[0x0010D177],
            (0x0010D200, 0x0010D260, 0x0010F9F0, 0x0010FAF0, 0x0010FE60),
        )

    def test_base_manager_object_vtable_recovers_shutdown_family(self) -> None:
        recovered = _phase7_base_manager_object_targets(
            _base_manager_object_memory(),
            _base_manager_object_instructions(),
            set(PHASE7_BASE_MANAGER_OBJECT_BINDINGS),
            set(PHASE7_BASE_MANAGER_OBJECT_VTABLE_ENTRIES),
        )

        self.assertEqual(
            recovered,
            {
                site: (target,)
                for site, (_slot, target) in PHASE7_BASE_MANAGER_OBJECT_BINDINGS.items()
            },
        )
        self.assertNotIn(0x000D869F, PHASE7_CAPTURED_VTABLE_SLOT_BINDINGS)
        self.assertNotIn(0x000D86CD, PHASE7_CAPTURED_VTABLE_SLOT_BINDINGS)

    def test_base_manager_object_vtable_fails_closed_on_family_drift(self) -> None:
        instructions = _base_manager_object_instructions()
        targets = set(PHASE7_BASE_MANAGER_OBJECT_VTABLE_ENTRIES)
        wrong_call = dict(instructions)
        wrong_call[0x000D95BE] = lift_x86_function(
            bytes.fromhex("FF5010"),
            base_address=0x000D95BE,
            symbol="wrong_base_manager_object_slot",
        ).instructions[0]
        missing_installer = dict(instructions)
        del missing_installer[0x00139390]
        added_installer = dict(instructions)
        added_installer[0x00140000] = lift_x86_function(
            bytes.fromhex("C700445D2900"),
            base_address=0x00140000,
            symbol="added_base_manager_object_installer",
        ).instructions[0]
        added_direct_caller = dict(instructions)
        caller_site = 0x00140010
        displacement = (0x000D9B60 - (caller_site + 5)) & 0xFFFFFFFF
        added_direct_caller[caller_site] = lift_x86_function(
            b"\xe8" + displacement.to_bytes(4, "little"),
            base_address=caller_site,
            symbol="added_base_manager_object_direct_caller",
        ).instructions[0]

        for memory, candidate, sites, candidate_targets in (
            (
                _base_manager_object_memory(corrupt_slot=7),
                instructions,
                set(PHASE7_BASE_MANAGER_OBJECT_BINDINGS),
                targets,
            ),
            (
                _base_manager_object_memory(),
                wrong_call,
                set(PHASE7_BASE_MANAGER_OBJECT_BINDINGS),
                targets,
            ),
            (
                _base_manager_object_memory(),
                missing_installer,
                set(PHASE7_BASE_MANAGER_OBJECT_BINDINGS),
                targets,
            ),
            (
                _base_manager_object_memory(),
                added_installer,
                set(PHASE7_BASE_MANAGER_OBJECT_BINDINGS),
                targets,
            ),
            (
                _base_manager_object_memory(),
                added_direct_caller,
                set(PHASE7_BASE_MANAGER_OBJECT_BINDINGS),
                targets,
            ),
            (_base_manager_object_memory(), instructions, set(), targets),
            (
                _base_manager_object_memory(),
                instructions,
                set(PHASE7_BASE_MANAGER_OBJECT_BINDINGS),
                targets - {0x000D9B60},
            ),
        ):
            with self.subTest(
                instruction_count=len(candidate),
                site_count=len(sites),
                target_count=len(candidate_targets),
            ):
                self.assertEqual(
                    _phase7_base_manager_object_targets(
                        memory,
                        candidate,
                        sites,
                        candidate_targets,
                    ),
                    {},
                )

    def test_game_state_registry_freezes_complete_expansive_family(self) -> None:
        objects = {
            index: (cell, object_address, vtable, entries)
            for index, cell, object_address, vtable, entries in PHASE7_GAME_STATE_OBJECTS
        }

        self.assertEqual(set(objects), set(range(0x10)))
        self.assertEqual(
            {cell for cell, _object, _vtable, _entries in objects.values()},
            set(range(0x002FE318, 0x002FE358, 4)),
        )
        self.assertEqual(objects[0x09], (0x002FE33C, 0, 0, ()))
        self.assertEqual(
            sum(
                bool(object_address)
                for _cell, object_address, _vtable, _entries in objects.values()
            ),
            15,
        )
        self.assertTrue(
            all(
                len(entries) == 16
                for _cell, object_address, _vtable, entries in objects.values()
                if object_address
            )
        )
        self.assertEqual(PHASE7_GAME_STATE_REFERENCE_COUNT, 93)
        self.assertEqual(
            PHASE7_GAME_STATE_REFERENCE_SHA256,
            "f06a773c9493dddf10bdaa3fd09bf9e72bcf0b91c9032ef924bd85cc3a230553",
        )
        self.assertEqual(PHASE7_GAME_STATE_DERIVED_SITE_COUNT, 29)
        self.assertEqual(
            PHASE7_GAME_STATE_DERIVED_SITE_SHA256,
            "4bf7b8a9889f9484bc4776e29794412f68b55bbb4b633ceb021992c0573c1034",
        )
        self.assertEqual(
            PHASE7_GAME_STATE_SPECIAL_BINDINGS,
            {
                0x00011093: (0x04, None),
                0x0001114B: (0x10, None),
                0x00013910: (0x04, 0x00),
            },
        )
        self.assertEqual(PHASE7_GAME_STATE_HELPER_START, 0x000157E0)
        self.assertEqual(PHASE7_GAME_STATE_HELPER_END, 0x00015DF5)
        self.assertEqual(
            PHASE7_GAME_STATE_HELPER_SHA256,
            "35ca5777a7d7d208a778f68dfda542c1d79ba0c088524339fc65e9cadbba235c",
        )
        self.assertEqual(
            set(PHASE7_GAME_STATE_HELPER_CALLERS),
            {0x00014E5D, 0x00015423, 0x000163C3, 0x00016843},
        )
        self.assertEqual(
            PHASE7_GAME_STATE_HELPER_STATE_INDEXES,
            (0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x0F),
        )
        self.assertEqual(
            PHASE7_GAME_STATE_HELPER_BINDINGS,
            {
                0x00015D7D: (0x18, "FF5018"),
                0x00015DC3: (0x1C, "FF521C"),
            },
        )
        self.assertEqual(PHASE7_GAME_STATE_SITE_COUNT, 34)
        self.assertEqual(
            PHASE7_GAME_STATE_SITE_SHA256,
            "46b1d6ddc548251c302bbbe49d2cb3ef3c540286a7df6cc08b5effb96594b20a",
        )
        self.assertEqual(
            PHASE7_GAME_STATE_TRANSITION_CODE_END
            - PHASE7_GAME_STATE_TRANSITION_CODE_START,
            370,
        )
        self.assertEqual(
            PHASE7_GAME_STATE_TRANSITION_CODE_SHA256,
            "21b3ebf8179c4532ab7b6e4d5b9f09dba4cbf3cc04b794c9282245245d733326",
        )
        self.assertEqual(
            PHASE7_GAME_STATE_OBSERVED_BOUNDARY,
            (0x000127E7, 0x0F, 0x14, 0x000144F0),
        )
        self.assertEqual(objects[0x0F][3][0x14 // 4], 0x000144F0)
        self.assertEqual(
            _phase7_game_state_object_targets(SparseMemory({}), {}, set(), set()),
            {},
        )

    def test_level_load_strategy_vtables_recover_complete_22_site_family(self) -> None:
        targets = {
            target
            for _object_address, _vtable, entries in PHASE7_LEVEL_LOAD_STRATEGY_OBJECTS
            for target in entries
        }
        recovered = _phase7_level_load_strategy_targets(
            _level_load_strategy_memory(),
            _level_load_strategy_instructions(),
            set(PHASE7_LEVEL_LOAD_STRATEGY_BINDINGS),
            targets,
        )
        vtables = {
            vtable: entries
            for _object_address, vtable, entries in PHASE7_LEVEL_LOAD_STRATEGY_OBJECTS
        }
        expected = {}
        for site, (_caller_start, vtable, slot) in (
            PHASE7_LEVEL_LOAD_STRATEGY_BINDINGS.items()
        ):
            expected[site] = (
                (vtables[vtable][slot // 4],)
                if vtable
                else tuple(sorted({entries[slot // 4] for entries in vtables.values()}))
            )

        self.assertEqual(recovered, expected)
        self.assertEqual(len(recovered), 22)
        self.assertEqual(
            recovered[0x00013F7D],
            (0x00011960, 0x00012790, 0x000172D0, 0x00018590),
        )
        self.assertEqual(
            recovered[0x000143CE],
            (0x00011980, 0x000127A0, 0x000148E0, 0x000172E0, 0x000186F0),
        )

    def test_level_load_strategy_vtables_fail_closed_on_family_drift(self) -> None:
        instructions = _level_load_strategy_instructions()
        targets = {
            target
            for _object_address, _vtable, entries in PHASE7_LEVEL_LOAD_STRATEGY_OBJECTS
            for target in entries
        }
        wrong_call = dict(instructions)
        wrong_call[0x00014D5B] = lift_x86_function(
            bytes.fromhex("FF5218"),
            base_address=0x00014D5B,
            symbol="wrong_level_load_strategy_slot",
        ).instructions[0]
        added_reference = dict(instructions)
        added_reference[0x00140000] = lift_x86_function(
            bytes.fromhex("B9C43B3000"),
            base_address=0x00140000,
            symbol="added_level_load_strategy_reference",
        ).instructions[0]
        first_object, first_vtable, _first_entries = PHASE7_LEVEL_LOAD_STRATEGY_OBJECTS[0]

        for memory, candidate, sites, candidate_targets in (
            (
                _level_load_strategy_memory(corrupt_object=first_object),
                instructions,
                set(PHASE7_LEVEL_LOAD_STRATEGY_BINDINGS),
                targets,
            ),
            (
                _level_load_strategy_memory(
                    corrupt_vtable=first_vtable,
                    corrupt_slot=1,
                ),
                instructions,
                set(PHASE7_LEVEL_LOAD_STRATEGY_BINDINGS),
                targets,
            ),
            (
                _level_load_strategy_memory(corrupt_caller=0x0001A365),
                instructions,
                set(PHASE7_LEVEL_LOAD_STRATEGY_BINDINGS),
                targets,
            ),
            (
                _level_load_strategy_memory(corrupt_installer=0x00016500),
                instructions,
                set(PHASE7_LEVEL_LOAD_STRATEGY_BINDINGS),
                targets,
            ),
            (
                _level_load_strategy_memory(),
                wrong_call,
                set(PHASE7_LEVEL_LOAD_STRATEGY_BINDINGS),
                targets,
            ),
            (
                _level_load_strategy_memory(),
                added_reference,
                set(PHASE7_LEVEL_LOAD_STRATEGY_BINDINGS),
                targets,
            ),
            (
                _level_load_strategy_memory(),
                instructions,
                {0x00013F7D},
                targets,
            ),
            (
                _level_load_strategy_memory(),
                instructions,
                set(PHASE7_LEVEL_LOAD_STRATEGY_BINDINGS),
                targets - {0x00018590},
            ),
        ):
            with self.subTest(
                instruction_count=len(candidate),
                site_count=len(sites),
                target_count=len(candidate_targets),
            ):
                self.assertEqual(
                    _phase7_level_load_strategy_targets(
                        memory,
                        candidate,
                        sites,
                        candidate_targets,
                    ),
                    {},
                )

    def test_level_parameter_bank_freezes_complete_expansive_family(self) -> None:
        fields = {
            field: (object_address, vtable, entries)
            for field, object_address, vtable, entries in (
                PHASE7_LEVEL_PARAMETER_BANK_OBJECTS
            )
        }

        self.assertEqual(set(fields), set(range(0x10, 0x90, 4)))
        self.assertEqual(len(fields), 32)
        self.assertEqual(len({vtable for _object, vtable, _entries in fields.values()}), 19)
        self.assertEqual(
            PHASE7_LEVEL_PARAMETER_BANK_GENERIC_BINDINGS,
            {0x0001AB3F: 0x00, 0x0001ABEB: 0x08, 0x0001AC27: 0x04},
        )
        self.assertEqual(PHASE7_LEVEL_PARAMETER_BANK_SETTER_SITE_COUNT, 142)
        self.assertEqual(
            PHASE7_LEVEL_PARAMETER_BANK_SETTER_SITE_SHA256,
            "43c4f4c8280b909b7fbcb5f3323d35a5221717aac585a69bba44e9caf20322bf",
        )
        self.assertEqual(
            PHASE7_LEVEL_PARAMETER_BANK_CODE_END
            - PHASE7_LEVEL_PARAMETER_BANK_CODE_START,
            9670,
        )
        self.assertEqual(
            PHASE7_LEVEL_PARAMETER_BANK_CODE_SHA256,
            "b2143380234d2c2d73fd07da2b3699b8517e52074140d48cf8e2f789ecb531a2",
        )
        self.assertEqual(
            PHASE7_LEVEL_PARAMETER_BANK_OBSERVED_BOUNDARY,
            (0x0001B200, 0x10, 0x00026700),
        )
        self.assertEqual(fields[0x10][2][1], 0x00026700)
        self.assertTrue(
            {0x0001ADF7, 0x0001AF38, 0x0001B128}.isdisjoint(
                PHASE7_LEVEL_LOAD_STRATEGY_BINDINGS
            )
        )
        self.assertEqual(
            _phase7_level_parameter_bank_targets(
                SparseMemory({}),
                {},
                set(),
                set(),
            ),
            {},
        )

    def test_level_load_manager_vtables_recover_complete_29_site_family(self) -> None:
        targets = {
            target
            for _vtable, entries in PHASE7_LEVEL_LOAD_MANAGER_VTABLES
            for target in entries
        }
        recovered = _phase7_level_load_manager_targets(
            _level_load_manager_memory(),
            _level_load_manager_instructions(),
            set(PHASE7_LEVEL_LOAD_MANAGER_BINDINGS),
            targets,
        )
        vtables = dict(PHASE7_LEVEL_LOAD_MANAGER_VTABLES)
        expected = {}
        for site, (_caller_start, vtable, slot) in (
            PHASE7_LEVEL_LOAD_MANAGER_BINDINGS.items()
        ):
            expected[site] = (
                (vtables[vtable][slot // 4],)
                if vtable
                else (vtables[0x00295D68][slot // 4],)
            )

        self.assertEqual(recovered, expected)
        self.assertEqual(len(recovered), 29)
        self.assertEqual(recovered[0x0005EAD3], (0x0005FF60,))
        self.assertEqual(recovered[0x000743B5], (0x00055B50,))
        self.assertEqual(recovered[0x00082222], (0x0007ECD0,))
        self.assertEqual(recovered[0x00082319], (0x0007F4E0,))
        self.assertEqual(recovered[0x00082485], (0x0005B670,))
        self.assertEqual(recovered[0x00082709], (0x0007F4E0,))
        self.assertEqual(recovered[0x00082727], (0x00066410,))
        self.assertEqual(
            PHASE7_LEVEL_LOAD_MANAGER_SWEEP_OBSERVED_BOUNDARY,
            (0x00082709, 0x00295E84, 0x10, 0x0007F4E0),
        )
        self.assertEqual(len(PHASE7_LEVEL_LOAD_MANAGER_OBJECT_BINDINGS), 20)

    def test_level_load_manager_vtables_fail_closed_on_family_drift(self) -> None:
        instructions = _level_load_manager_instructions()
        targets = {
            target
            for _vtable, entries in PHASE7_LEVEL_LOAD_MANAGER_VTABLES
            for target in entries
        }
        wrong_call = dict(instructions)
        wrong_call[0x0005EAD3] = lift_x86_function(
            bytes.fromhex("FF501C"),
            base_address=0x0005EAD3,
            symbol="wrong_level_load_manager_slot",
        ).instructions[0]
        added_reference = dict(instructions)
        added_reference[0x00140000] = lift_x86_function(
            bytes.fromhex("B8B85D2900"),
            base_address=0x00140000,
            symbol="added_level_load_manager_reference",
        ).instructions[0]
        first_object, _first_object_vtable = PHASE7_LEVEL_LOAD_MANAGER_OBJECT_BINDINGS[0]
        first_vtable, _first_entries = PHASE7_LEVEL_LOAD_MANAGER_VTABLES[0]

        for memory, candidate, sites, candidate_targets in (
            (
                _level_load_manager_memory(corrupt_object=first_object),
                instructions,
                set(PHASE7_LEVEL_LOAD_MANAGER_BINDINGS),
                targets,
            ),
            (
                _level_load_manager_memory(
                    corrupt_vtable=first_vtable,
                    corrupt_slot=6,
                ),
                instructions,
                set(PHASE7_LEVEL_LOAD_MANAGER_BINDINGS),
                targets,
            ),
            (
                _level_load_manager_memory(corrupt_caller=0x0005B8DA),
                instructions,
                set(PHASE7_LEVEL_LOAD_MANAGER_BINDINGS),
                targets,
            ),
            (
                _level_load_manager_memory(corrupt_installer=0x001392B0),
                instructions,
                set(PHASE7_LEVEL_LOAD_MANAGER_BINDINGS),
                targets,
            ),
            (
                _level_load_manager_memory(),
                wrong_call,
                set(PHASE7_LEVEL_LOAD_MANAGER_BINDINGS),
                targets,
            ),
            (
                _level_load_manager_memory(),
                added_reference,
                set(PHASE7_LEVEL_LOAD_MANAGER_BINDINGS),
                targets,
            ),
            (
                _level_load_manager_memory(),
                instructions,
                {0x0005EAD3},
                targets,
            ),
            (
                _level_load_manager_memory(),
                instructions,
                set(PHASE7_LEVEL_LOAD_MANAGER_BINDINGS),
                targets - {0x0005FF60},
            ),
        ):
            with self.subTest(
                instruction_count=len(candidate),
                site_count=len(sites),
                target_count=len(candidate_targets),
            ):
                self.assertEqual(
                    _phase7_level_load_manager_targets(
                        memory,
                        candidate,
                        sites,
                        candidate_targets,
                    ),
                    {},
                )

    def test_level_update_switch_recovers_all_nine_primary_helpers(self) -> None:
        recovered = _phase7_level_update_primary_targets(
            _level_update_primary_memory(),
            _level_update_primary_instructions(),
            set(PHASE7_LEVEL_UPDATE_PRIMARY_BINDINGS)
            | set(PHASE7_LEVEL_UPDATE_SECONDARY_GUARDED_SITES),
            {0x0007EE80},
        )

        self.assertEqual(
            recovered,
            {site: (0x0007EE80,) for site in PHASE7_LEVEL_UPDATE_PRIMARY_BINDINGS},
        )
        self.assertEqual(len(recovered), 9)
        self.assertEqual(
            PHASE7_LEVEL_UPDATE_PRIMARY_OBJECTS,
            (0x004B9C50, 0x004BE1C0, 0x004C2730, 0x004C6CA0),
        )
        self.assertEqual(PHASE7_LEVEL_UPDATE_PRIMARY_VTABLE, 0x00295E84)
        self.assertEqual(
            PHASE7_LEVEL_UPDATE_OBSERVED_BOUNDARIES,
            {(0x000AA3B9, 0x0007EE80), (0x000AB0D5, 0x0007EE80)},
        )
        self.assertTrue(
            set(recovered).isdisjoint(PHASE7_LEVEL_UPDATE_SECONDARY_GUARDED_SITES)
        )
        self.assertEqual(
            {helper for _case, helper in PHASE7_LEVEL_UPDATE_HELPER_CALLS.values()},
            {helper for helper, _caller in PHASE7_LEVEL_UPDATE_PRIMARY_BINDINGS.values()},
        )

    def test_level_update_switch_fails_closed_on_family_drift(self) -> None:
        instructions = _level_update_primary_instructions()
        wrong_switch = dict(instructions)
        wrong_switch[0x000ADCDA] = lift_x86_function(
            bytes.fromhex("E800000000"),
            base_address=0x000ADCDA,
            symbol="wrong_level_update_switch_helper",
        ).instructions[0]
        wrong_slot = dict(instructions)
        wrong_slot[0x000AA3B9] = lift_x86_function(
            bytes.fromhex("FF501C"),
            base_address=0x000AA3B9,
            symbol="wrong_level_update_primary_slot",
        ).instructions[0]
        sites = set(PHASE7_LEVEL_UPDATE_PRIMARY_BINDINGS)

        for memory, candidate, candidate_sites, targets in (
            (_level_update_primary_memory(corrupt_cell=True), instructions, sites, {0x7EE80}),
            (
                _level_update_primary_memory(corrupt_selector=True),
                instructions,
                sites,
                {0x7EE80},
            ),
            (
                _level_update_primary_memory(corrupt_jump_table=True),
                instructions,
                sites,
                {0x7EE80},
            ),
            (
                _level_update_primary_memory(corrupt_caller=0x000AB0C3),
                instructions,
                sites,
                {0x7EE80},
            ),
            (_level_update_primary_memory(), wrong_switch, sites, {0x7EE80}),
            (_level_update_primary_memory(), wrong_slot, sites, {0x7EE80}),
            (_level_update_primary_memory(), instructions, {0x000AB0D5}, {0x7EE80}),
            (_level_update_primary_memory(), instructions, sites, set()),
        ):
            with self.subTest(
                instruction_count=len(candidate),
                site_count=len(candidate_sites),
                target_count=len(targets),
            ):
                self.assertEqual(
                    _phase7_level_update_primary_targets(
                        memory,
                        candidate,
                        candidate_sites,
                        targets,
                    ),
                    {},
                )

    def test_level_sample_loop_recovers_all_three_primary_queries(self) -> None:
        recovered = _phase7_level_sample_primary_targets(
            _level_sample_primary_memory(),
            _level_sample_primary_instructions(),
            set(PHASE7_LEVEL_SAMPLE_PRIMARY_BINDINGS),
            {0x0007EE80},
        )

        self.assertEqual(
            recovered,
            {
                site: (0x0007EE80,)
                for site in PHASE7_LEVEL_SAMPLE_PRIMARY_BINDINGS
            },
        )
        self.assertEqual(len(recovered), 3)
        self.assertEqual(
            PHASE7_LEVEL_SAMPLE_DIRECT_CALLS,
            {0x00097AB4: 0x0009D160},
        )
        self.assertEqual(
            PHASE7_LEVEL_SAMPLE_OBSERVED_BOUNDARY,
            (0x0009D679, 0x004B9C50, 0x00295E84, 0x0007EE80),
        )

    def test_level_sample_loop_fails_closed_on_family_drift(self) -> None:
        instructions = _level_sample_primary_instructions()
        sites = set(PHASE7_LEVEL_SAMPLE_PRIMARY_BINDINGS)
        wrong_slot = dict(instructions)
        wrong_slot[0x0009D679] = lift_x86_function(
            bytes.fromhex("FF521C"),
            base_address=0x0009D679,
            symbol="wrong_level_sample_slot",
        ).instructions[0]
        added_direct_caller = dict(instructions)
        added_direct_caller[0x00140000] = lift_x86_function(
            bytes.fromhex("E85BD1F5FF"),
            base_address=0x00140000,
            symbol="added_level_sample_direct_caller",
        ).instructions[0]

        for memory, candidate, candidate_sites, targets in (
            (_level_sample_primary_memory(corrupt_cell=3), instructions, sites, {0x7EE80}),
            (
                _level_sample_primary_memory(corrupt_proof=0x0009D652),
                instructions,
                sites,
                {0x7EE80},
            ),
            (
                _level_sample_primary_memory(corrupt_vtable_slot=6),
                instructions,
                sites,
                {0x7EE80},
            ),
            (_level_sample_primary_memory(), wrong_slot, sites, {0x7EE80}),
            (
                _level_sample_primary_memory(),
                added_direct_caller,
                sites,
                {0x7EE80},
            ),
            (_level_sample_primary_memory(), instructions, {0x0009D679}, {0x7EE80}),
            (_level_sample_primary_memory(), instructions, sites, set()),
        ):
            with self.subTest(
                instruction_count=len(candidate),
                site_count=len(candidate_sites),
                target_count=len(targets),
            ):
                self.assertEqual(
                    _phase7_level_sample_primary_targets(
                        memory,
                        candidate,
                        candidate_sites,
                        targets,
                    ),
                    {},
                )

    def test_level_query_recovers_observed_and_predicted_sibling(self) -> None:
        recovered = _phase7_level_query_targets(
            _level_query_memory(),
            _level_query_instructions(),
            set(PHASE7_LEVEL_QUERY_BINDINGS),
            {0x0007EE80, 0x0009F570, 0x0009F660},
        )

        self.assertEqual(
            recovered,
            {
                0x000798B5: (0x0007EE80,),
                0x0007994D: (0x0009F660,),
                0x0009F562: (0x0009F570,),
                0x0009F650: (0x0009F660,),
                0x0009F6AC: (0x0009F570,),
            },
        )
        self.assertEqual(
            PHASE7_LEVEL_QUERY_OBSERVED_BOUNDARY,
            (0x000798B5, 0x004B9C50, 0x00295E84, 0x0007EE80),
        )
        self.assertEqual(
            PHASE7_LEVEL_QUERY_PREDICTED_BOUNDARY,
            (0x0007994D, 0x0050D890, 0x002BF3A0, 0x0009F660),
        )
        self.assertEqual(
            PHASE7_LEVEL_QUERY_DIRECT_CALLS,
            {
                0x0001395E: 0x000822B0,
                0x00082741: 0x0009F9B0,
                0x0009FA60: 0x00079880,
            },
        )
        self.assertEqual(PHASE7_LEVEL_QUERY_RECORD_CAPACITY, 20)
        self.assertEqual(PHASE7_LEVEL_QUERY_POOL_CAPACITY, 256)

    def test_level_query_fails_closed_on_family_drift(self) -> None:
        instructions = _level_query_instructions()
        sites = set(PHASE7_LEVEL_QUERY_BINDINGS)
        targets = {0x0007EE80, 0x0009F570, 0x0009F660}

        wrong_slot = dict(instructions)
        wrong_slot[0x000798B5] = lift_x86_function(
            bytes.fromhex("FF521C"),
            base_address=0x000798B5,
            symbol="wrong_level_query_primary_slot",
        ).instructions[0]
        missing_direct_caller = dict(instructions)
        missing_direct_caller.pop(0x0009FA60)
        added_vtable_reference = dict(instructions)
        added_vtable_reference[0x00140000] = lift_x86_function(
            bytes.fromhex("B8A0F32B00"),
            base_address=0x00140000,
            symbol="added_level_query_record_vtable_reference",
        ).instructions[0]

        corrupt_proof = _level_query_memory()
        corrupt_proof.write(0x0009F9B0, b"\x82")
        corrupt_owner = _level_query_memory()
        corrupt_owner.write_u32(PHASE7_LEVEL_QUERY_OWNER, 0)
        corrupt_record_base = _level_query_memory()
        corrupt_record_base.write_u32(PHASE7_LEVEL_QUERY_RECORD_BASE_CELL, 0)
        empty_record_bank = _level_query_memory()
        empty_record_bank.write_u32(PHASE7_LEVEL_QUERY_RECORD_COUNT_CELL, 0)
        oversized_record_bank = _level_query_memory()
        oversized_record_bank.write_u32(
            PHASE7_LEVEL_QUERY_RECORD_COUNT_CELL,
            PHASE7_LEVEL_QUERY_RECORD_CAPACITY + 1,
        )
        corrupt_record = _level_query_memory()
        corrupt_record.write_u32(
            PHASE7_LEVEL_QUERY_RECORD_BASE
            + (PHASE7_LEVEL_QUERY_RECORD_CAPACITY - 1)
            * PHASE7_LEVEL_QUERY_RECORD_STRIDE,
            0,
        )
        corrupt_record_slot = _level_query_memory()
        corrupt_record_slot.write_u32(
            PHASE7_LEVEL_QUERY_RECORD_VTABLE + 4,
            0,
        )
        corrupt_pool_base = _level_query_memory()
        corrupt_pool_base.write_u32(PHASE7_LEVEL_QUERY_POOL_BASE_CELL, 0)
        corrupt_pool = _level_query_memory()
        corrupt_pool.write_u32(
            PHASE7_LEVEL_QUERY_POOL_BASE
            + (PHASE7_LEVEL_QUERY_POOL_CAPACITY - 1)
            * PHASE7_LEVEL_QUERY_POOL_STRIDE,
            0,
        )
        corrupt_pool_slot = _level_query_memory()
        corrupt_pool_slot.write_u32(PHASE7_LEVEL_QUERY_POOL_VTABLE + 4, 0)

        for memory, candidate, candidate_sites, candidate_targets in (
            (corrupt_proof, instructions, sites, targets),
            (corrupt_owner, instructions, sites, targets),
            (corrupt_record_base, instructions, sites, targets),
            (empty_record_bank, instructions, sites, targets),
            (oversized_record_bank, instructions, sites, targets),
            (corrupt_record, instructions, sites, targets),
            (corrupt_record_slot, instructions, sites, targets),
            (corrupt_pool_base, instructions, sites, targets),
            (corrupt_pool, instructions, sites, targets),
            (corrupt_pool_slot, instructions, sites, targets),
            (_level_query_memory(), wrong_slot, sites, targets),
            (_level_query_memory(), missing_direct_caller, sites, targets),
            (_level_query_memory(), added_vtable_reference, sites, targets),
            (_level_query_memory(), instructions, {0x000798B5}, targets),
            (_level_query_memory(), instructions, sites, {0x0007EE80}),
        ):
            with self.subTest(
                instruction_count=len(candidate),
                site_count=len(candidate_sites),
                target_count=len(candidate_targets),
            ):
                self.assertEqual(
                    _phase7_level_query_targets(
                        memory,
                        candidate,
                        candidate_sites,
                        candidate_targets,
                    ),
                    {},
                )

    def test_runtime_callback_vector_recovers_complete_seven_site_family(self) -> None:
        targets = {target for _cell, target in PHASE7_RUNTIME_CALLBACK_VECTOR}
        recovered = _phase7_runtime_callback_vector_targets(
            _runtime_callback_vector_memory(),
            _runtime_callback_vector_instructions(),
            set(PHASE7_RUNTIME_CALLBACK_BINDINGS),
            targets,
        )

        self.assertEqual(
            recovered,
            {
                site: (target,)
                for site, (_caller_start, _cell, target) in (
                    PHASE7_RUNTIME_CALLBACK_BINDINGS.items()
                )
            },
        )
        self.assertEqual(len(recovered), 7)
        self.assertEqual(len(PHASE7_RUNTIME_CALLBACK_VECTOR), 6)
        self.assertEqual(recovered[0x00121EAA], (0x0012186B,))
        self.assertEqual(recovered[0x00126034], (0x0012157B,))

    def test_runtime_callback_vector_fails_closed_on_family_drift(self) -> None:
        instructions = _runtime_callback_vector_instructions()
        sites = set(PHASE7_RUNTIME_CALLBACK_BINDINGS)
        targets = {target for _cell, target in PHASE7_RUNTIME_CALLBACK_VECTOR}
        wrong_call = dict(instructions)
        wrong_call[0x00121EAA] = lift_x86_function(
            bytes.fromhex("FF15B87E3400"),
            base_address=0x00121EAA,
            symbol="wrong_runtime_callback_cell",
        ).instructions[0]
        wrong_installer_reference = dict(instructions)
        wrong_installer_reference[0x0011E256] = lift_x86_function(
            bytes.fromhex("A3C87E3400"),
            base_address=0x0011E256,
            symbol="wrong_runtime_callback_installer_cell",
        ).instructions[0]
        added_reference = dict(instructions)
        added_reference[0x00140000] = lift_x86_function(
            bytes.fromhex("A1B47E3400"),
            base_address=0x00140000,
            symbol="added_runtime_callback_reference",
        ).instructions[0]

        for memory, candidate, candidate_sites, candidate_targets in (
            (
                _runtime_callback_vector_memory(corrupt_cell=0x00347EB4),
                instructions,
                sites,
                targets,
            ),
            (
                _runtime_callback_vector_memory(corrupt_caller=0x00121E87),
                instructions,
                sites,
                targets,
            ),
            (
                _runtime_callback_vector_memory(corrupt_installer=0x0011E251),
                instructions,
                sites,
                targets,
            ),
            (_runtime_callback_vector_memory(), wrong_call, sites, targets),
            (
                _runtime_callback_vector_memory(),
                wrong_installer_reference,
                sites,
                targets,
            ),
            (_runtime_callback_vector_memory(), added_reference, sites, targets),
            (
                _runtime_callback_vector_memory(),
                instructions,
                {0x00121EAA},
                targets,
            ),
            (
                _runtime_callback_vector_memory(),
                instructions,
                sites,
                targets - {0x0012186B},
            ),
        ):
            with self.subTest(
                instruction_count=len(candidate),
                site_count=len(candidate_sites),
                target_count=len(candidate_targets),
            ):
                self.assertEqual(
                    _phase7_runtime_callback_vector_targets(
                        memory,
                        candidate,
                        candidate_sites,
                        candidate_targets,
                    ),
                    {},
                )

    def test_memmove_jump_tables_recover_complete_16_site_family(self) -> None:
        targets = {
            target
            for entries in PHASE7_MEMMOVE_JUMP_TABLES.values()
            for _index, target in entries
        }
        recovered = _phase7_memmove_jump_table_targets(
            _memmove_jump_table_memory(),
            _memmove_jump_table_instructions(),
            set(PHASE7_MEMMOVE_JUMP_BINDINGS),
            targets,
        )

        self.assertEqual(len(recovered), 16)
        self.assertEqual(
            recovered[0x0011EFFD],
            (0x0011F020, 0x0011F04C, 0x0011F070),
        )
        self.assertEqual(
            recovered[0x0011F004],
            (0x0011F10C, 0x0011F114, 0x0011F120, 0x0011F134),
        )
        self.assertEqual(
            recovered[0x0011F176],
            tuple(0x0011F24C + offset for offset in range(0, 0x38, 8))
            + (0x0011F28F,),
        )
        self.assertEqual(
            recovered[0x0011F191],
            (0x0011F1AC, 0x0011F1D0, 0x0011F1F8),
        )

    def test_memmove_jump_tables_fail_closed_on_family_drift(self) -> None:
        instructions = _memmove_jump_table_instructions()
        sites = set(PHASE7_MEMMOVE_JUMP_BINDINGS)
        targets = {
            target
            for entries in PHASE7_MEMMOVE_JUMP_TABLES.values()
            for _index, target in entries
        }
        wrong_index = dict(instructions)
        wrong_index[0x0011F191] = lift_x86_function(
            bytes.fromhex("FF248D9CF11100"),
            base_address=0x0011F191,
            symbol="wrong_memmove_jump_index",
        ).instructions[0]
        added_reference = dict(instructions)
        added_reference[0x00140000] = lift_x86_function(
            bytes.fromhex("8B048D9CF11100"),
            base_address=0x00140000,
            symbol="added_memmove_table_reference",
        ).instructions[0]

        for memory, candidate, candidate_sites, candidate_targets in (
            (
                _memmove_jump_table_memory(corrupt_control=0x0011EFB0),
                instructions,
                sites,
                targets,
            ),
            (
                _memmove_jump_table_memory(corrupt_entry=(0x0011F19C, 1)),
                instructions,
                sites,
                targets,
            ),
            (_memmove_jump_table_memory(), wrong_index, sites, targets),
            (_memmove_jump_table_memory(), added_reference, sites, targets),
            (
                _memmove_jump_table_memory(),
                instructions,
                {0x0011F191},
                targets,
            ),
            (
                _memmove_jump_table_memory(),
                instructions,
                sites,
                targets - {0x0011F1AC},
            ),
        ):
            with self.subTest(
                instruction_count=len(candidate),
                site_count=len(candidate_sites),
                target_count=len(candidate_targets),
            ):
                self.assertEqual(
                    _phase7_memmove_jump_table_targets(
                        memory,
                        candidate,
                        candidate_sites,
                        candidate_targets,
                    ),
                    {},
                )

    def test_title_input_object_vtable_recovers_start_slot_as_one_batch(self) -> None:
        recovered = _phase7_title_input_object_targets(
            _title_input_object_memory(),
            _title_input_object_instructions(),
            set(PHASE7_TITLE_INPUT_OBJECT_BINDINGS),
            set(PHASE7_TITLE_INPUT_OBJECT_VTABLE_ENTRIES),
        )

        self.assertEqual(recovered, {0x0002AB5A: (0x0002B040,)})

    def test_title_input_object_vtable_fails_closed_on_family_drift(self) -> None:
        instructions = _title_input_object_instructions()
        targets = set(PHASE7_TITLE_INPUT_OBJECT_VTABLE_ENTRIES)
        wrong_call = dict(instructions)
        wrong_call[0x0002AB5A] = lift_x86_function(
            bytes.fromhex("FF5008"),
            base_address=0x0002AB5A,
            symbol="wrong_title_input_object_slot",
        ).instructions[0]
        added_reference = dict(instructions)
        added_reference[0x00140000] = lift_x86_function(
            bytes.fromhex("A158C93100"),
            base_address=0x00140000,
            symbol="added_title_input_object_pointer_reference",
        ).instructions[0]

        for memory, candidate, sites, candidate_targets in (
            (
                _title_input_object_memory(corrupt_slot=3),
                instructions,
                set(PHASE7_TITLE_INPUT_OBJECT_BINDINGS),
                targets,
            ),
            (
                _title_input_object_memory(),
                wrong_call,
                set(PHASE7_TITLE_INPUT_OBJECT_BINDINGS),
                targets,
            ),
            (
                _title_input_object_memory(),
                added_reference,
                set(PHASE7_TITLE_INPUT_OBJECT_BINDINGS),
                targets,
            ),
            (_title_input_object_memory(), instructions, set(), targets),
            (
                _title_input_object_memory(),
                instructions,
                set(PHASE7_TITLE_INPUT_OBJECT_BINDINGS),
                targets - {0x0002B040},
            ),
        ):
            with self.subTest(
                instruction_count=len(candidate),
                site_count=len(sites),
                target_count=len(candidate_targets),
            ):
                self.assertEqual(
                    _phase7_title_input_object_targets(
                        memory,
                        candidate,
                        sites,
                        candidate_targets,
                    ),
                    {},
                )

    def test_post_start_object_vtable_recovers_two_slot_c_sites(self) -> None:
        recovered = _phase7_post_start_object_targets(
            _post_start_object_memory(),
            _post_start_object_instructions(),
            set(PHASE7_POST_START_OBJECT_BINDINGS),
            set(PHASE7_POST_START_OBJECT_VTABLE_ENTRIES),
        )

        self.assertEqual(
            recovered,
            {
                site: (target,)
                for site, (_slot, target) in PHASE7_POST_START_OBJECT_BINDINGS.items()
            },
        )

    def test_post_start_object_vtable_fails_closed_on_family_drift(self) -> None:
        instructions = _post_start_object_instructions()
        targets = set(PHASE7_POST_START_OBJECT_VTABLE_ENTRIES)
        wrong_call = dict(instructions)
        wrong_call[0x0004501F] = lift_x86_function(
            bytes.fromhex("FF5208"),
            base_address=0x0004501F,
            symbol="wrong_post_start_object_slot",
        ).instructions[0]
        added_direct_caller = dict(instructions)
        caller_site = 0x00140000
        displacement = (0x0002F1C0 - (caller_site + 5)) & 0xFFFFFFFF
        added_direct_caller[caller_site] = lift_x86_function(
            b"\xe8" + displacement.to_bytes(4, "little"),
            base_address=caller_site,
            symbol="added_post_start_object_direct_caller",
        ).instructions[0]

        for memory, candidate, sites, candidate_targets in (
            (
                _post_start_object_memory(corrupt_slot=3),
                instructions,
                set(PHASE7_POST_START_OBJECT_BINDINGS),
                targets,
            ),
            (
                _post_start_object_memory(),
                wrong_call,
                set(PHASE7_POST_START_OBJECT_BINDINGS),
                targets,
            ),
            (
                _post_start_object_memory(),
                added_direct_caller,
                set(PHASE7_POST_START_OBJECT_BINDINGS),
                targets,
            ),
            (_post_start_object_memory(), instructions, set(), targets),
            (
                _post_start_object_memory(),
                instructions,
                set(PHASE7_POST_START_OBJECT_BINDINGS),
                targets - {0x0002F1C0},
            ),
        ):
            with self.subTest(
                instruction_count=len(candidate),
                site_count=len(sites),
                target_count=len(candidate_targets),
            ):
                self.assertEqual(
                    _phase7_post_start_object_targets(
                        memory,
                        candidate,
                        sites,
                        candidate_targets,
                    ),
                    {},
                )

    def test_save_slot_object_vtables_recover_complete_five_site_family(self) -> None:
        recovered = _phase7_save_slot_object_targets(
            _save_slot_object_memory(),
            _save_slot_object_instructions(),
            set(PHASE7_SAVE_SLOT_OBJECT_BINDINGS),
            set(PHASE7_SAVE_SLOT_OBJECT_VTABLE_ENTRIES)
            | set(PHASE7_SAVE_SLOT_OWNER_VTABLE_ENTRIES),
        )

        self.assertEqual(
            recovered,
            {site: (target,) for site, (_slot, target) in PHASE7_SAVE_SLOT_OBJECT_BINDINGS.items()},
        )

    def test_new_save_object_vtables_recover_complete_call_batch(self) -> None:
        recovered = _phase7_new_save_object_targets(
            _new_save_object_memory(),
            _new_save_object_instructions(),
            set(PHASE7_NEW_SAVE_OBJECT_BINDINGS),
            set(PHASE7_POST_START_OBJECT_VTABLE_ENTRIES)
            | set(PHASE7_NEW_SAVE_COMPANION_OBJECT_VTABLE_ENTRIES),
        )

        self.assertEqual(
            recovered,
            {
                site: (target,)
                for site, (_vtable, _slot, target) in PHASE7_NEW_SAVE_OBJECT_BINDINGS.items()
            },
        )

    def test_resource_selection_object_recovers_all_34_exact_callers(self) -> None:
        recovered = _phase7_resource_selection_object_targets(
            _resource_selection_object_memory(),
            _resource_selection_object_instructions(),
            set(PHASE7_RESOURCE_SELECTION_OBJECT_BINDINGS),
            set(PHASE7_RESOURCE_SELECTION_OBJECT_VTABLE_ENTRIES),
        )

        self.assertEqual(
            recovered,
            {
                site: (target,)
                for site, (_caller_start, _slot, target) in (
                    PHASE7_RESOURCE_SELECTION_OBJECT_BINDINGS.items()
                )
            },
        )

    def test_level_select_object_recovers_both_active_slot_c_callers(self) -> None:
        recovered = _phase7_level_select_object_targets(
            _level_select_object_memory(),
            _level_select_object_instructions(),
            set(PHASE7_LEVEL_SELECT_OBJECT_BINDINGS),
            set(PHASE7_LEVEL_SELECT_OBJECT_VTABLE_ENTRIES),
        )

        self.assertEqual(
            recovered,
            {
                site: (target,)
                for site, (_caller_start, _slot, target) in (
                    PHASE7_LEVEL_SELECT_OBJECT_BINDINGS.items()
                )
            },
        )

    def test_level_select_object_fails_closed_on_family_drift(self) -> None:
        instructions = _level_select_object_instructions()
        targets = set(PHASE7_LEVEL_SELECT_OBJECT_VTABLE_ENTRIES)
        wrong_call = dict(instructions)
        wrong_call[0x000428B3] = lift_x86_function(
            bytes.fromhex("FF5208"),
            base_address=0x000428B3,
            symbol="wrong_level_select_object_slot",
        ).instructions[0]

        for memory, candidate, sites, candidate_targets in (
            (
                _level_select_object_memory(corrupt_vtable_slot=3),
                instructions,
                set(PHASE7_LEVEL_SELECT_OBJECT_BINDINGS),
                targets,
            ),
            (
                _level_select_object_memory(corrupt_caller=0x00042A7E),
                instructions,
                set(PHASE7_LEVEL_SELECT_OBJECT_BINDINGS),
                targets,
            ),
            (
                _level_select_object_memory(),
                wrong_call,
                set(PHASE7_LEVEL_SELECT_OBJECT_BINDINGS),
                targets,
            ),
            (
                _level_select_object_memory(),
                instructions,
                {0x000428B3},
                targets,
            ),
            (
                _level_select_object_memory(),
                instructions,
                set(PHASE7_LEVEL_SELECT_OBJECT_BINDINGS),
                targets - {0x00033880},
            ),
        ):
            with self.subTest(
                instruction_count=len(candidate),
                site_count=len(sites),
                target_count=len(candidate_targets),
            ):
                self.assertEqual(
                    _phase7_level_select_object_targets(
                        memory,
                        candidate,
                        sites,
                        candidate_targets,
                    ),
                    {},
                )

    def test_resource_selection_object_fails_closed_on_family_drift(self) -> None:
        instructions = _resource_selection_object_instructions()
        targets = set(PHASE7_RESOURCE_SELECTION_OBJECT_VTABLE_ENTRIES)
        wrong_call = dict(instructions)
        wrong_call[0x0004436A] = lift_x86_function(
            bytes.fromhex("FF5208"),
            base_address=0x0004436A,
            symbol="wrong_resource_selection_slot",
        ).instructions[0]

        for memory, candidate, sites, candidate_targets in (
            (
                _resource_selection_object_memory(corrupt_vtable_slot=3),
                instructions,
                set(PHASE7_RESOURCE_SELECTION_OBJECT_BINDINGS),
                targets,
            ),
            (
                _resource_selection_object_memory(corrupt_caller=0x0004435A),
                instructions,
                set(PHASE7_RESOURCE_SELECTION_OBJECT_BINDINGS),
                targets,
            ),
            (
                _resource_selection_object_memory(),
                wrong_call,
                set(PHASE7_RESOURCE_SELECTION_OBJECT_BINDINGS),
                targets,
            ),
            (
                _resource_selection_object_memory(),
                instructions,
                {0x0004436A},
                targets,
            ),
            (
                _resource_selection_object_memory(),
                instructions,
                set(PHASE7_RESOURCE_SELECTION_OBJECT_BINDINGS),
                targets - {0x00032EE0},
            ),
        ):
            with self.subTest(
                instruction_count=len(candidate),
                site_count=len(sites),
                target_count=len(candidate_targets),
            ):
                self.assertEqual(
                    _phase7_resource_selection_object_targets(
                        memory,
                        candidate,
                        sites,
                        candidate_targets,
                    ),
                    {},
                )

    def test_new_save_object_vtables_fail_closed_on_family_drift(self) -> None:
        instructions = _new_save_object_instructions()
        targets = set(PHASE7_POST_START_OBJECT_VTABLE_ENTRIES) | set(
            PHASE7_NEW_SAVE_COMPANION_OBJECT_VTABLE_ENTRIES
        )
        wrong_call = dict(instructions)
        wrong_call[0x00045E98] = lift_x86_function(
            bytes.fromhex("FF5208"),
            base_address=0x00045E98,
            symbol="wrong_new_save_companion_slot",
        ).instructions[0]

        for memory, candidate, sites, candidate_targets in (
            (
                _new_save_object_memory(
                    corrupt_vtable=PHASE7_POST_START_OBJECT_VTABLE,
                    corrupt_slot=3,
                ),
                instructions,
                set(PHASE7_NEW_SAVE_OBJECT_BINDINGS),
                targets,
            ),
            (
                _new_save_object_memory(
                    corrupt_vtable=PHASE7_NEW_SAVE_COMPANION_OBJECT_VTABLE,
                    corrupt_slot=3,
                ),
                instructions,
                set(PHASE7_NEW_SAVE_OBJECT_BINDINGS),
                targets,
            ),
            (
                _new_save_object_memory(),
                wrong_call,
                set(PHASE7_NEW_SAVE_OBJECT_BINDINGS),
                targets,
            ),
            (
                _new_save_object_memory(),
                instructions,
                {0x00045141},
                targets,
            ),
            (
                _new_save_object_memory(),
                instructions,
                set(PHASE7_NEW_SAVE_OBJECT_BINDINGS),
                targets - {0x00034880},
            ),
        ):
            with self.subTest(
                instruction_count=len(candidate),
                site_count=len(sites),
                target_count=len(candidate_targets),
            ):
                self.assertEqual(
                    _phase7_new_save_object_targets(
                        memory,
                        candidate,
                        sites,
                        candidate_targets,
                    ),
                    {},
                )

    def test_save_slot_object_vtable_fails_closed_on_family_drift(self) -> None:
        instructions = _save_slot_object_instructions()
        targets = set(PHASE7_SAVE_SLOT_OBJECT_VTABLE_ENTRIES) | set(
            PHASE7_SAVE_SLOT_OWNER_VTABLE_ENTRIES
        )
        wrong_call = dict(instructions)
        wrong_call[0x0003D25C] = lift_x86_function(
            bytes.fromhex("FF5208"),
            base_address=0x0003D25C,
            symbol="wrong_save_slot_object_slot",
        ).instructions[0]

        for memory, candidate, sites, candidate_targets in (
            (
                _save_slot_object_memory(corrupt_slot=3),
                instructions,
                set(PHASE7_SAVE_SLOT_OBJECT_BINDINGS),
                targets,
            ),
            (
                _save_slot_object_memory(corrupt_owner_slot=3),
                instructions,
                set(PHASE7_SAVE_SLOT_OBJECT_BINDINGS),
                targets,
            ),
            (
                _save_slot_object_memory(),
                wrong_call,
                set(PHASE7_SAVE_SLOT_OBJECT_BINDINGS),
                targets,
            ),
            (_save_slot_object_memory(), instructions, set(), targets),
            (
                _save_slot_object_memory(),
                instructions,
                set(PHASE7_SAVE_SLOT_OBJECT_BINDINGS),
                targets - {0x0003A030},
            ),
        ):
            with self.subTest(
                instruction_count=len(candidate),
                site_count=len(sites),
                target_count=len(candidate_targets),
            ):
                self.assertEqual(
                    _phase7_save_slot_object_targets(
                        memory,
                        candidate,
                        sites,
                        candidate_targets,
                    ),
                    {},
                )

    def test_directsound_refcount_vtable_recovers_both_slots_as_one_batch(
        self,
    ) -> None:
        memory = SparseMemory(
            {
                PHASE7_DIRECTSOUND_REFCOUNT_VTABLE: 0x00232BFF,
                PHASE7_DIRECTSOUND_REFCOUNT_VTABLE + 4: 0x0022BF1D,
                PHASE7_DIRECTSOUND_REFCOUNT_VTABLE + 8: 0x0022BF2A,
            }
        )
        targets = {target for _slot, target in PHASE7_DIRECTSOUND_REFCOUNT_BINDINGS.values()}

        recovered = _phase7_directsound_refcount_targets(
            memory,
            _directsound_refcount_instructions(),
            set(PHASE7_DIRECTSOUND_REFCOUNT_BINDINGS),
            targets,
        )

        self.assertEqual(recovered, {0x00232AC1: (0x0022BF1D,), 0x00232CA1: (0x0022BF2A,)})

    def test_directsound_refcount_vtable_fails_closed_on_family_drift(self) -> None:
        instructions = _directsound_refcount_instructions()
        memory = SparseMemory(
            {
                PHASE7_DIRECTSOUND_REFCOUNT_VTABLE: 0x00232BFF,
                PHASE7_DIRECTSOUND_REFCOUNT_VTABLE + 4: 0x0022BF1D,
                PHASE7_DIRECTSOUND_REFCOUNT_VTABLE + 8: 0x0022BF2A,
            }
        )
        targets = {target for _slot, target in PHASE7_DIRECTSOUND_REFCOUNT_BINDINGS.values()}
        wrong_slot = dict(instructions)
        wrong_slot[0x00232CA1] = lift_x86_function(
            bytes.fromhex("FF500C"),
            base_address=0x00232CA1,
            symbol="wrong_directsound_refcount_slot",
        ).instructions[0]
        wrong_memory = SparseMemory(
            {
                PHASE7_DIRECTSOUND_REFCOUNT_VTABLE: 0x00232BFF,
                PHASE7_DIRECTSOUND_REFCOUNT_VTABLE + 4: 0x0022BF1D,
                PHASE7_DIRECTSOUND_REFCOUNT_VTABLE + 8: 0x0022BF1D,
            }
        )
        missing_constructor = dict(instructions)
        del missing_constructor[0x00232928]

        for candidate_memory, candidate_instructions, sites, candidate_targets in (
            (
                wrong_memory,
                instructions,
                set(PHASE7_DIRECTSOUND_REFCOUNT_BINDINGS),
                targets,
            ),
            (
                memory,
                wrong_slot,
                set(PHASE7_DIRECTSOUND_REFCOUNT_BINDINGS),
                targets,
            ),
            (
                memory,
                missing_constructor,
                set(PHASE7_DIRECTSOUND_REFCOUNT_BINDINGS),
                targets,
            ),
            (
                memory,
                instructions,
                {0x00232AC1},
                targets,
            ),
            (
                memory,
                instructions,
                set(PHASE7_DIRECTSOUND_REFCOUNT_BINDINGS),
                targets - {0x0022BF2A},
            ),
        ):
            with self.subTest(
                instruction_count=len(candidate_instructions),
                site_count=len(sites),
                target_count=len(candidate_targets),
            ):
                self.assertEqual(
                    _phase7_directsound_refcount_targets(
                        candidate_memory,
                        candidate_instructions,
                        sites,
                        candidate_targets,
                    ),
                    {},
                )

    def test_directsound_voice_vtable_recovers_complete_object_family(self) -> None:
        recovered = _phase7_directsound_voice_targets(
            _directsound_voice_memory(),
            _directsound_voice_instructions(),
            set(PHASE7_DIRECTSOUND_VOICE_BINDINGS),
            set(PHASE7_DIRECTSOUND_VOICE_VTABLE_ENTRIES),
        )

        self.assertEqual(
            recovered,
            {
                0x00232D9B: (0x00235460,),
                0x00234AF4: (0x00234F1C,),
                0x002351DA: (0x00234EED,),
            },
        )

    def test_directsound_voice_vtable_fails_closed_on_family_drift(self) -> None:
        instructions = _directsound_voice_instructions()
        targets = set(PHASE7_DIRECTSOUND_VOICE_VTABLE_ENTRIES)
        wrong_call = dict(instructions)
        wrong_call[0x00234AF4] = lift_x86_function(
            bytes.fromhex("FF5018"),
            base_address=0x00234AF4,
            symbol="wrong_directsound_voice_slot",
        ).instructions[0]
        added_caller = dict(instructions)
        displacement = (0x00234F36 - (0x00220000 + 5)) & 0xFFFFFFFF
        added_caller[0x00220000] = lift_x86_function(
            b"\xe8" + displacement.to_bytes(4, "little"),
            base_address=0x00220000,
            symbol="added_directsound_voice_constructor_caller",
        ).instructions[0]
        missing_installer = dict(instructions)
        del missing_installer[0x00235510]

        for memory, candidate, sites, candidate_targets in (
            (
                _directsound_voice_memory(corrupt_slot=8),
                instructions,
                set(PHASE7_DIRECTSOUND_VOICE_BINDINGS),
                targets,
            ),
            (
                _directsound_voice_memory(),
                wrong_call,
                set(PHASE7_DIRECTSOUND_VOICE_BINDINGS),
                targets,
            ),
            (
                _directsound_voice_memory(),
                added_caller,
                set(PHASE7_DIRECTSOUND_VOICE_BINDINGS),
                targets,
            ),
            (
                _directsound_voice_memory(),
                missing_installer,
                set(PHASE7_DIRECTSOUND_VOICE_BINDINGS),
                targets,
            ),
            (_directsound_voice_memory(), instructions, set(), targets),
            (
                _directsound_voice_memory(),
                instructions,
                set(PHASE7_DIRECTSOUND_VOICE_BINDINGS),
                targets - {0x00235460},
            ),
        ):
            with self.subTest(
                instruction_count=len(candidate),
                site_count=len(sites),
                target_count=len(candidate_targets),
            ):
                self.assertEqual(
                    _phase7_directsound_voice_targets(
                        memory,
                        candidate,
                        sites,
                        candidate_targets,
                    ),
                    {},
                )

    def test_d150_callback_family_fails_closed_on_caller_drift(self) -> None:
        instructions = _d150_callback_instructions()
        targets = {
            target
            for callback_targets in PHASE7_D150_CALLBACK_TARGETS.values()
            for target in callback_targets
        }
        wrong_install = dict(instructions)
        wrong_install[0x0010D4B2] = lift_x86_function(
            bytes.fromhex("6800D21000"),
            base_address=0x0010D4B2,
            symbol="wrong_d150_callback",
        ).instructions[0]
        extra_caller = dict(instructions)
        displacement = (0x0010D150 - (0x00120000 + 5)) & 0xFFFFFFFF
        extra_caller[0x00120000] = lift_x86_function(
            b"\xe8" + displacement.to_bytes(4, "little"),
            base_address=0x00120000,
            symbol="unexpected_d150_caller",
        ).instructions[0]

        for candidate_instructions, candidate_targets in (
            (wrong_install, targets),
            (extra_caller, targets),
            (instructions, targets - {0x0010D260}),
        ):
            with self.subTest(
                instruction_count=len(candidate_instructions),
                target_count=len(candidate_targets),
            ):
                self.assertEqual(
                    _phase7_d150_callback_targets(
                        candidate_instructions,
                        set(PHASE7_D150_CALLBACK_TARGETS),
                        candidate_targets,
                    ),
                    {},
                )

    def test_bb0_callback_binds_the_only_caller_installed_target(self) -> None:
        recovered = _phase7_bb0_callback_targets(
            _bb0_callback_instructions(),
            {PHASE7_BB0_CALLBACK_SITE},
            {PHASE7_BB0_CALLBACK_TARGET},
        )

        self.assertEqual(
            recovered,
            {PHASE7_BB0_CALLBACK_SITE: (PHASE7_BB0_CALLBACK_TARGET,)},
        )
        self.assertEqual(PHASE7_BB0_CALLBACK_INSTALL, 0x00109BD5)

    def test_bb0_callback_fails_closed_on_caller_drift(self) -> None:
        instructions = _bb0_callback_instructions()
        wrong_install = dict(instructions)
        wrong_install[PHASE7_BB0_CALLBACK_INSTALL] = lift_x86_function(
            bytes.fromhex("6800D21000"),
            base_address=PHASE7_BB0_CALLBACK_INSTALL,
            symbol="wrong_bb0_callback",
        ).instructions[0]
        extra_caller = dict(instructions)
        address = 0x00120000
        displacement = (0x00112BB0 - (address + 5)) & 0xFFFFFFFF
        extra_caller[address] = lift_x86_function(
            b"\xe8" + displacement.to_bytes(4, "little"),
            base_address=address,
            symbol="unexpected_bb0_caller",
        ).instructions[0]

        for candidate_instructions, sites, targets in (
            (wrong_install, {PHASE7_BB0_CALLBACK_SITE}, {PHASE7_BB0_CALLBACK_TARGET}),
            (extra_caller, {PHASE7_BB0_CALLBACK_SITE}, {PHASE7_BB0_CALLBACK_TARGET}),
            (instructions, set(), {PHASE7_BB0_CALLBACK_TARGET}),
            (instructions, {PHASE7_BB0_CALLBACK_SITE}, set()),
        ):
            with self.subTest(
                instruction_count=len(candidate_instructions),
                site_count=len(sites),
                target_count=len(targets),
            ):
                self.assertEqual(
                    _phase7_bb0_callback_targets(
                        candidate_instructions,
                        sites,
                        targets,
                    ),
                    {},
                )

    def test_asset_stream_callback_binds_complete_installed_record(self) -> None:
        targets = {
            target for _slot, target, _installer in PHASE7_ASSET_STREAM_CALLBACK_RECORD_SLOTS
        }

        recovered = _phase7_asset_stream_callback_targets(
            _asset_stream_callback_instructions(),
            set(PHASE7_ASSET_STREAM_CALLBACK_BINDINGS),
            targets,
        )

        self.assertEqual(
            recovered,
            {
                site: (target,)
                for site, (_slot, target) in PHASE7_ASSET_STREAM_CALLBACK_BINDINGS.items()
            },
        )

    def test_asset_stream_callback_fails_closed_on_record_drift(self) -> None:
        instructions = _asset_stream_callback_instructions()
        targets = {
            target for _slot, target, _installer in PHASE7_ASSET_STREAM_CALLBACK_RECORD_SLOTS
        }
        wrong_slot = dict(instructions)
        wrong_slot[0x0011052D] = lift_x86_function(
            bytes.fromhex("C7461080031100"),
            base_address=0x0011052D,
            symbol="wrong_asset_stream_slot",
        ).instructions[0]
        extra_installer = dict(instructions)
        extra_installer[0x00120000] = lift_x86_function(
            bytes.fromhex("B880031100"),
            base_address=0x00120000,
            symbol="unexpected_asset_stream_installer",
        ).instructions[0]
        extra_helper_caller = dict(instructions)
        address = 0x00120000
        entry = 0x00112EA0
        displacement = (entry - (address + 5)) & 0xFFFFFFFF
        extra_helper_caller[address] = lift_x86_function(
            b"\xe8" + displacement.to_bytes(4, "little"),
            base_address=address,
            symbol="unexpected_asset_stream_helper_caller",
        ).instructions[0]

        for candidate, sites, candidate_targets in (
            (wrong_slot, set(PHASE7_ASSET_STREAM_CALLBACK_BINDINGS), targets),
            (extra_installer, set(PHASE7_ASSET_STREAM_CALLBACK_BINDINGS), targets),
            (
                extra_helper_caller,
                set(PHASE7_ASSET_STREAM_CALLBACK_BINDINGS),
                targets,
            ),
            (instructions, set(), targets),
            (
                instructions,
                set(PHASE7_ASSET_STREAM_CALLBACK_BINDINGS),
                targets - {0x00110490},
            ),
        ):
            with self.subTest(
                instruction_count=len(candidate),
                site_count=len(sites),
                target_count=len(candidate_targets),
            ):
                self.assertEqual(
                    _phase7_asset_stream_callback_targets(
                        candidate,
                        sites,
                        candidate_targets,
                    ),
                    {},
                )

    def test_resource_converter_callback_binds_complete_five_site_family(self) -> None:
        targets = {
            target for _slot, target in PHASE7_RESOURCE_CONVERTER_CALLBACK_VECTOR
        }

        recovered = _phase7_resource_converter_callback_targets(
            _resource_converter_callback_memory(),
            _resource_converter_callback_instructions(),
            set(PHASE7_RESOURCE_CONVERTER_CALLBACK_BINDINGS),
            targets,
        )

        self.assertEqual(
            recovered,
            {
                site: (target,)
                for site, (_slot, target) in (
                    PHASE7_RESOURCE_CONVERTER_CALLBACK_BINDINGS.items()
                )
            },
        )
        self.assertEqual(
            set(PHASE7_RESOURCE_CONVERTER_INSTALLER_BINDINGS.values()),
            set(PHASE7_RESOURCE_CONVERTER_CALLBACK_VECTOR),
        )
        self.assertEqual(
            PHASE7_RESOURCE_CONVERTER_OBSERVED_BOUNDARY,
            (0x00105E72, 0x00105830),
        )

    def test_resource_converter_callback_fails_closed_on_provenance_drift(self) -> None:
        memory = _resource_converter_callback_memory()
        instructions = _resource_converter_callback_instructions()
        sites = set(PHASE7_RESOURCE_CONVERTER_CALLBACK_BINDINGS)
        targets = {
            target for _slot, target in PHASE7_RESOURCE_CONVERTER_CALLBACK_VECTOR
        }
        wrong_descriptor = _resource_converter_callback_memory()
        wrong_descriptor.write_u32(PHASE7_RESOURCE_CONVERTER_DESCRIPTOR, 0)
        wrong_site = dict(instructions)
        wrong_site[0x00105E72] = lift_x86_function(
            bytes.fromhex("FFD1"),
            base_address=0x00105E72,
            symbol="wrong_resource_converter_callback_register",
        ).instructions[0]
        extra_converter_caller = dict(instructions)
        address = 0x00120000
        displacement = (0x00105C90 - (address + 5)) & 0xFFFFFFFF
        extra_converter_caller[address] = lift_x86_function(
            b"\xe8" + displacement.to_bytes(4, "little"),
            base_address=address,
            symbol="unexpected_resource_converter_caller",
        ).instructions[0]

        for candidate_memory, candidate_instructions, candidate_sites, candidate_targets in (
            (wrong_descriptor, instructions, sites, targets),
            (memory, wrong_site, sites, targets),
            (memory, extra_converter_caller, sites, targets),
            (memory, instructions, sites - {0x00105A89}, targets),
            (memory, instructions, sites, targets - {0x00105450}),
        ):
            with self.subTest(
                instruction_count=len(candidate_instructions),
                site_count=len(candidate_sites),
                target_count=len(candidate_targets),
            ):
                self.assertEqual(
                    _phase7_resource_converter_callback_targets(
                        candidate_memory,
                        candidate_instructions,
                        candidate_sites,
                        candidate_targets,
                    ),
                    {},
                )

    def test_supported_workload_registry_covers_exact_measured_abi_targets(self) -> None:
        self.assertEqual(
            set(SUPPORTED_WORKLOAD_SERVICE_SPECS),
            {
                0x31F10300,
                0x31F10700,
                0x31F10800,
                0xE0000050,
                0xE0000070,
                0xE0000080,
                0xE0000200,
                0xE0000280,
                0xE0000360,
                0xE0000370,
                0xE00003A0,
                0xE0000570,
                0xE00005C0,
            },
        )
        self.assertEqual(len(PHASE7_PROFILED_BOOT_SERVICE_SPECS), 93)
        self.assertNotIn(0x000CB690, PHASE7_PROFILED_BOOT_SERVICE_SPECS)
        self.assertEqual(
            PHASE7_PROFILED_BOOT_SERVICE_SPECS[0x000CC2F0],
            {
                "shim_name": "TitleMusicModeSet",
                "runtime_kind": 13,
                "runtime_value": 19,
                "stack_cleanup_bytes": 4,
                "boundary": "audio",
                "normal_live_body": "implemented-native32",
            },
        )
        self.assertEqual(
            PHASE7_NATIVE_BUFFER_CONFIGURATION_SERVICE_TARGETS,
            {
                0x0022D85E,
                0x0022D87A,
                0x0022D8F6,
                0x0022E754,
                0x0022E770,
                0x0022E78C,
                0x0022E8C4,
                0x0022E8E8,
                0x0022E908,
            },
        )
        self.assertEqual(
            PHASE7_NATIVE_AUDIO_LIFETIME_SERVICE_TARGETS,
            {0x0022C11B, 0x0022CC0B},
        )
        self.assertEqual(
            PHASE7_NATIVE_XINPUT_CONTROL_SERVICE_TARGETS,
            {0x0028CF96, 0x0028D1EC},
        )
        self.assertEqual(
            {
                target: (
                    PHASE7_PROFILED_BOOT_SERVICE_SPECS[target]["shim_name"],
                    PHASE7_PROFILED_BOOT_SERVICE_SPECS[target][
                        "stack_cleanup_bytes"
                    ],
                )
                for target in PHASE7_NATIVE_XINPUT_CONTROL_SERVICE_TARGETS
            },
            {
                0x0028CF96: ("XInputClose", 4),
                0x0028D1EC: ("XInputSetState", 8),
            },
        )
        self.assertEqual(
            {
                target: (
                    PHASE7_PROFILED_BOOT_SERVICE_SPECS[target]["shim_name"],
                    PHASE7_PROFILED_BOOT_SERVICE_SPECS[target]["runtime_value"],
                    PHASE7_PROFILED_BOOT_SERVICE_SPECS[target][
                        "stack_cleanup_bytes"
                    ],
                )
                for target in PHASE7_NATIVE_AUDIO_LIFETIME_SERVICE_TARGETS
            },
            {
                0x0022C11B: ("DirectSoundBufferRelease", 29, 4),
                0x0022CC0B: ("DirectSoundStreamRelease", 30, 4),
            },
        )
        self.assertEqual(
            {
                target: (
                    PHASE7_PROFILED_BOOT_SERVICE_SPECS[target]["shim_name"],
                    PHASE7_PROFILED_BOOT_SERVICE_SPECS[target]["runtime_value"],
                    PHASE7_PROFILED_BOOT_SERVICE_SPECS[target][
                        "stack_cleanup_bytes"
                    ],
                )
                for target in {
                    0x0022D85E,
                    0x0022D87A,
                    0x0022D8F6,
                    0x0022E754,
                    0x0022E770,
                    0x0022E78C,
                    0x0022E8C4,
                    0x0022E8E8,
                    0x0022E908,
                }
            },
            {
                0x0022D85E: ("DirectSoundBufferSetHeadroom", 20, 8),
                0x0022D87A: ("DirectSoundBufferSetMixBinVolumes", 21, 8),
                0x0022D8F6: ("DirectSoundBufferSetLoopRegion", 22, 12),
                0x0022E754: ("DirectSoundBufferSetOutputBuffer", 23, 8),
                0x0022E770: ("DirectSoundBufferSetMixBins", 24, 8),
                0x0022E78C: ("DirectSoundBufferSetAllParameters", 25, 12),
                0x0022E8C4: ("DirectSoundBufferSetRolloffCurve", 26, 16),
                0x0022E8E8: ("DirectSoundBufferSetI3DL2Source", 27, 12),
                0x0022E908: ("DirectSoundBufferSetPlayRegion", 28, 12),
            },
        )
        self.assertEqual(
            PHASE7_CAPTURED_SERVICE_IMPORT_CELLS,
            {0x293CDC: 0xE0000400, 0x293E58: 0xE0000030},
        )
        self.assertFalse(
            set(SUPPORTED_WORKLOAD_SERVICE_SPECS) & set(PHASE7_PROFILED_BOOT_SERVICE_SPECS)
        )
        self.assertEqual(
            PHASE7_PROFILED_BOOT_SERVICE_SPECS[0x31F10200]["normal_live_body"],
            "implemented-native32",
        )
        self.assertEqual(
            PHASE7_PROFILED_BOOT_SERVICE_SPECS[0xE0000120]["stack_cleanup_bytes"],
            24,
        )
        self.assertEqual(
            PHASE7_PROFILED_BOOT_SERVICE_SPECS[0xE0000120]["normal_live_body"],
            "implemented-native32",
        )
        self.assertNotIn(IA32_VBLANK_RETURN_SENTINEL, SUPPORTED_WORKLOAD_SERVICE_SPECS)

    def test_phase7_captured_indirect_call_batch_is_finite_and_address_named(self) -> None:
        self.assertEqual(len(PHASE7_CAPTURED_REGISTER_IMPORT_BINDINGS), 40)
        self.assertEqual(
            PHASE7_CAPTURED_REGISTER_IMPORT_BINDINGS[0x21AAA7],
            (0x21AA8F, 0x293CAC, 0xE00003C0),
        )
        self.assertEqual(
            PHASE7_CAPTURED_REGISTER_IMPORT_BINDINGS[0x21AABA],
            (0x21AA8F, 0x293CAC, 0xE00003C0),
        )
        self.assertEqual(len(PHASE7_CAPTURED_CALLBACK_CELLS), 34)
        self.assertEqual(PHASE7_CAPTURED_CALLBACK_CELLS[0x5ADCB4], (0xF4780,))
        self.assertEqual(PHASE7_CAPTURED_CALLBACK_CELLS[0x5ADCC0], (0xF0AF0,))
        self.assertEqual(PHASE7_CAPTURED_CALLBACK_CELLS[0x5ADDBC], (0x120F60,))
        self.assertEqual(PHASE7_CAPTURED_CALLBACK_CELLS[0x5ADDD0], (0x11F398,))
        self.assertEqual(PHASE7_CAPTURED_CALLBACK_CELLS[0x5ADDD4], (0x11F2ED,))
        self.assertEqual(PHASE7_CAPTURED_CALLBACK_CELLS[0x5ADDD8], (0x1206BF,))
        self.assertEqual(PHASE7_CAPTURED_CALLBACK_CELLS[0x5ADDDC], (0xF1FE0,))
        self.assertEqual(PHASE7_CAPTURED_CALLBACK_CELLS[0x5ADDE0], (0xE8720,))
        self.assertEqual(PHASE7_CAPTURED_CALLBACK_CELLS[0x5ADDE4], (0xE8730,))
        self.assertEqual(
            set(PHASE7_CAPTURED_INDIRECT_TARGETS),
            {
                0x11086,
                0x14920,
                0x3F7AF,
                0x3F7BB,
                0x4404E,
                0x4475F,
                0x4CB30,
                0xE689B,
                0x10AA58,
                0x12799E,
                0x28C571,
            },
        )
        self.assertEqual(PHASE7_CAPTURED_INDIRECT_TARGETS[0x10AA58], (0xD8890,))
        self.assertEqual(PHASE7_CAPTURED_INDIRECT_TARGETS[0x14920], (0x154B0,))
        self.assertEqual(PHASE7_CAPTURED_INDIRECT_TARGETS[0x4404E], (0x32EE0,))
        self.assertEqual(PHASE7_CAPTURED_INDIRECT_TARGETS[0x4475F], (0x3A030,))
        self.assertEqual(PHASE7_CAPTURED_INDIRECT_TARGETS[0x4CB30], (0x309F0,))
        self.assertEqual(PHASE7_CAPTURED_INDIRECT_TARGETS[0xE689B], (0xE24E1,))
        self.assertEqual(PHASE7_CAPTURED_INDIRECT_TARGETS[0x12799E], (0x127965,))
        self.assertEqual(
            PHASE7_CAPTURED_INDIRECT_TARGETS[0x28C571],
            (0x28C180, 0x28C32F, 0x28C9C8),
        )
        self.assertEqual(len(PHASE7_CAPTURED_INDIRECT_TARGETS[0x3F7BB]), 29)
        self.assertEqual(
            PHASE7_CAPTURED_VTABLE_SLOT_BINDINGS,
            {
                0xD865B: ((0x295D38, 0xD9590),),
                0x22FC0E: ((0x2C9450, 0x22E932),),
            },
        )

    def test_vblank_return_target_becomes_an_explicit_scheduler_boundary(self) -> None:
        profile = self.profile()
        profile = Ia32CoverageProfile(
            workload=profile.workload,
            mode=profile.mode,
            targets=profile.targets,
            transitions=profile.transitions
            + (
                {
                    "source": 0x000C0200,
                    "target": IA32_VBLANK_RETURN_SENTINEL,
                    "count": 1,
                },
            ),
            boundary_exits=profile.boundary_exits,
        )

        normalized = _phase6_profile_scheduler_boundaries(profile)

        boundary = normalized.boundary_exits[-1]
        self.assertNotIn(
            IA32_VBLANK_RETURN_SENTINEL, {record["target"] for record in normalized.transitions}
        )
        self.assertEqual(boundary["target"], IA32_VBLANK_RETURN_SENTINEL)
        self.assertEqual(boundary["boundary"], "scheduler")

    def test_detached_worker_reserves_low_title_image_and_high_exchange_views(self) -> None:
        assembly = _detached_guest_reservation_assembly()
        source = _persistent_c_source(
            page_addresses=(DETACHED_GUEST_RESERVATION_START,),
            mapped_page_addresses=(DETACHED_GUEST_RESERVATION_START,),
            guest_virtual_size=0x1000,
            code_spans=((DETACHED_GUEST_RESERVATION_START, b"\x90\x90"),),
            stop_eip=DETACHED_GUEST_RESERVATION_START + 1,
            detached_guest=True,
        )

        self.assertIn('.section .text$a,"xr"', assembly)
        self.assertIn(
            f".fill {DETACHED_GUEST_RESERVATION_END - DETACHED_GUEST_RESERVATION_START}",
            assembly,
        )
        self.assertIn('#pragma code_seg(".text$z")', source)
        self.assertIn("MapViewOfFileEx", source)
        self.assertIn("kExchangeViewAddresses", source)
        self.assertIn("PAGE_NOACCESS, &previous_protection", source)
        self.assertIn("kDetachedGuestSpans[index].code", source)

    def test_hot_scc_ranks_before_lower_address_and_closes(self) -> None:
        result = plan_ia32_coverage_growth(
            self.plan,
            self.profile(),
            registered_service_targets=(0xE0001000,),
        ).coverage_growth_map
        self.assertEqual(result["ranking"][0]["targets"], [0x000C0100, 0x000C0200])
        self.assertEqual(result["lesson_one_scc_rank"], 1)
        self.assertTrue(result["promotion_eligible"])
        self.assertTrue(result["final_cutover_candidate"])
        self.assertTrue(all(record["closed"] for record in result["slices"]))

    def test_discovery_names_next_store_target_but_cannot_promote(self) -> None:
        profile = self.profile(mode="diagnostic-discovery")
        profile = Ia32CoverageProfile(
            workload=profile.workload,
            mode=profile.mode,
            targets=profile.targets,
            transitions=profile.transitions
            + ({"source": 0x000C0200, "target": 0x000C4000, "count": 1},),
            boundary_exits=profile.boundary_exits,
            frontier_interpreter_invocations=1,
            frontier_interpreter_steps=3,
        )
        result = plan_ia32_coverage_growth(
            self.plan,
            profile,
            registered_service_targets=(0xE0001000,),
        ).coverage_growth_map
        self.assertEqual(result["required_next_decoded_store_targets"], [0x000C4000])
        self.assertFalse(result["promotion_eligible"])

    def test_normal_validation_rejects_unknown_target(self) -> None:
        profile = self.profile()
        profile = Ia32CoverageProfile(
            workload=profile.workload,
            mode=profile.mode,
            targets=profile.targets,
            transitions=profile.transitions
            + ({"source": 0x000C0200, "target": 0x000C4000, "count": 1},),
            boundary_exits=profile.boundary_exits,
        )
        with self.assertRaisesRegex(Ia32BackendError, "unknown executable targets"):
            plan_ia32_coverage_growth(
                self.plan,
                profile,
                registered_service_targets=(0xE0001000,),
            )

    def test_profile_loader_round_trips_canonical_fields(self) -> None:
        profile = self.profile()
        record = {
            "format": "b2-recomp-ia32-coverage-profile",
            "version": 1,
            "workload": profile.workload,
            "mode": profile.mode,
            "targets": list(profile.targets),
            "transitions": list(profile.transitions),
            "boundary_exits": list(profile.boundary_exits),
            "frontier_interpreter_invocations": 0,
            "frontier_interpreter_steps": 0,
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "profile.json"
            path.write_text(json.dumps(record), encoding="utf-8")
            loaded = load_ia32_coverage_profile(path)
        self.assertEqual(loaded.workload, profile.workload)
        self.assertEqual(loaded.targets, profile.targets)

    def test_performance_debug_report_converts_measured_slices(self) -> None:
        report = {
            "workload": "lesson-one-capture",
            "hot_path_analysis": {
                "status": "complete",
                "targets": [
                    {
                        "target": address,
                        "estimated_native_time_us": 10.0
                        if address in {0x000C0100, 0x000C0200}
                        else 0.1,
                        "guest_steps": 100,
                        "module_calls": 10,
                        "dominant_execution_lane": "worker" if address == 0x000C0300 else "primary",
                        "module_exit_reasons": {"yield": 3 if address == 0x000C0200 else 0},
                    }
                    for address in (0x000C0000, 0x000C0100, 0x000C0200, 0x000C0300)
                ],
                "transitions": {
                    "edges": [
                        {"entry_target": 0x000C0000, "exit_target": 0x000C0100, "module_calls": 1},
                        {"entry_target": 0x000C0100, "exit_target": 0x000C0200, "module_calls": 8},
                        {"entry_target": 0x000C0200, "exit_target": 0x000C0100, "module_calls": 7},
                        {"entry_target": 0x000C0100, "exit_target": 0xE0001000, "module_calls": 8},
                    ]
                },
                "cycles": {"hot_cycles": [{"targets": ["0x000C0100", "0x000C0200"]}]},
                "native_host_services": [{"target": 0xE0001000, "boundary": "render"}],
            },
            "profiling": {
                "accepted": True,
                "static_runtime": {
                    "clean": True,
                    "developer_live_compilation_enabled": False,
                    "frontier_interpreter_invocation_count": 0,
                    "frontier_interpreter_steps": 0,
                    "native_promotion_enabled": False,
                },
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "performance-debug-report.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            profile = coverage_profile_from_performance_debug_report(path)
        slices = {int(record["address"]): record["slice"] for record in profile.targets}
        self.assertEqual(slices[0x000C0100], "lesson-one-hot-scc")
        self.assertEqual(slices[0x000C0000], "boot-frontend-continuity")
        self.assertEqual(slices[0x000C0300], "worker-vblank-cold")
        self.assertEqual(
            {record["boundary"] for record in profile.boundary_exits},
            {"service", "render"},
        )


if __name__ == "__main__":
    unittest.main()
