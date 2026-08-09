from __future__ import annotations

import unittest

from tools.recomp.audit_ia32_boundaries import (
    _manifest_guard_map,
    _operand_family,
    _optimization_budget,
    _require_provenance,
)
from tools.recomp.ia32_native_backend import (
    PHASE7_OFFLINE_BOUNDARY_OPTIMIZATIONS,
    PHASE7_OFFLINE_VTABLE_RDATA_START,
    Ia32BackendError,
    _phase7_offline_address_taken_targets,
    _phase7_offline_closed_vtable_targets,
)
from tools.recomp.x86_lifter import Operand, SparseMemory, X86Instruction


class Ia32OfflineBoundaryAuditTests(unittest.TestCase):
    def test_provenance_mismatch_fails_closed(self) -> None:
        _require_provenance("capsule ID", "same", "same")
        with self.assertRaisesRegex(Ia32BackendError, "capsule ID mismatch"):
            _require_provenance("capsule ID", "new", "old")

    def test_groups_virtual_slots_without_runtime_execution(self) -> None:
        instruction = X86Instruction(
            address=0x1234,
            size=3,
            mnemonic="call",
            operands=(Operand.memory(base="eax", displacement=0x18),),
            bytes_hex="FF5018",
        )

        self.assertEqual(
            _operand_family(instruction),
            ("virtual-slot", "base=eax;slot=0x18"),
        )

    def test_optimization_budget_requires_one_runtime_change_per_twenty(self) -> None:
        baseline_ids = tuple(
            str(record["id"])
            for record in PHASE7_OFFLINE_BOUNDARY_OPTIMIZATIONS[:18]
        )
        seventh_milestone = _optimization_budget(150, baseline_ids)
        eighth_milestone = _optimization_budget(160, baseline_ids)
        ninth_milestone = _optimization_budget(180, baseline_ids)

        self.assertEqual(seventh_milestone["required_optimization_count"], 7)
        self.assertEqual(seventh_milestone["new_optimization_count"], 8)
        self.assertTrue(seventh_milestone["passed"])
        self.assertEqual(eighth_milestone["required_optimization_count"], 8)
        self.assertTrue(eighth_milestone["passed"])
        self.assertEqual(ninth_milestone["required_optimization_count"], 9)
        self.assertFalse(ninth_milestone["passed"])

    def test_candidate_budget_credits_only_manifest_optimizations(self) -> None:
        budget = _optimization_budget(
            40,
            (),
            (
                {
                    "id": "compiled-runtime-change",
                    "kind": "runtime",
                    "boundary_credit": 20,
                },
            ),
        )

        self.assertEqual(budget["required_optimization_count"], 2)
        self.assertEqual(budget["credited_boundary_count"], 20)
        self.assertFalse(budget["passed"])

    def test_candidate_manifest_preserves_new_non_indirect_guards(self) -> None:
        guards = _manifest_guard_map(
            {
                "guarded_boundaries": [
                    {"address": 0x1000, "reason": "indirect-target gap"},
                    {
                        "address": 0x2000,
                        "reason": "control transfer exits the verified execution island",
                    },
                ]
            }
        )

        self.assertEqual(set(guards), {0x1000, 0x2000})

    def test_closed_vtable_corpus_recovers_low_virtual_call_offline(self) -> None:
        base = PHASE7_OFFLINE_VTABLE_RDATA_START + 0x40
        first_target = 0x00110000
        slot_target = 0x00120000
        memory = SparseMemory(
            {
                0x4000: base,
                base: first_target,
                base + 0x0C: slot_target,
            }
        )
        site = 0x00050000
        instruction = X86Instruction(
            address=site,
            size=3,
            mnemonic="call",
            operands=(Operand.memory(base="eax", displacement=0x0C),),
            bytes_hex="FF500C",
        )

        self.assertEqual(
            _phase7_offline_closed_vtable_targets(
                memory,
                {site: instruction},
                {site},
                {first_target, slot_target},
            ),
            {site: (slot_target,)},
        )

    def test_address_taken_corpus_closes_only_aot_callback_targets(self) -> None:
        immediate_target = 0x00110000
        immutable_target = 0x00120000
        callback_cell_target = 0x00130000
        callback_cell = 0x00400000
        decoded_rdata_decoy = PHASE7_OFFLINE_VTABLE_RDATA_START + 0x80
        register_site = 0x00050000
        absolute_site = 0x00050010
        instructions = {
            0x00040000: X86Instruction(
                address=0x00040000,
                size=5,
                mnemonic="mov",
                operands=(
                    Operand.register("eax"),
                    Operand.immediate_u32(immediate_target),
                ),
                bytes_hex="B800001100",
            ),
            register_site: X86Instruction(
                address=register_site,
                size=2,
                mnemonic="call",
                operands=(Operand.register("eax"),),
                bytes_hex="FFD0",
            ),
            absolute_site: X86Instruction(
                address=absolute_site,
                size=6,
                mnemonic="call",
                operands=(Operand.memory(absolute=callback_cell),),
                bytes_hex="FF1500004000",
            ),
            decoded_rdata_decoy: X86Instruction(
                address=decoded_rdata_decoy,
                size=1,
                mnemonic="ret",
                bytes_hex="C3",
            ),
        }
        memory = SparseMemory(
            {
                PHASE7_OFFLINE_VTABLE_RDATA_START: immutable_target,
                callback_cell: callback_cell_target,
            }
        )

        recovered = _phase7_offline_address_taken_targets(
            memory,
            instructions,
            {register_site, absolute_site},
            {
                immediate_target,
                immutable_target,
                callback_cell_target,
                decoded_rdata_decoy,
            },
        )

        expected = (
            immediate_target,
            immutable_target,
            callback_cell_target,
        )
        self.assertEqual(recovered[register_site], expected)
        self.assertEqual(recovered[absolute_site], expected)
        self.assertNotIn(decoded_rdata_decoy, recovered[register_site])


if __name__ == "__main__":
    unittest.main()
