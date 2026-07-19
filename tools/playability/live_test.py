#!/usr/bin/env python3
"""Run the native guest loop and live Vulkan presenter from one command."""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.host.first_frame_smoke import read_debug_events
from tools.playability.render_debug_report import (
    build_render_debug_report,
    write_render_debug_report,
)
DEFAULT_XBE = REPO_ROOT / "data" / "local" / "extracted" / "burnout_2_poi_usa" / "default.xbe"
DEFAULT_EXTRACTED_ROOT = DEFAULT_XBE.parent
DEFAULT_SAVE_DATA_ROOT = REPO_ROOT / "data" / "local" / "save-data"
DEFAULT_DASHBOARD_ROOT = REPO_ROOT / "data" / "local" / "dashboard-data"
DEFAULT_CACHE_ROOT = REPO_ROOT / "data" / "local" / "cache-data"
DEFAULT_BLOCK_CACHE = REPO_ROOT / "reports" / "local" / "playability" / "dynamic-block-cache.json"
DEFAULT_RENDER_STREAM = REPO_ROOT / "reports" / "local" / "live" / "render.json"
DEFAULT_CONTROLLER_STATE = REPO_ROOT / "reports" / "local" / "live" / "controller.json"
DEFAULT_PROBE_SUMMARY = REPO_ROOT / "reports" / "local" / "playability" / "native-live.json"
DEFAULT_RUNNER_LOG = REPO_ROOT / "reports" / "local" / "playability" / "native-live.log"
DEFAULT_RENDER_DEBUG_EVENTS = (
    REPO_ROOT / "reports" / "local" / "playability" / "render-debug-events.jsonl"
)
DEFAULT_RENDER_DEBUG_REPORT = (
    REPO_ROOT / "reports" / "local" / "playability" / "render-debug-report.json"
)
DEFAULT_SCENE_RECORD_AUDIT_REPORT = (
    REPO_ROOT / "reports" / "local" / "playability" / "scene-record-audit.json"
)
DEFAULT_AUDIT_ROOT = REPO_ROOT / "reports" / "local" / "flip-audit"
DEFAULT_NATIVE_SLICE_STEPS = 100_000


def build_guest_command(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        "-u",
        str(REPO_ROOT / "tools" / "playability" / "playability_probe.py"),
        str(args.xbe),
        "--extracted-root", str(args.extracted_root),
        "--save-data-root", str(args.save_data_root),
        "--dashboard-root", str(args.dashboard_root),
        "--cache-root", str(args.cache_root),
        "--dynamic-block-cache", str(args.dynamic_block_cache),
        "--native-guest-loop",
        "--native-slice-steps", str(args.native_slice_steps),
        "--max-steps", str(args.max_steps),
        "--live-render-stream", str(args.live_render_stream),
        "--live-controller-state", str(args.live_controller_state),
        "--json-output", str(args.json_output),
        "--quiet",
    ]
    if getattr(args, "lossless_flip_audit", False):
        command.extend(
            [
                "--live-flip-audit-ack",
                str(args.flip_audit_ack),
                "--live-flip-audit-health-interval",
                str(getattr(args, "flip_audit_health_interval", 30)),
                "--live-flip-audit-max-flips",
                str(getattr(args, "flip_audit_max_flips", 0)),
            ]
        )
    audit_world_matrix_address = getattr(args, "audit_world_matrix_address", None)
    if getattr(args, "audit_world_matrices", False) or audit_world_matrix_address is not None:
        command.append("--audit-world-matrices")
    if audit_world_matrix_address is not None:
        command.extend(
            ["--audit-world-matrix-address", f"0x{audit_world_matrix_address:08X}"]
        )
    if getattr(args, "audit_scene_records", False):
        command.extend(
            [
                "--audit-scene-records",
                "--scene-record-audit-output",
                str(args.scene_record_audit_output),
            ]
        )
    return command


