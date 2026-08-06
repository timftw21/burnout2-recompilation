from __future__ import annotations

import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path

from tools.playability.replay_capsule import (
    capture_replay_capsule,
    load_replay_capsule,
)
from tools.recomp.ia32_native_backend import (
    PHASE1_PERSISTENT_WORKER_CONTRACT_ID,
    WORKER_COMMAND_ENTER,
    WORKER_EXCHANGE_VERSION,
    WORKER_PROTOCOL_VERSION,
    WORKER_STATUS_PROTOCOL_ERROR,
    Ia32BackendError,
    Ia32PersistentWorker,
    _exchange_payload,
    _set_windows_event,
    _wait_worker_signal,
    build_ia32_persistent_slice_artifact,
    ia32_persistent_worker_contract,
    ia32_persistent_worker_contract_id,
    validate_ia32_artifact_manifest,
)
from tools.recomp.ia32_proof_contract import (
    PHASE1_PROOF_SET_FORMAT,
    build_asset_free_phase0_capsules,
    run_asset_free_phase1_proofs,
)
from tools.recomp.x86_lifter import CpuState, SparseMemory, lift_x86_function


IA32_INTEGRATION_AVAILABLE = (
    sys.platform == "win32" and shutil.which("clang-cl") and shutil.which("lld-link")
)


class Ia32PersistentWorkerTests(unittest.TestCase):
    def test_phase1_contract_freezes_worker_lifecycle_and_transport(self) -> None:
        contract = ia32_persistent_worker_contract()

        self.assertEqual(
            ia32_persistent_worker_contract_id(),
            PHASE1_PERSISTENT_WORKER_CONTRACT_ID,
        )
        self.assertEqual(contract["exchange"]["version"], WORKER_EXCHANGE_VERSION)
        self.assertEqual(contract["exchange"]["protocol_version"], WORKER_PROTOCOL_VERSION)
        self.assertEqual(contract["normal_runtime_python_callbacks"], 0)
        self.assertIn("process-creation", contract["steady_state_excludes"])
        self.assertIn("PE-mapping", contract["steady_state_excludes"])

    @unittest.skipUnless(
        IA32_INTEGRATION_AVAILABLE,
        "the IA-32 worker proof requires the supported Windows LLVM toolchain",
    )
    def test_asset_free_phase1_proof_set_reuses_one_process_without_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report = run_asset_free_phase1_proofs(Path(temp_dir))

        self.assertEqual(report["format"], PHASE1_PROOF_SET_FORMAT)
        self.assertTrue(report["passed"])
        self.assertEqual(len(report["proofs"]), 2)
        for proof in report["proofs"]:
            self.assertTrue(proof["same_process"])
            self.assertTrue(proof["reset_isolated"])
            self.assertTrue(proof["artifact_reproducible"])
            self.assertEqual(proof["resident_process_count"], 1)
            self.assertEqual(
                [dispatch["index"] for dispatch in proof["dispatches"]],
                [1, 2, 3],
            )
            self.assertTrue(
                all(dispatch["architectural_match"] for dispatch in proof["dispatches"])
            )

    @unittest.skipUnless(
        IA32_INTEGRATION_AVAILABLE,
        "the IA-32 worker fault test requires the supported Windows LLVM toolchain",
    )
    def test_fault_report_names_artifact_and_current_guest_eip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            function = lift_x86_function(
                bytes.fromhex("8B01C3"),
                base_address=0x000C0000,
                symbol="resident_fault",
            )
            state = CpuState.with_registers(ecx=0x000D0000, esp=0x000E0800)
            state.eip = function.base_address
            capsule = load_replay_capsule(
                capture_replay_capsule(
                    root / "fault.b2rcap",
                    state=state,
                    memory=SparseMemory({0x000D0000: 42, 0x000E0000: 0}),
                    functions=(function,),
                    scheduler_state={},
                    service_state={},
                    provenance={"synthetic": True},
                    capture_kind="synthetic",
                )
            )
            artifact = build_ia32_persistent_slice_artifact(
                capsule,
                stop_eip=0x000C0002,
                build_dir=root / "artifacts",
            )
            with Ia32PersistentWorker.start(artifact, capsule) as worker:
                capsule.state.set_register("ecx", 0x30000000)
                with self.assertRaises(Ia32BackendError) as raised:
                    worker.dispatch(capsule)

        message = str(raised.exception)
        self.assertIn("crashed with an access violation", message)
        self.assertIn("last published guest EIP 0x000C0000", message)
        self.assertIn(artifact.artifact_id, message)

    @unittest.skipUnless(
        IA32_INTEGRATION_AVAILABLE,
        "the IA-32 worker protocol test requires the supported Windows LLVM toolchain",
    )
    def test_protocol_mismatch_reports_current_guest_eip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = build_asset_free_phase0_capsules(Path(temp_dir) / "capsules")[0]
            artifact = build_ia32_persistent_slice_artifact(
                fixture.capsule,
                stop_eip=fixture.stop_eip,
                build_dir=Path(temp_dir) / "artifacts",
            )
            with Ia32PersistentWorker.start(artifact, fixture.capsule) as worker:
                payload = _exchange_payload(
                    fixture.capsule,
                    artifact,
                    exchange_version=WORKER_EXCHANGE_VERSION,
                    protocol_version=WORKER_PROTOCOL_VERSION + 1,
                    worker_command=WORKER_COMMAND_ENTER,
                )
                worker._view.seek(0)
                worker._view.write(payload)
                worker._view.seek(0)
                _set_windows_event(worker._request_event)
                outcome = _wait_worker_signal(
                    worker._response_event,
                    worker._process,
                    worker.timeout_seconds,
                )
                output = worker._read_exchange()

        _magic, _version, status, error, _pages, detail = struct.unpack_from("<6I", output)
        self.assertEqual(outcome, "response")
        self.assertEqual(status, WORKER_STATUS_PROTOCOL_ERROR)
        self.assertEqual(error, 24)
        self.assertEqual(detail, fixture.capsule.state.eip)

    @unittest.skipUnless(
        IA32_INTEGRATION_AVAILABLE,
        "the IA-32 worker timeout test requires the supported Windows LLVM toolchain",
    )
    def test_idle_worker_wait_has_bounded_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = build_asset_free_phase0_capsules(Path(temp_dir) / "capsules")[0]
            artifact = build_ia32_persistent_slice_artifact(
                fixture.capsule,
                stop_eip=fixture.stop_eip,
                build_dir=Path(temp_dir) / "artifacts",
            )
            with Ia32PersistentWorker.start(artifact, fixture.capsule) as worker:
                outcome = _wait_worker_signal(
                    worker._response_event,
                    worker._process,
                    0.001,
                )

        self.assertEqual(outcome, "timeout")

    @unittest.skipUnless(
        IA32_INTEGRATION_AVAILABLE,
        "the IA-32 worker manifest test requires the supported Windows LLVM toolchain",
    )
    def test_manifest_rejects_persistent_contract_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = build_asset_free_phase0_capsules(Path(temp_dir) / "capsules")[0]
            artifact = build_ia32_persistent_slice_artifact(
                fixture.capsule,
                stop_eip=fixture.stop_eip,
                build_dir=Path(temp_dir) / "artifacts",
            )
            tampered = dict(artifact.manifest)
            tampered["persistent_worker_contract_id"] = "0" * 64

            with self.assertRaisesRegex(Ia32BackendError, "resident-worker contract"):
                validate_ia32_artifact_manifest(tampered)


if __name__ == "__main__":
    unittest.main()
