from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.playability import live_test
from tools.playability.live_test import (
    _finalize_lossless_flip_audit,
    _startup_wait_status,
    _wait_for_guest_shutdown,
    build_guest_command,
    build_lossless_flip_audit_report,
    build_presenter_command,
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
            native_slice_steps=20_000,
            max_steps=0,
            live_render_stream=Path("render.json"),
            live_controller_state=Path("controller.json"),
            json_output=Path("summary.json"),
            render_debug_events=Path("render-debug-events.jsonl"),
            render_debug_report=Path("render-debug-report.json"),
            skip_host_build=True,
            no_diagnostics=False,
            audit_world_matrices=False,
            audit_world_matrix_address=None,
            audit_scene_records=False,
            scene_record_audit_output=Path("scene-record-audit.json"),
            lossless_flip_audit=False,
            flip_audit_health_interval=30,
        )

    def test_guest_command_uses_unlimited_steps(self) -> None:
        command = build_guest_command(self._args())

        self.assertEqual(command[1], "-u")
        self.assertEqual(command[command.index("--max-steps") + 1], "0")
        self.assertIn("--native-guest-loop", command)
        self.assertIn("--quiet", command)
        self.assertEqual(command[command.index("--save-data-root") + 1], "save")
        self.assertEqual(command[command.index("--dashboard-root") + 1], "dashboard")
        self.assertEqual(command[command.index("--cache-root") + 1], "cache-data")
        self.assertEqual(
            command[command.index("--native-slice-steps") + 1],
            "20000",
        )

    def test_default_native_slice_cadence_targets_frame_rate(self) -> None:
        self.assertEqual(live_test.DEFAULT_NATIVE_SLICE_STEPS, 100_000)

    def test_startup_timeout_allows_one_time_cache_recovery(self) -> None:
        self.assertEqual(live_test.DEFAULT_STARTUP_TIMEOUT_SECONDS, 600.0)

    def test_startup_wait_status_reports_cache_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = Path(temp_dir) / "decoded-blocks.sqlite3"
            cache.write_bytes(bytes(1024 * 1024))
            (cache.parent / "native-loop-one.dll").touch()

            status = _startup_wait_status(cache, 30.0)

        self.assertIn("after 30s", status)
        self.assertIn("decoded store 1.0 MiB", status)
        self.assertIn("native DLLs 1", status)

    def test_hot_path_profile_is_opt_in_and_forwarded_to_guest(self) -> None:
        args = self._args()
        self.assertNotIn("--profile-hot-paths", build_guest_command(args))
        args.profile_hot_paths = True
        self.assertIn("--profile-hot-paths", build_guest_command(args))

    def test_world_matrix_audit_is_forwarded_to_guest(self) -> None:
        args = self._args()
        args.audit_world_matrices = True

        self.assertIn("--audit-world-matrices", build_guest_command(args))

    def test_world_matrix_write_address_is_forwarded_to_guest(self) -> None:
        args = self._args()
        args.audit_world_matrix_address = 0x20D74D00

        command = build_guest_command(args)

        self.assertIn("--audit-world-matrices", command)
        self.assertEqual(
            command[command.index("--audit-world-matrix-address") + 1],
            "0x20D74D00",
        )

    def test_scene_record_audit_uses_compact_dedicated_output(self) -> None:
        args = self._args()
        args.audit_scene_records = True

        command = build_guest_command(args)

        self.assertIn("--audit-scene-records", command)
        self.assertEqual(
            command[command.index("--scene-record-audit-output") + 1],
            "scene-record-audit.json",
        )

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

    def test_no_diagnostics_omits_guest_and_presenter_artifacts(self) -> None:
        args = self._args()
        args.no_diagnostics = True

        guest = build_guest_command(args)
        presenter = build_presenter_command(args)

        self.assertNotIn("--json-output", guest)
        self.assertIn("--quiet", guest)
        self.assertNotIn("--debug-json", presenter)
        self.assertIn("--no-diagnostics", presenter)

    def test_presenter_cpu_vertex_ab_flags_are_opt_in(self) -> None:
        args = self._args()
        self.assertNotIn("--cpu-vertex-programs", build_presenter_command(args))
        self.assertNotIn("--cpu-vertex-attributes", build_presenter_command(args))
        self.assertNotIn("--cpu-texture-conversion", build_presenter_command(args))

        args.cpu_vertex_programs = True
        args.cpu_vertex_attributes = True
        args.cpu_texture_conversion = True
        command = build_presenter_command(args)

        self.assertIn("--cpu-vertex-programs", command)
        self.assertIn("--cpu-vertex-attributes", command)
        self.assertIn("--cpu-texture-conversion", command)

    def test_presentation_pipeline_depth_two_is_default(self) -> None:
        args = self._args()
        command = build_presenter_command(args)

        self.assertEqual(
            command[command.index("--presentation-pipeline-depth") + 1],
            "2",
        )

        args.presentation_pipeline_depth = 1
        command = build_presenter_command(args)

        self.assertNotIn("--presentation-pipeline-depth", command)

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

    def test_guest_shutdown_timeout_forces_cleanup_and_reports_failure(self) -> None:
        process = Mock()
        process.wait.side_effect = live_test.subprocess.TimeoutExpired(
            cmd="guest",
            timeout=30.0,
        )
        process.poll.return_value = None

        with patch.object(live_test, "_stop_process") as stop_process:
            returncode, forced = _wait_for_guest_shutdown(process)

        self.assertTrue(forced)
        self.assertEqual(returncode, 1)
        stop_process.assert_called_once_with(process)

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
        self.assertNotIn("--presentation-pipeline-depth", presenter)
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