def build_presenter_command(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        str(REPO_ROOT / "tools" / "host" / "first_frame_smoke.py"),
        "--max-frames", "0",
        "--timeout-seconds", "0",
        "--render-stream-json", str(args.live_render_stream),
        "--live-render-stream",
        "--controller-state-json", str(args.live_controller_state),
        "--no-inject-input",
        "--pretty",
    ]
    if args.skip_host_build:
        command.append("--skip-build")
    if getattr(args, "lossless_flip_audit", False):
        command.extend(
            [
                "--strict-render-validation",
                "--flip-audit-ack", str(args.flip_audit_ack),
                "--flip-audit-frame-directory", str(args.flip_audit_frames),
                "--debug-json", str(args.flip_audit_events),
                "--summary-output", str(args.flip_audit_presenter_summary),
                "--no-automatic-screenshot",
                "--flip-audit-health-interval",
                str(getattr(args, "flip_audit_health_interval", 30)),
            ]
        )
        if getattr(args, "flip_audit_max_flips", 0) > 0:
            command.extend(
                ["--flip-audit-max-flips", str(args.flip_audit_max_flips)]
            )
    else:
        command.extend(
            [
                "--debug-json",
                str(getattr(args, "render_debug_events", DEFAULT_RENDER_DEBUG_EVENTS)),
            ]
        )
    return command


