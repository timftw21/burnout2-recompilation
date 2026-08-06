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
    _persistent_c_source,
    _phase6_profile_scheduler_boundaries,
    _phase6_static_jump_table_targets,
    _phase7_base_manager_object_targets,
    _phase7_asset_stream_callback_targets,
    _phase7_bb0_callback_targets,
    _phase7_d150_callback_targets,
    _phase7_directsound_refcount_targets,
    _phase7_directsound_voice_targets,
    _phase7_function_pair_helper_targets,
    _phase7_post_start_object_targets,
    _phase7_save_slot_object_targets,
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


def _save_slot_object_instructions() -> dict[int, X86Instruction]:
    return {
        address: lift_x86_function(
            bytes.fromhex(encoded),
            base_address=address,
            symbol=f"save_slot_object_{address:08x}",
        ).instructions[0]
        for address, encoded in PHASE7_SAVE_SLOT_OBJECT_INSTRUCTION_BYTES.items()
    }


def _save_slot_object_memory(*, corrupt_slot: int | None = None) -> SparseMemory:
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


class Ia32CoverageGrowthTests(unittest.TestCase):
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

    def test_save_slot_object_vtable_recovers_three_slot_c_sites(self) -> None:
        recovered = _phase7_save_slot_object_targets(
            _save_slot_object_memory(),
            _save_slot_object_instructions(),
            set(PHASE7_SAVE_SLOT_OBJECT_BINDINGS),
            set(PHASE7_SAVE_SLOT_OBJECT_VTABLE_ENTRIES),
        )

        self.assertEqual(
            recovered,
            {site: (target,) for site, (_slot, target) in PHASE7_SAVE_SLOT_OBJECT_BINDINGS.items()},
        )

    def test_save_slot_object_vtable_fails_closed_on_family_drift(self) -> None:
        instructions = _save_slot_object_instructions()
        targets = set(PHASE7_SAVE_SLOT_OBJECT_VTABLE_ENTRIES)
        wrong_call = dict(instructions)
        wrong_call[0x0004486A] = lift_x86_function(
            bytes.fromhex("FF5008"),
            base_address=0x0004486A,
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
        self.assertEqual(len(PHASE7_PROFILED_BOOT_SERVICE_SPECS), 81)
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
