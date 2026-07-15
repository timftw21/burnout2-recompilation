from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.playability import live_test
from tools.playability.live_test import (
    build_guest_command,
    build_lossless_flip_audit_report,
    build_presenter_command,
    _finalize_lossless_flip_audit,
)

class LiveTestTests(unittest.TestCase):
    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(
            xbe=Path("game/default.xbe"),
            extracted_root=Path("game"),
            save_data_root=Path("save"),
            dashboard_root=Path("dashboard"),
            cache_root=Path("cache-data"),
            dynamic_block_cache=Path("cache.json"),
            native_slice_steps=2500,
            max_steps=0,
            live_render_stream=Path("render.json"),
            live_controller_state=Path("controller.json"),
            json_output=Path("summary.json"),
            render_debug_events=Path("render-debug-events.jsonl"),
            render_debug_report=Path("render-debug-report.json"),
            skip_host_build=True,
            lossless_flip_audit=False,
            flip_audit_health_interval=30,
        )

    def test_guest_command_uses_unlimited_steps(self) -> None:
        command = build_guest_command(self._args())

        self.assertEqual(command[command.index("--max-steps") + 1], "0")
        self.assertIn("--native-guest-loop", command)
        self.assertEqual(command[command.index("--save-data-root") + 1], "save")
        self.assertEqual(command[command.index("--dashboard-root") + 1], "dashboard")
        self.assertEqual(command[command.index("--cache-root") + 1], "cache-data")

    def test_presenter_command_runs_until_window_closes(self) -> None:
        command = build_presenter_command(self._args())

        self.assertEqual(command[command.index("--max-frames") + 1], "0")
        self.assertEqual(command[command.index("--timeout-seconds") + 1], "0")
        self.assertIn("--live-render-stream", command)
        self.assertIn("--skip-build", command)
        self.assertEqual(
            command[command.index("--debug-json") + 1],
            "render-debug-events.jsonl",
        )

    def test_windows_forced_cleanup_terminates_the_presenter_process_tree(self) -> None:
        process = Mock()
        process.pid = 1234
        process.poll.return_value = None
        completed = Mock(returncode=0)
        with (
            patch.object(live_test.sys, "platform", "win32"),
            patch.object(live_test.subprocess, "run", return_value=completed) as run,
        ):
            live_test._stop_process(process)

        run.assert_called_once_with(
            ["taskkill", "/PID", "1234", "/T", "/F"],
            stdout=live_test.subprocess.DEVNULL,
            stderr=live_test.subprocess.DEVNULL,
            check=False,
        )
        process.wait.assert_called_once_with()
        process.terminate.assert_not_called()

    def test_lossless_mode_wires_guest_and_presenter_handshake(self) -> None:
        args = self._args()
        args.lossless_flip_audit = True
        args.flip_audit_ack = Path("audit/ack.bin")
        args.flip_audit_frames = Path("audit/frames")
        args.flip_audit_events = Path("audit/events.jsonl")
        args.flip_audit_presenter_summary = Path("audit/presenter.json")
        args.flip_audit_output_dir = Path("audit")

        guest = build_guest_command(args)
        presenter = build_presenter_command(args)

        self.assertEqual(
            guest[guest.index("--live-flip-audit-ack") + 1], "audit\\ack.bin"
        )
        self.assertIn("--strict-render-validation", presenter)
        self.assertEqual(
            presenter[presenter.index("--flip-audit-frame-directory") + 1],
            "audit\\frames",
        )
        self.assertIn("--no-automatic-screenshot", presenter)
        self.assertEqual(
            presenter[presenter.index("--flip-audit-health-interval") + 1],
            "30",
        )
        self.assertNotIn("--screenshot-output", presenter)

    def test_audit_report_allows_unsampled_frames_without_readbacks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger = root / "flips.jsonl"
            events = root / "events.jsonl"
            ledger.write_text(
                "\n".join(
                    json.dumps(
                        {
                            "audit_flip_index": flip,
                            "audit_health_selected": flip == 1,
                        }
                    )
                    for flip in (1, 2)
                )
                + "\n",
                encoding="utf-8",
            )
            events.write_text(
                "\n".join(
                    json.dumps(event)
                    for event in (
                        {"event": "render_validation", "passed": True},
                        {
                            "event": "lossless_flip_audited",
                            "flip_index": 1,
                            "health_checked": True,
                            "health_check_reason": "first_flip",
                            "readback_captured": True,
                            "pixel_fingerprint": "sample-1",
                            "pixel_count": 16,
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )

            report = build_lossless_flip_audit_report(
                ledger_path=ledger,
                events_path=events,
            )

            self.assertTrue(report["passed"])
            self.assertEqual(report["health_checked_flip_count"], 1)
            self.assertEqual(report["health_skipped_flip_count"], 1)
            self.assertEqual(report["missing_frame_flips"], [])

    def test_audit_report_proves_contiguous_acknowledged_frame_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger = root / "flips.jsonl"
            events = root / "events.jsonl"
            event_records: list[dict[str, object]] = []
            ledger_records: list[dict[str, object]] = []
            for flip in (1, 2):
                ledger_records.append({"audit_flip_index": flip})
                event_records.extend(
                    [
                        {"event": "render_validation", "passed": True},
                        {
                            "event": "lossless_flip_audited",
                            "flip_index": flip,
                            "guest_steps": flip * 100,
                            "command_record_count": flip * 10,
                            "readback_captured": True,
                            "pixel_fingerprint": f"frame-{flip}",
                            "pixel_count": 16,
                            "unique_colors": 16 + flip,
                            "dominant_count": 1,
                            "bright_count": 0,
                            "dark_count": 0,
                            "near_solid_frame": False,
                            "whiteout_frame": False,
                            "low_information_frame": False,
                            "visual_issue": False,
                            "frame_saved": False,
                            "frame_path": "",
                        },
                    ]
                )
            ledger.write_text(
                "".join(json.dumps(item) + "\n" for item in ledger_records),
                encoding="utf-8",
            )
            events.write_text(
                "".join(json.dumps(item) + "\n" for item in event_records),
                encoding="utf-8",
            )

            report = build_lossless_flip_audit_report(
                ledger_path=ledger,
                events_path=events,
            )

        self.assertTrue(report["passed"])
        self.assertEqual(report["acknowledged_flip_count"], 2)
        self.assertEqual(report["distinct_pixel_frame_count"], 2)
        self.assertEqual(report["saved_issue_screenshot_count"], 0)

    def test_audit_finalizer_marks_nonzero_process_exit_as_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger = root / "flips.jsonl"
            events = root / "events.jsonl"
            report_path = root / "report.json"
            ledger.write_text("", encoding="utf-8")
            events.write_text("", encoding="utf-8")
            args = argparse.Namespace(
                lossless_flip_audit=True,
                flip_audit_ledger=ledger,
                flip_audit_events=events,
                flip_audit_report=report_path,
            )

            result = _finalize_lossless_flip_audit(args, 7)
            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(result, 7)
        self.assertFalse(report["passed"])
        self.assertEqual(report["process_returncode"], 7)
        self.assertIn("live_process_failed", report["failure_reasons"])


if __name__ == "__main__":
    unittest.main()
