from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from tools.recomp.ia32_native_backend import (
    DECODED_BLOCK_STORE_VERSION,
    PHASE2_DECODED_STORE_ARTIFACT_CONTRACT_ID,
    Ia32BackendError,
    build_ia32_decoded_store_artifact,
    ia32_decoded_store_artifact_contract,
    ia32_decoded_store_artifact_contract_id,
    inspect_ia32_decoded_store,
    load_ia32_slice_artifact,
    validate_ia32_decoded_store_artifact_sources,
)
from tools.recomp.ia32_proof_contract import (
    PHASE2_PROOF_SET_FORMAT,
    build_asset_free_phase2_fixture,
    run_asset_free_phase2_proofs,
)
from tools.xbe.synthetic_fixture import build_synthetic_xbe


IA32_INTEGRATION_AVAILABLE = (
    sys.platform == "win32" and shutil.which("clang-cl") and shutil.which("lld-link")
)


class Ia32DecodedStoreArtifactTests(unittest.TestCase):
    def test_phase2_contract_freezes_offline_inputs_outputs_and_runtime_exclusions(self) -> None:
        contract = ia32_decoded_store_artifact_contract()

        self.assertEqual(
            ia32_decoded_store_artifact_contract_id(),
            PHASE2_DECODED_STORE_ARTIFACT_CONTRACT_ID,
        )
        self.assertEqual(
            contract["decoded_block_store"]["version"],
            DECODED_BLOCK_STORE_VERSION,
        )
        self.assertEqual(len(contract["required_outputs"]), 5)
        self.assertTrue(all(not value for value in contract["normal_execution"].values()))

    def test_store_snapshot_binds_decoded_metadata_to_xbe_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = build_asset_free_phase2_fixture(Path(temp_dir))

            plan = inspect_ia32_decoded_store(
                fixture.xbe_path,
                fixture.decoded_block_store_path,
            )

        self.assertEqual(plan.coverage_map["block_count"], 3)
        self.assertEqual(plan.coverage_map["instruction_count"], 10)
        self.assertEqual(len(plan.direct_edge_relocations["edges"]), 1)
        self.assertEqual(
            plan.direct_edge_relocations["edges"][0]["relocation"],
            "preserved-fixed-va",
        )
        self.assertEqual(len(plan.indirect_target_table["sites"]), 1)
        self.assertFalse(plan.coverage_map["complete"])
        self.assertEqual(
            plan.indirect_target_table["sites"][0]["status"],
            "requires-verified-target-set",
        )

    def test_unknown_direct_target_fails_closed_before_artifact_build(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = build_asset_free_phase2_fixture(
                Path(temp_dir),
                code=bytes.fromhex("E87B000000C3"),
                stop_offset=5,
            )

            with self.assertRaisesRegex(
                Ia32BackendError,
                "direct-target-coverage-gap",
            ):
                build_ia32_decoded_store_artifact(
                    fixture.capsule,
                    xbe_path=fixture.xbe_path,
                    decoded_block_store_path=fixture.decoded_block_store_path,
                    stop_eip=fixture.stop_eip,
                    build_dir=Path(temp_dir) / "artifacts",
                )

    def test_xbe_digest_drift_is_rejected_before_byte_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = build_asset_free_phase2_fixture(Path(temp_dir))
            payload = bytearray(fixture.xbe_path.read_bytes())
            payload[0x1020] ^= 0xFF
            fixture.xbe_path.write_bytes(payload)

            with self.assertRaisesRegex(Ia32BackendError, "XBE source"):
                inspect_ia32_decoded_store(
                    fixture.xbe_path,
                    fixture.decoded_block_store_path,
                )

    @unittest.skipUnless(
        IA32_INTEGRATION_AVAILABLE,
        "the Phase-2 proof requires the supported Windows LLVM toolchain",
    )
    def test_asset_free_phase2_proof_set_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report = run_asset_free_phase2_proofs(Path(temp_dir))

        self.assertEqual(report["format"], PHASE2_PROOF_SET_FORMAT)
        self.assertTrue(report["passed"])
        proof = report["proofs"][0]
        self.assertTrue(proof["artifact_reproducible_across_mutable_store_metadata"])
        self.assertTrue(proof["resident_execution"]["same_process"])
        self.assertTrue(proof["resident_execution"]["reset_isolated"])

    @unittest.skipUnless(
        IA32_INTEGRATION_AVAILABLE,
        "the Phase-2 loader test requires the supported Windows LLVM toolchain",
    )
    def test_loader_rejects_xbe_store_and_sidecar_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fixture = build_asset_free_phase2_fixture(root / "fixture")
            artifact = build_ia32_decoded_store_artifact(
                fixture.capsule,
                xbe_path=fixture.xbe_path,
                decoded_block_store_path=fixture.decoded_block_store_path,
                stop_eip=fixture.stop_eip,
                build_dir=root / "artifacts",
            )

            other_xbe, _layout = build_synthetic_xbe(text_payload=bytes.fromhex("48C3"))
            other_xbe_path = root / "other.xbe"
            other_xbe_path.write_bytes(other_xbe)
            with self.assertRaisesRegex(Ia32BackendError, "no records for XBE"):
                validate_ia32_decoded_store_artifact_sources(
                    artifact,
                    xbe_path=other_xbe_path,
                    decoded_block_store_path=fixture.decoded_block_store_path,
                )

            connection = sqlite3.connect(fixture.decoded_block_store_path)
            try:
                row = connection.execute(
                    "SELECT cache_key, image_sha256, target, entry_bytes, "
                    "max_block_instructions, payload, payload_bytes "
                    "FROM decoded_blocks"
                ).fetchone()
                assert row is not None
                connection.execute(
                    """
                    INSERT INTO decoded_blocks(
                        cache_key, image_sha256, target, entry_bytes,
                        max_block_instructions, payload, payload_bytes,
                        last_used_ns, access_count
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, 0, 0)
                    """,
                    (row[0] + "-peer", *row[1:]),
                )
                connection.commit()
            finally:
                connection.close()
            with self.assertRaisesRegex(Ia32BackendError, "snapshot identity mismatch"):
                validate_ia32_decoded_store_artifact_sources(
                    artifact,
                    xbe_path=fixture.xbe_path,
                    decoded_block_store_path=fixture.decoded_block_store_path,
                )

            sidecar = artifact.root / "coverage-map.json"
            sidecar.write_text(json.dumps({"tampered": True}), encoding="utf-8")
            with self.assertRaisesRegex(Ia32BackendError, "output identity mismatch"):
                load_ia32_slice_artifact(artifact.root)


if __name__ == "__main__":
    unittest.main()
