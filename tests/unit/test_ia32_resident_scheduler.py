from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tools.recomp.ia32_native_backend import (
    PHASE5_RESIDENT_SCHEDULER_CONTRACT_ID,
    RESIDENT_SCHEDULER_CONTRACT_FORMAT,
    RESIDENT_SCHEDULER_EXCHANGE_VERSION,
    Ia32BackendError,
    _resident_scheduler_plan,
    build_ia32_resident_scheduler_artifact,
    execute_ia32_scheduler_reference,
    ia32_resident_scheduler_contract,
    ia32_resident_scheduler_contract_id,
    run_ia32_slice_artifact,
)
from tools.recomp.x86_lifter import lift_x86_function
from tools.recomp.ia32_proof_contract import (
    PHASE5_PROOF_SET_FORMAT,
    PHASE5_PROOF_SET_ID,
    SCHEDULER_LANE_TLS,
    build_asset_free_phase5_fixture,
    run_asset_free_phase5_proofs,
)


IA32_INTEGRATION_AVAILABLE = (
    sys.platform == "win32" and shutil.which("clang-cl") and shutil.which("lld-link")
)


class Ia32ResidentSchedulerTests(unittest.TestCase):
    def test_phase5_contract_freezes_lanes_tls_ordering_and_products(self) -> None:
        contract = ia32_resident_scheduler_contract()

        self.assertEqual(contract["format"], RESIDENT_SCHEDULER_CONTRACT_FORMAT)
        self.assertEqual(contract["exchange"]["version"], RESIDENT_SCHEDULER_EXCHANGE_VERSION)
        self.assertEqual(
            ia32_resident_scheduler_contract_id(),
            PHASE5_RESIDENT_SCHEDULER_CONTRACT_ID,
        )
        self.assertEqual(contract["lanes"], ["primary", "worker", "vblank"])
        self.assertEqual(
            contract["ordering"],
            "primary-safe-point-then-vblank-then-runnable-worker",
        )
        self.assertEqual(contract["normal_runtime_python_callbacks"], 0)

    def test_reference_scheduler_preserves_tls_wakeups_flips_and_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = build_asset_free_phase5_fixture(Path(temp_dir))
            result = execute_ia32_scheduler_reference(
                fixture.capsule,
                stop_eip=fixture.stop_eip,
            )

        self.assertEqual(
            tuple(record["lane"] for record in result.scheduler_trace),
            fixture.expected_lane_order,
        )
        self.assertEqual(result.worker_wakeups, 2)
        self.assertEqual(result.completed_flips, 1)
        self.assertEqual(
            [result.memory.read_u32(address) for address in SCHEDULER_LANE_TLS],
            [103, 202, 302],
        )
        self.assertNotEqual(result.render_hash, 0)
        self.assertNotEqual(result.audio_hash, 0)
        self.assertTrue(all(lane["state"] == "completed" for lane in result.lane_states))

    def test_scheduler_plan_rejects_a_stranded_waiting_lane(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = build_asset_free_phase5_fixture(Path(temp_dir))
        scheduler_state = dict(fixture.capsule.scheduler_state)
        resident = dict(scheduler_state["resident_scheduler"])
        resident["steps"] = list(resident["steps"][:-1])
        scheduler_state["resident_scheduler"] = resident
        malformed = replace(fixture.capsule, scheduler_state=scheduler_state)

        with self.assertRaisesRegex(Ia32BackendError, "stranded contexts"):
            _resident_scheduler_plan(malformed, stop_eip=fixture.stop_eip)

    def test_scheduler_plan_accepts_terminal_lane_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = build_asset_free_phase5_fixture(Path(temp_dir))
        scheduler_state = dict(fixture.capsule.scheduler_state)
        resident = dict(scheduler_state["resident_scheduler"])
        lanes = [dict(lane) for lane in resident["lanes"]]
        lanes[1]["initial_state"] = "completed"
        lanes[1].pop("wait_object", None)
        lanes[2]["initial_state"] = "completed"
        lanes[2].pop("wait_object", None)
        first_step = dict(resident["steps"][0])
        first_step.update(
            {
                "next_eip": fixture.stop_eip,
                "exit": "complete",
            }
        )
        first_step.pop("wake_lane", None)
        first_step.pop("wait_object", None)
        resident.update({"lanes": lanes, "steps": [first_step]})
        scheduler_state["resident_scheduler"] = resident
        captured = replace(fixture.capsule, scheduler_state=scheduler_state)

        plan = _resident_scheduler_plan(captured, stop_eip=fixture.stop_eip)
        reference = execute_ia32_scheduler_reference(
            captured,
            stop_eip=fixture.stop_eip,
        )

        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.scheduler_map["final_states"], [4, 4, 4])
        self.assertEqual(
            [lane["state"] for lane in reference.lane_states],
            ["completed", "completed", "completed"],
        )

    @unittest.skipUnless(
        IA32_INTEGRATION_AVAILABLE,
        "the IA-32 Phase-5 proof requires the supported Windows LLVM toolchain",
    )
    def test_scheduler_artifact_accepts_no_reached_host_services(self) -> None:
        entry_eip = 0x000D0000
        stop_eip = 0x000D0010
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fixture = build_asset_free_phase5_fixture(root / "fixture")
            state = fixture.capsule.state
            state.eip = entry_eip
            state.set_register("edx", 0x000080C0)
            functions = (
                lift_x86_function(
                    b"\x0F\x09\xEE\xE9\x08\x00\x00\x00",
                    base_address=entry_eip,
                    symbol="captured_primary_slice",
                ),
                lift_x86_function(
                    b"\x90\x90\x90\x90\x90",
                    base_address=stop_eip,
                    symbol="captured_primary_stop",
                ),
                lift_x86_function(
                    b"\x90",
                    base_address=0x00010000,
                    symbol="unrelated_low_decoded_block",
                ),
            )
            scheduler_state = dict(fixture.capsule.scheduler_state)
            resident = dict(scheduler_state["resident_scheduler"])
            lanes = [dict(lane) for lane in resident["lanes"]]
            lanes[0]["initial_eip"] = entry_eip
            lanes[1]["initial_state"] = "completed"
            lanes[1].pop("wait_object", None)
            lanes[2]["initial_state"] = "completed"
            lanes[2].pop("wait_object", None)
            resident.update(
                {
                    "lanes": lanes,
                    "steps": [
                        {
                            "sequence": 0,
                            "lane": "primary",
                            "entry_eip": entry_eip,
                            "next_eip": stop_eip,
                            "exit": "complete",
                        }
                    ],
                }
            )
            scheduler_state["resident_scheduler"] = resident
            capsule = replace(
                fixture.capsule,
                state=state,
                functions=functions,
                scheduler_state=scheduler_state,
                service_state={"service_registry": []},
            )

            artifact = build_ia32_resident_scheduler_artifact(
                capsule,
                stop_eip=stop_eip,
                build_dir=root / "build",
            )
            result = run_ia32_slice_artifact(artifact, capsule)
            architecture_map = artifact.root.joinpath(
                "architecture-map.json"
            ).read_text(encoding="utf-8")

        self.assertEqual(artifact.manifest["registered_service_count"], 0)
        self.assertIn("implemented-resident-observed-noop", architecture_map)
        self.assertGreaterEqual(
            min(record["address"] for record in artifact.manifest["code_spans"]),
            0x00050000,
        )
        self.assertEqual(
            [lane["state"] for lane in result.resident_scheduler["lanes"]],
            ["completed", "completed", "completed"],
        )

    @unittest.skipUnless(
        IA32_INTEGRATION_AVAILABLE,
        "the IA-32 Phase-5 proof requires the supported Windows LLVM toolchain",
    )
    def test_asset_free_phase5_proof_matches_resident_scheduler_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report = run_asset_free_phase5_proofs(Path(temp_dir))

        self.assertEqual(report["format"], PHASE5_PROOF_SET_FORMAT)
        self.assertEqual(
            report["resident_scheduler_contract_id"],
            ia32_resident_scheduler_contract_id(),
        )
        self.assertEqual(report["proof_set_id"], PHASE5_PROOF_SET_ID)
        self.assertTrue(report["passed"])
        checks = report["proofs"][0]["checks"]
        self.assertTrue(checks["scheduler_trace_match"])
        self.assertTrue(checks["service_trace_match"])
        self.assertTrue(checks["lane_contexts_match"])
        self.assertTrue(checks["no_stranded_context"])
        self.assertTrue(checks["no_unclassified_exit"])
        fault_checks = report["proofs"][1]["checks"]
        self.assertTrue(fault_checks["fault_contained_in_vblank"])
        self.assertTrue(fault_checks["other_lanes_completed"])
        self.assertTrue(fault_checks["schedule_continued_after_fault"])


if __name__ == "__main__":
    unittest.main()
