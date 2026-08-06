from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.playability.differential_replay import (
    ReplayObservation,
    bisect_first_divergence,
    compare_event_streams,
    diagnose_observation_divergence,
    run_differential_replay,
)
from tools.playability.replay_capsule import build_synthetic_capsule, load_replay_capsule
from tools.recomp.x86_lifter import CpuState, SparseMemory


class _FakeBackend:
    def __init__(self, name: str, *, diverges_at: int | None = None) -> None:
        self.name = name
        self.diverges_at = diverges_at
        self.prefixes: list[int] = []

    def run_prefix(self, prefix_steps: int) -> ReplayObservation:
        self.prefixes.append(prefix_steps)
        state = CpuState.with_registers(eax=prefix_steps, esp=0x8000)
        state.eip = 0x1000 + prefix_steps
        memory = SparseMemory({0x2000: prefix_steps & 0xFF})
        if self.diverges_at is not None and prefix_steps >= self.diverges_at:
            state.set_register("eax", prefix_steps + 1)
            memory.write(0x2001, b"\xcc")
        return ReplayObservation(
            backend=self.name,
            prefix_steps=prefix_steps,
            steps_executed=prefix_steps,
            exit_reason="fake",
            state=state,
            memory=memory,
            events={
                "service_sequence": [],
                "render_events": [],
                "audio_events": [],
            },
            native_symbol="b2r_guest_00001000_00001020",
        )


class DifferentialReplayTests(unittest.TestCase):
    def test_binary_search_finds_first_bad_transition(self) -> None:
        accepted = _FakeBackend("accepted")
        experimental = _FakeBackend("experimental", diverges_at=5)

        first_bad, left, right, comparisons = bisect_first_divergence(
            accepted, experimental, max_steps=32
        )

        self.assertEqual(first_bad, 5)
        self.assertNotEqual(left.architectural_hash(), right.architectural_hash())
        self.assertLessEqual(comparisons, 8)

    def test_failure_capsule_starts_at_last_matching_edge(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            capsule = load_replay_capsule(build_synthetic_capsule(root / "input.b2rcap"))
            failure = root / "failure.b2rcap"

            report = run_differential_replay(
                capsule,
                _FakeBackend("accepted"),
                _FakeBackend("experimental", diverges_at=3),
                max_steps=8,
                failure_capsule=failure,
            )
            replay = load_replay_capsule(failure)

            self.assertFalse(report["matches"])
            self.assertEqual(report["first_divergent_edge"], 3)
            self.assertEqual(replay.state.get_register("eax"), 2)
            self.assertEqual(replay.manifest["capture_kind"], "divergence")
            self.assertIn("diagnostics/divergence.json", replay.resources)
            self.assertTrue(report["divergence"]["changed_registers"])
            self.assertTrue(report["divergence"]["page_diffs"])

    def test_typed_packet_comparison_reports_exact_boundary(self) -> None:
        accepted = [{"kind": "draw", "mesh": 1}, {"kind": "flip", "frame": 4}]
        experimental = [
            {"kind": "draw", "mesh": 1},
            {"kind": "draw", "mesh": 2},
        ]

        report = compare_event_streams(accepted, experimental)

        self.assertFalse(report["matches"])
        self.assertEqual(report["first_mismatch"], 1)
        self.assertEqual(report["accepted_event"]["kind"], "flip")

    def test_compact_diagnostic_names_fpu_control_difference(self) -> None:
        accepted = _FakeBackend("accepted").run_prefix(1)
        experimental = _FakeBackend("experimental").run_prefix(1)
        experimental.state.fpu_control_word ^= 1

        report = diagnose_observation_divergence(accepted, experimental, compact=True)

        self.assertIn(
            "fpu_control_word",
            [change["register"] for change in report["changed_registers"]],
        )
        self.assertNotIn("page_hashes", report["accepted"])
        self.assertEqual(report["accepted"]["memory_page_count"], 1)


if __name__ == "__main__":
    unittest.main()
