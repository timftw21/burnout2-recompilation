from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from tools.recomp.ia32_native_backend import (
    EXECUTION_CONTRACT_FORMAT,
    MXCSR_DEFINED_MASK,
    MXCSR_PHASE0_COMPARISON_MASK,
    MXCSR_STICKY_STATUS_MASK,
    PHASE0_EXECUTION_CONTRACT_ID,
    X87_CONTROL_WORD_OBSERVABLE_MASK,
    ia32_execution_contract,
    ia32_execution_contract_id,
    normalize_ia32_phase0_state,
)
from tools.recomp.ia32_proof_contract import (
    ASSET_FREE_COMPUTE_CAPSULE_ID,
    ASSET_FREE_SERVICE_CAPSULE_ID,
    PROOF_SET_FORMAT,
    build_asset_free_phase0_capsules,
    run_asset_free_phase0_proofs,
)
from tools.recomp.x86_lifter import CpuState


class Ia32ProofContractTests(unittest.TestCase):
    def test_execution_contract_freezes_thunk_timing_and_state_abis(self) -> None:
        contract = ia32_execution_contract()

        self.assertEqual(contract["format"], EXECUTION_CONTRACT_FORMAT)
        self.assertEqual(contract["entry_exit_thunk"]["abi_version"], 1)
        self.assertEqual(contract["host_service_thunk"]["abi_version"], 1)
        self.assertEqual(contract["timing"]["version"], 1)
        self.assertEqual(
            contract["observable_state"]["x87_control_word_mask"],
            X87_CONTROL_WORD_OBSERVABLE_MASK,
        )
        self.assertEqual(
            contract["observable_state"]["mxcsr_defined_mask"],
            MXCSR_DEFINED_MASK,
        )
        self.assertEqual(
            contract["observable_state"]["mxcsr_sticky_status_mask"],
            MXCSR_STICKY_STATUS_MASK,
        )
        self.assertEqual(ia32_execution_contract_id(), PHASE0_EXECUTION_CONTRACT_ID)

    def test_observable_state_applies_phase0_saved_state_policy(self) -> None:
        state = CpuState.with_registers(eax=42)
        state.fpu_control_word = 0xFFFF
        state.mxcsr = 0xFFFFFFFF
        normalized = normalize_ia32_phase0_state(state)

        self.assertEqual(normalized.fpu_control_word, X87_CONTROL_WORD_OBSERVABLE_MASK)
        self.assertEqual(normalized.mxcsr, MXCSR_PHASE0_COMPARISON_MASK)
        self.assertEqual(normalized.get_register("eax"), 42)
        self.assertEqual(state.fpu_control_word, 0xFFFF)
        self.assertEqual(state.mxcsr, 0xFFFFFFFF)

    def test_asset_free_capsules_have_reproducible_content_identities(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = build_asset_free_phase0_capsules(root / "first")
            second = build_asset_free_phase0_capsules(root / "second")

        self.assertEqual(
            [(fixture.name, fixture.capsule.capsule_id) for fixture in first],
            [(fixture.name, fixture.capsule.capsule_id) for fixture in second],
        )
        self.assertEqual(
            [fixture.capsule.capsule_id for fixture in first],
            [ASSET_FREE_COMPUTE_CAPSULE_ID, ASSET_FREE_SERVICE_CAPSULE_ID],
        )

    @unittest.skipUnless(
        sys.platform == "win32" and shutil.which("clang-cl") and shutil.which("lld-link"),
        "the IA-32 Phase-0 proof gate requires the supported Windows LLVM toolchain",
    )
    def test_asset_free_phase0_proof_set_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report = run_asset_free_phase0_proofs(Path(temp_dir))

        self.assertEqual(report["format"], PROOF_SET_FORMAT)
        self.assertEqual(report["execution_contract_id"], ia32_execution_contract_id())
        self.assertTrue(report["passed"])
        self.assertEqual(len(report["proofs"]), 2)
        self.assertTrue(all(proof["artifact_reproducible"] for proof in report["proofs"]))
        self.assertTrue(all(proof["passed"] for proof in report["proofs"]))


if __name__ == "__main__":
    unittest.main()