def _read_json_lines(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def _longest_identical_frame_streak(frames: list[dict[str, object]]) -> int:
    longest = 0
    current = 0
    previous: object = None
    for frame in frames:
        digest = frame.get("pixel_fingerprint")
        current = current + 1 if digest == previous else 1
        longest = max(longest, current)
        previous = digest
    return longest


def build_lossless_flip_audit_report(
    *,
    ledger_path: Path,
    events_path: Path,
) -> dict[str, object]:
    ledger = _read_json_lines(ledger_path)
    events = read_debug_events(events_path)
    host_audits = [
        event for event in events if event.get("event") == "lossless_flip_audited"
    ]
    producer_indices = [int(record["audit_flip_index"]) for record in ledger]
    selected_producer_indices = [
        int(record["audit_flip_index"])
        for record in ledger
        if bool(record.get("audit_health_selected", True))
    ]
    host_indices = [int(event["flip_index"]) for event in host_audits]
    expected_indices = list(range(1, len(producer_indices) + 1))
    frame_reports: list[dict[str, object]] = []
    missing_readbacks: list[int] = []
    missing_issue_screenshots: list[int] = []
    for event in host_audits:
        flip_index = int(event["flip_index"])
        health_checked = bool(
            event.get("health_checked", event.get("readback_captured", False))
        )
        if health_checked and (
            not bool(event.get("readback_captured", False))
            or not event.get("pixel_fingerprint")
        ):
            missing_readbacks.append(flip_index)
        frame_path_text = str(event.get("frame_path", ""))
        frame_saved = bool(event.get("frame_saved", False))
        if frame_saved and (
            not frame_path_text or not Path(frame_path_text).is_file()
        ):
            missing_issue_screenshots.append(flip_index)
        pixel_count = int(event.get("pixel_count", 0))
        dominant_count = int(event.get("dominant_count", 0))
        bright_count = int(event.get("bright_count", 0))
        dark_count = int(event.get("dark_count", 0))
        failure_reasons = [
            name
            for name, flagged in (
                ("near_solid_frame", event.get("near_solid_frame", False)),
                ("whiteout_frame", event.get("whiteout_frame", False)),
                ("low_information_frame", event.get("low_information_frame", False)),
            )
            if bool(flagged)
        ]
        frame_reports.append(
            {
                "flip_index": flip_index,
                "guest_steps": int(event.get("guest_steps", 0)),
                "command_record_count": int(event.get("command_record_count", 0)),
                "health_checked": health_checked,
                "health_check_reason": event.get("health_check_reason"),
                "path": frame_path_text or None,
                "frame_saved": frame_saved,
                "pixel_fingerprint": event.get("pixel_fingerprint"),
                "pixel_count": pixel_count,
                "unique_colors": int(event.get("unique_colors", 0)),
                "dominant_fraction": round(dominant_count / pixel_count, 6)
                if pixel_count
                else 0.0,
                "bright_fraction": round(bright_count / pixel_count, 6)
                if pixel_count
                else 0.0,
                "dark_fraction": round(dark_count / pixel_count, 6)
                if pixel_count
                else 0.0,
                "failure_reasons": failure_reasons,
                "passed": not failure_reasons,
            }
        )
    validation_events = [
        event for event in events if event.get("event") == "render_validation"
    ]
    failed_validations = [
        event for event in validation_events if not bool(event.get("passed", False))
    ]
    failures: list[str] = []
    if not ledger:
        failures.append("no_acknowledged_guest_flips")
    if producer_indices != expected_indices:
        failures.append("producer_flip_sequence_gap")
    if host_indices != selected_producer_indices:
        failures.append("host_flip_sequence_mismatch")
    if missing_readbacks:
        failures.append("missing_frame_readback")
    if missing_issue_screenshots:
        failures.append("missing_issue_screenshot")
    if failed_validations:
        failures.append("strict_render_validation_failed")
    if len(validation_events) < len(selected_producer_indices):
        failures.append("missing_strict_render_validation_event")
    return {
        "format": "b2-recomp-lossless-flip-audit-report",
        "public_safe": False,
        "ledger_path": str(ledger_path),
        "events_path": str(events_path),
        "acknowledged_flip_count": len(ledger),
        "host_audited_flip_count": len(host_audits),
        "first_flip": producer_indices[0] if producer_indices else None,
        "last_flip": producer_indices[-1] if producer_indices else None,
        "producer_sequence_contiguous": producer_indices == expected_indices,
        "host_sequence_matches": host_indices == selected_producer_indices,
        "selected_producer_flip_count": len(selected_producer_indices),
        "strict_validation_event_count": len(validation_events),
        "failed_strict_validation_count": len(failed_validations),
        "missing_frame_flips": missing_readbacks,
        "missing_issue_screenshot_flips": missing_issue_screenshots,
        "saved_issue_screenshot_count": sum(
            bool(frame["frame_saved"]) for frame in frame_reports
        ),
        "health_checked_flip_count": len(selected_producer_indices),
        "health_skipped_flip_count": len(ledger) - len(selected_producer_indices),
        "distinct_pixel_frame_count": len(
            {
                frame["pixel_fingerprint"]
                for frame in frame_reports
                if frame["health_checked"] and frame["pixel_fingerprint"]
            }
        ),
        "longest_identical_frame_streak": _longest_identical_frame_streak(
            [frame for frame in frame_reports if frame["health_checked"]]
        ),
        "health_flagged_flip_count": sum(
            bool(frame["health_checked"]) and not bool(frame.get("passed", False))
            for frame in frame_reports
        ),
        "frames": frame_reports,
        "failure_reasons": failures,
        "passed": not failures,
    }


def _stop_process(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    if sys.platform == "win32":
        completed = subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode == 0:
            process.wait()
            return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _wait_for_guest_shutdown(
    process: subprocess.Popen[bytes],
    *,
    timeout_seconds: float = 30.0,
) -> tuple[int, bool]:
    try:
        return process.wait(timeout=timeout_seconds), False
    except subprocess.TimeoutExpired:
        print("Guest did not stop after the presenter closed; terminating it.")
        _stop_process(process)
        returncode = process.poll()
        return (returncode if returncode not in {None, 0} else 1), True


def _request_presenter_close(process: subprocess.Popen[bytes]) -> bool:
    if sys.platform != "win32" or process.poll() is not None:
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_ulong),
            ("cntUsage", ctypes.c_ulong),
            ("th32ProcessID", ctypes.c_ulong),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", ctypes.c_ulong),
            ("cntThreads", ctypes.c_ulong),
            ("th32ParentProcessID", ctypes.c_ulong),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_ulong),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.Process32FirstW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ProcessEntry32W),
    ]
    kernel32.Process32FirstW.restype = ctypes.c_bool
    kernel32.Process32NextW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ProcessEntry32W),
    ]
    kernel32.Process32NextW.restype = ctypes.c_bool
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool

    parent_process_ids: dict[int, int] = {}
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot != ctypes.c_void_p(-1).value:
        try:
            entry = ProcessEntry32W()
            entry.dwSize = ctypes.sizeof(ProcessEntry32W)
            if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
                while True:
                    parent_process_ids[int(entry.th32ProcessID)] = int(
                        entry.th32ParentProcessID
                    )
                    if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                        break
        finally:
            kernel32.CloseHandle(snapshot)
    presenter_process_ids = {process.pid}
    while True:
        descendants = {
            process_id
            for process_id, parent_id in parent_process_ids.items()
            if parent_id in presenter_process_ids
        }
        expanded = presenter_process_ids | descendants
        if expanded == presenter_process_ids:
            break
        presenter_process_ids = expanded

    window_handles: list[int] = []
    callback_type = ctypes.WINFUNCTYPE(
        ctypes.c_bool,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    user32.EnumWindows.argtypes = [callback_type, ctypes.c_void_p]
    user32.EnumWindows.restype = ctypes.c_bool
    user32.GetWindowThreadProcessId.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user32.IsWindowVisible.restype = ctypes.c_bool
    user32.PostMessageW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    user32.PostMessageW.restype = ctypes.c_bool

    @callback_type
    def find_window(hwnd: int, _parameter: int) -> bool:
        process_id = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if process_id.value in presenter_process_ids and user32.IsWindowVisible(hwnd):
            window_handles.append(int(hwnd or 0))
            return False
        return True

    user32.EnumWindows(find_window, 0)
    if not window_handles:
        return False
    return bool(user32.PostMessageW(window_handles[0], 0x0010, 0, 0))


def _finalize_lossless_flip_audit(args: argparse.Namespace, result: int) -> int:
    if not args.lossless_flip_audit:
        return result
    audit_report = build_lossless_flip_audit_report(
        ledger_path=args.flip_audit_ledger,
        events_path=args.flip_audit_events,
    )
    audit_report["process_returncode"] = result
    presenter_elapsed = getattr(args, "flip_audit_presenter_elapsed_seconds", None)
    total_elapsed = getattr(args, "flip_audit_total_elapsed_seconds", None)
    if presenter_elapsed is not None:
        audit_report["presenter_elapsed_seconds"] = round(
            float(presenter_elapsed), 6
        )
        audit_report["audited_flip_fps"] = round(
            int(audit_report["acknowledged_flip_count"])
            / max(float(presenter_elapsed), 1e-9),
            3,
        )
    if total_elapsed is not None:
        audit_report["total_elapsed_seconds"] = round(float(total_elapsed), 6)
    if result != 0:
        audit_report["failure_reasons"] = sorted(
            {*audit_report["failure_reasons"], "live_process_failed"}
        )
        audit_report["passed"] = False
    args.flip_audit_report.write_text(
        json.dumps(audit_report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        "Lossless flip audit: "
        f"{audit_report['acknowledged_flip_count']} flips, "
        f"{audit_report['distinct_pixel_frame_count']} distinct frames, "
        f"fps={audit_report.get('audited_flip_fps', 'n/a')}, "
        f"passed={str(audit_report['passed']).lower()}"
    )
    print(f"Audit report: {args.flip_audit_report}")
    return 1 if not audit_report["passed"] and result == 0 else result


def _finalize_render_diagnostics(args: argparse.Namespace) -> None:
    events_path = (
        args.flip_audit_events
        if getattr(args, "lossless_flip_audit", False)
        else args.render_debug_events
    )
    try:
        report = build_render_debug_report(
            probe_summary_path=args.json_output,
            presenter_events_path=events_path,
            xbe_path=args.xbe,
        )
        write_render_debug_report(report, args.render_debug_report)
        print(
            "Render diagnostics: "
            f"{report['status']}; report={args.render_debug_report}"
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"Could not finalize render diagnostics: {exc}")


def _summarize_scene_record_audit(args: argparse.Namespace) -> None:
    if not getattr(args, "audit_scene_records", False):
        return
    try:
        report = json.loads(args.scene_record_audit_output.read_text(encoding="utf-8"))
        print(
            "Scene-record audit: "
            f"{report.get('status', 'unknown')}; "
            f"draws={report.get('scene_draw_count', 0)}, "
            f"argument/RAM divergences="
            f"{report.get('argument_memory_divergence_count', 0)}, "
            f"RAM/source divergences="
            f"{report.get('runtime_source_divergence_count', 0)}; "
            f"report={args.scene_record_audit_output}"
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Could not summarize scene-record audit: {exc}")


def run_live_test(args: argparse.Namespace) -> int:
    run_started = time.monotonic()
    args.runner_log.parent.mkdir(parents=True, exist_ok=True)
    args.live_render_stream.parent.mkdir(parents=True, exist_ok=True)
    args.live_controller_state.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.render_debug_events.parent.mkdir(parents=True, exist_ok=True)
    args.render_debug_report.parent.mkdir(parents=True, exist_ok=True)
    if getattr(args, "audit_scene_records", False):
        args.scene_record_audit_output.parent.mkdir(parents=True, exist_ok=True)
    args.save_data_root.mkdir(parents=True, exist_ok=True)
    args.dashboard_root.mkdir(parents=True, exist_ok=True)
    args.cache_root.mkdir(parents=True, exist_ok=True)
    if args.lossless_flip_audit:
        args.flip_audit_output_dir.mkdir(parents=True, exist_ok=True)
        args.flip_audit_frames.mkdir(parents=True, exist_ok=True)
        for stale_frame in args.flip_audit_frames.glob("flip-*.bmp"):
            stale_frame.unlink()
        for stale in (
            args.flip_audit_ack,
            args.flip_audit_ledger,
            args.flip_audit_events,
            args.flip_audit_report,
            args.flip_audit_presenter_summary,
        ):
            if stale.exists():
                stale.unlink()
    if args.live_render_stream.exists():
        args.live_render_stream.unlink()
    if args.live_controller_state.exists():
        args.live_controller_state.unlink()
    if args.json_output.exists():
        args.json_output.unlink()
    for stale in (args.render_debug_events, args.render_debug_report):
        if stale.exists():
            stale.unlink()
    if (
        getattr(args, "audit_scene_records", False)
        and args.scene_record_audit_output.exists()
    ):
        args.scene_record_audit_output.unlink()
    controller_consumed = args.live_controller_state.with_name(
        args.live_controller_state.name + ".consumed.json"
    )
    if controller_consumed.exists():
        controller_consumed.unlink()

    guest: subprocess.Popen[bytes] | None = None
    presenter: subprocess.Popen[bytes] | None = None
    presenter_started: float | None = None
    with args.runner_log.open("wb") as runner_log:
        try:
            print("Starting native guest loop...")
            guest = subprocess.Popen(
                build_guest_command(args),
                cwd=REPO_ROOT,
                stdout=runner_log,
                stderr=subprocess.STDOUT,
            )
            deadline = time.monotonic() + args.startup_timeout_seconds
            while not args.live_render_stream.is_file():
                returncode = guest.poll()
                if returncode is not None:
                    print(f"Guest runner exited before publishing a frame (code {returncode}).")
                    print(f"See {args.runner_log}")
                    return returncode or 1
                if time.monotonic() >= deadline:
                    print(f"Timed out waiting for the first guest frame. See {args.runner_log}")
                    return 1
                time.sleep(0.05)

            print("Starting Vulkan presenter. Close the window or press Escape to stop.")
            presenter_started = time.monotonic()
            presenter = subprocess.Popen(
                build_presenter_command(args),
                cwd=REPO_ROOT,
                stdout=runner_log,
                stderr=subprocess.STDOUT,
            )
            scene_audit_run = getattr(args, "audit_scene_records", False)
            bounded_guest_run = args.max_steps > 0 or (
                args.lossless_flip_audit and args.flip_audit_max_flips > 0
            )
            if scene_audit_run:
                while guest.poll() is None and presenter.poll() is None:
                    time.sleep(0.05)
                guest_returncode = guest.poll()
                if guest_returncode is not None:
                    if presenter.poll() is None and not _request_presenter_close(presenter):
                        print("Could not request a graceful presenter close.")
                    try:
                        presenter_returncode = presenter.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        print("Presenter did not close after the scene-record audit.")
                        presenter_returncode = 1
                else:
                    presenter_returncode = presenter.returncode
                    args.live_controller_state.write_text(
                        json.dumps({"stop": True}, separators=(",", ":")) + "\n",
                        encoding="utf-8",
                    )
                    guest_returncode, forced_guest_stop = _wait_for_guest_shutdown(guest)
                    if forced_guest_stop:
                        _finalize_render_diagnostics(args)
                        args.flip_audit_total_elapsed_seconds = (
                            time.monotonic() - run_started
                        )
                        if presenter_started is not None:
                            args.flip_audit_presenter_elapsed_seconds = (
                                time.monotonic() - presenter_started
                            )
                        return _finalize_lossless_flip_audit(args, guest_returncode)
            elif bounded_guest_run:
                guest_returncode = guest.wait()
                if presenter.poll() is None and not _request_presenter_close(presenter):
                    print("Could not request a graceful presenter close.")
                try:
                    presenter_returncode = presenter.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    print("Presenter did not close after the bounded guest run.")
                    presenter_returncode = 1
            else:
                presenter_returncode = presenter.wait()
                args.live_controller_state.write_text(
                    json.dumps({"stop": True}, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )
                guest_returncode, forced_guest_stop = _wait_for_guest_shutdown(guest)
                if forced_guest_stop:
                    _finalize_render_diagnostics(args)
                    args.flip_audit_total_elapsed_seconds = (
                        time.monotonic() - run_started
                    )
                    if presenter_started is not None:
                        args.flip_audit_presenter_elapsed_seconds = (
                            time.monotonic() - presenter_started
                        )
                    return _finalize_lossless_flip_audit(args, guest_returncode)
            if guest_returncode != 0:
                print(f"Guest runner exited with code {guest_returncode}. See {args.runner_log}")
                _finalize_render_diagnostics(args)
                args.flip_audit_total_elapsed_seconds = time.monotonic() - run_started
                if presenter_started is not None:
                    args.flip_audit_presenter_elapsed_seconds = (
                        time.monotonic() - presenter_started
                    )
                return _finalize_lossless_flip_audit(args, guest_returncode)
            args.flip_audit_total_elapsed_seconds = time.monotonic() - run_started
            if presenter_started is not None:
                args.flip_audit_presenter_elapsed_seconds = (
                    time.monotonic() - presenter_started
                )
            _finalize_render_diagnostics(args)
            _summarize_scene_record_audit(args)
            return _finalize_lossless_flip_audit(args, presenter_returncode)
        except KeyboardInterrupt:
            print("Stopping live test...")
            return 130
        finally:
            _stop_process(presenter)
            _stop_process(guest)
            if args.live_controller_state.exists():
                args.live_controller_state.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the native guest and live Vulkan presenter together."
    )
    parser.add_argument("--xbe", type=Path, default=DEFAULT_XBE)
    parser.add_argument("--extracted-root", type=Path, default=DEFAULT_EXTRACTED_ROOT)
    parser.add_argument("--save-data-root", type=Path, default=DEFAULT_SAVE_DATA_ROOT)
    parser.add_argument("--dashboard-root", type=Path, default=DEFAULT_DASHBOARD_ROOT)
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument("--dynamic-block-cache", type=Path, default=DEFAULT_BLOCK_CACHE)
    parser.add_argument("--live-render-stream", type=Path, default=DEFAULT_RENDER_STREAM)
    parser.add_argument("--live-controller-state", type=Path, default=DEFAULT_CONTROLLER_STATE)
    parser.add_argument("--json-output", type=Path, default=DEFAULT_PROBE_SUMMARY)
    parser.add_argument("--runner-log", type=Path, default=DEFAULT_RUNNER_LOG)
    parser.add_argument(
        "--render-debug-events",
        type=Path,
        default=DEFAULT_RENDER_DEBUG_EVENTS,
        help="Presenter JSONL used for cross-layer geometry diagnostics.",
    )
    parser.add_argument(
        "--render-debug-report",
        type=Path,
        default=DEFAULT_RENDER_DEBUG_REPORT,
        help="Post-run guest/host geometry provenance report.",
    )
    parser.add_argument(
        "--native-slice-steps",
        type=int,
        default=DEFAULT_NATIVE_SLICE_STEPS,
        help=(
            "Guest instructions per input exchange; completed flips still "
            "yield immediately."
        ),
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=0,
        help="Maximum guest instructions; 0 (the default) runs until the presenter closes.",
    )
    parser.add_argument(
        "--audit-world-matrices",
        action="store_true",
        help=(
            "Capture singular RenderWare world-matrix inputs and their guest "
            "callers for the render debug report."
        ),
    )
    parser.add_argument(
        "--audit-world-matrix-address",
        type=lambda value: int(value, 0),
        help=(
            "Capture exact writes to one 64-byte RenderWare matrix and include "
            "the transitions in the render debug report."
        ),
    )
    parser.add_argument(
        "--audit-scene-records",
        action="store_true",
        help=(
            "Run the compact Lesson One scene-record/source audit and close "
            "automatically after its first complete table."
        ),
    )
    parser.add_argument(
        "--scene-record-audit-output",
        type=Path,
        default=DEFAULT_SCENE_RECORD_AUDIT_REPORT,
        help="Compact scene-record audit JSON written by the guest.",
    )
    parser.add_argument("--startup-timeout-seconds", type=float, default=180.0)
    parser.add_argument("--skip-host-build", action="store_true")
    parser.add_argument(
        "--lossless-flip-audit",
        action="store_true",
        help=(
            "Yield at every guest flip, strictly validate every frame, sample "
            "pixel health for candidates/intervals, and retain audit artifacts."
        ),
    )
    parser.add_argument(
        "--flip-audit-output-dir",
        type=Path,
        help="Audit run directory; defaults to a new timestamped directory.",
    )
    parser.add_argument(
        "--flip-audit-max-flips",
        type=int,
        default=0,
        help="Stop automatically after this many acknowledged flips; 0 is unlimited.",
    )
    parser.add_argument(
        "--flip-audit-health-interval",
        type=int,
        default=30,
        help=(
            "Read back/analyze every Nth flip plus candidate-triggered frames; "
            "1 restores exhaustive health checks and 0 uses candidates only."
        ),
    )
    args = parser.parse_args()
    if args.max_steps < 0:
        parser.error("--max-steps must not be negative")
    if args.native_slice_steps <= 0:
        parser.error("--native-slice-steps must be greater than zero")
    if args.startup_timeout_seconds <= 0:
        parser.error("--startup-timeout-seconds must be greater than zero")
    if args.flip_audit_max_flips < 0:
        parser.error("--flip-audit-max-flips must not be negative")
    if args.flip_audit_health_interval < 0:
        parser.error("--flip-audit-health-interval must not be negative")
    if args.audit_world_matrix_address is not None and not (
        0 <= args.audit_world_matrix_address <= 0xFFFFFFC0
    ):
        parser.error("--audit-world-matrix-address must fit a 64-byte 32-bit range")
    if args.flip_audit_max_flips and not args.lossless_flip_audit:
        parser.error("--flip-audit-max-flips requires --lossless-flip-audit")
    if args.lossless_flip_audit:
        if args.flip_audit_output_dir is None:
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
            args.flip_audit_output_dir = DEFAULT_AUDIT_ROOT / stamp
        elif not args.flip_audit_output_dir.is_absolute():
            args.flip_audit_output_dir = REPO_ROOT / args.flip_audit_output_dir
        args.flip_audit_ack = args.flip_audit_output_dir / "ack.bin"
        args.flip_audit_ledger = args.flip_audit_output_dir / "flips.jsonl"
        args.flip_audit_frames = args.flip_audit_output_dir / "frames"
        args.flip_audit_events = args.flip_audit_output_dir / "presenter-events.jsonl"
        args.flip_audit_report = args.flip_audit_output_dir / "report.json"
        args.flip_audit_presenter_summary = (
            args.flip_audit_output_dir / "presenter-summary.json"
        )
    return run_live_test(args)


if __name__ == "__main__":
    raise SystemExit(main())
