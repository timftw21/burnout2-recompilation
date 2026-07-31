#!/usr/bin/env python3
"""Run the native guest loop and live Vulkan presenter from one command."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.host.first_frame_smoke import (
    DEFAULT_BUILD_MANIFEST,
    DEFAULT_DLL,
    DEFAULT_EXE,
    DEFAULT_FRAGMENT_SPV,
    DEFAULT_HOTKEY_SCREENSHOT_DIR,
    DEFAULT_METRICS_REPORT_DIR,
    DEFAULT_PIPELINE_CACHE,
    DEFAULT_SOURCE,
    DEFAULT_TEXTURE_CONVERT_SPV,
    DEFAULT_VERTEX_SPV,
    FirstFrameSmokeError,
    build_presenter_arguments,
    compile_first_frame,
    compile_presenter_library,
    compile_shaders,
    discover_toolchain,
    read_debug_events,
    run_embedded_presenter,
    validate_presenter_build_manifest,
    write_presenter_build_manifest,
)
from tools.playability.render_debug_report import (
    build_render_debug_report,
    write_render_debug_report,
)
from tools.playability.performance_debug_report import (
    build_performance_debug_report,
    write_hot_path_tables,
    write_performance_debug_report,
)
from tools.playability.live_transport import (
    LIVE_COMMAND_SIZE,
    LIVE_CONTROL_SCHEMA_VERSION,
    LIVE_RESOURCE_SIZE,
    LiveCommandTransport,
    LiveControlTransport,
    LiveResourceTransport,
    live_control_transport_name,
)
from tools.project_identity import (
    DEFAULT_SUPPORTED_TARGETS,
    ProjectIdentityError,
    RUN_MANIFEST_SCHEMA_VERSION,
    file_identity,
    generated_code_identity,
    git_identity,
    machine_identity,
    new_run_id,
    utc_now,
    verify_supported_xbe,
    write_json_atomic,
)
from tools.recomp.native_executor import (
    AOT_OPTIMIZATION_MODES,
    DEFAULT_AOT_OPTIMIZATION_MODE,
)
DEFAULT_XBE = REPO_ROOT / "data" / "local" / "extracted" / "burnout_2_poi_usa" / "default.xbe"
DEFAULT_EXTRACTED_ROOT = DEFAULT_XBE.parent
DEFAULT_SAVE_DATA_ROOT = REPO_ROOT / "data" / "local" / "save-data"
DEFAULT_DASHBOARD_ROOT = REPO_ROOT / "data" / "local" / "dashboard-data"
DEFAULT_CACHE_ROOT = REPO_ROOT / "data" / "local" / "cache-data"
DEFAULT_DECODED_BLOCK_STORE = (
    REPO_ROOT / "build" / "native-guest-loop" / "decoded-blocks.sqlite3"
)
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
DEFAULT_PERFORMANCE_DEBUG_REPORT = (
    REPO_ROOT / "reports" / "local" / "playability" / "performance-debug-report.json"
)
DEFAULT_COMMAND_WORK_CACHE_TRACE = (
    REPO_ROOT / "reports" / "local" / "playability" / "command-work-cache-trace.jsonl"
)
DEFAULT_RUN_MANIFEST = (
    REPO_ROOT / "reports" / "local" / "playability" / "run-manifest.json"
)
DEFAULT_SCENE_RECORD_AUDIT_REPORT = (
    REPO_ROOT / "reports" / "local" / "playability" / "scene-record-audit.json"
)
DEFAULT_AUDIT_ROOT = REPO_ROOT / "reports" / "local" / "flip-audit"
DEFAULT_NATIVE_SLICE_STEPS = 100_000
DEFAULT_STARTUP_TIMEOUT_SECONDS = 1800.0
DEFAULT_PRESENTATION_PIPELINE_DEPTH = 2


def _live_transport_name(args: argparse.Namespace) -> str | None:
    explicit = getattr(args, "live_transport_name", None)
    if explicit:
        return str(explicit)
    run_id = getattr(args, "run_id", None)
    return live_control_transport_name(str(run_id)) if run_id else None


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
        "--decoded-block-store", str(args.decoded_block_store),
        "--native-guest-loop",
        "--native-slice-steps", str(args.native_slice_steps),
        "--max-steps", str(args.max_steps),
        "--live-render-stream", str(args.live_render_stream),
        "--live-controller-state", str(args.live_controller_state),
        "--quiet",
        "--supported-targets",
        str(getattr(args, "supported_targets", DEFAULT_SUPPORTED_TARGETS)),
    ]
    run_id = getattr(args, "run_id", None)
    run_manifest = getattr(args, "run_manifest", None)
    if run_id:
        command.extend(["--run-id", str(run_id)])
    if run_manifest is not None:
        command.extend(["--run-manifest", str(run_manifest)])
    transport_name = _live_transport_name(args)
    if transport_name is not None:
        command.extend(["--live-control-transport", transport_name])
    if getattr(args, "allow_unsupported_xbe", False):
        command.append("--allow-unsupported-xbe")
    if not getattr(args, "no_diagnostics", False):
        command.extend(["--json-output", str(args.json_output)])
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
    if getattr(args, "profile_hot_paths", False):
        command.append("--profile-hot-paths")
        command.extend(
            [
                "--aot-ab-mode",
                str(
                    getattr(
                        args,
                        "aot_ab_mode",
                        DEFAULT_AOT_OPTIMIZATION_MODE,
                    )
                ),
            ]
        )
    if getattr(args, "developer_live_compile", False):
        command.append("--developer-live-compile")
    audit_world_matrix_address = getattr(args, "audit_world_matrix_address", None)
    if getattr(args, "audit_world_matrices", False) or audit_world_matrix_address is not None:
        command.append("--audit-world-matrices")
    if getattr(args, "audit_traffic_meshes", False):
        command.append("--audit-traffic-meshes")
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
    transport_name = _live_transport_name(args)
    if transport_name is not None:
        command.extend(["--live-control-transport", transport_name])
    if getattr(args, "no_diagnostics", False):
        command.append("--no-diagnostics")
    if args.skip_host_build:
        command.append("--skip-build")
    command.extend(
        [
            "--build-manifest",
            str(getattr(args, "presenter_build_manifest", DEFAULT_BUILD_MANIFEST)),
        ]
    )
    if getattr(args, "allow_stale_artifacts", False):
        command.append("--allow-stale-artifacts")
    if getattr(args, "cpu_vertex_programs", False):
        command.append("--cpu-vertex-programs")
    if getattr(args, "cpu_vertex_attributes", False):
        command.append("--cpu-vertex-attributes")
    if getattr(args, "cpu_texture_conversion", False):
        command.append("--cpu-texture-conversion")
    command_work_cache_trace = getattr(args, "command_work_cache_trace", None)
    if command_work_cache_trace is not None:
        command.extend(
            ["--command-work-cache-trace", str(command_work_cache_trace)]
        )
    requested_pipeline_depth = getattr(args, "presentation_pipeline_depth", None)
    presentation_pipeline_depth = (
        1
        if getattr(args, "lossless_flip_audit", False)
        and requested_pipeline_depth is None
        else requested_pipeline_depth or DEFAULT_PRESENTATION_PIPELINE_DEPTH
    )
    if presentation_pipeline_depth == 2:
        command.extend(["--presentation-pipeline-depth", "2"])
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
    elif not getattr(args, "no_diagnostics", False):
        command.extend(
            [
                "--debug-json",
                str(getattr(args, "render_debug_events", DEFAULT_RENDER_DEBUG_EVENTS)),
            ]
        )
    return command


def build_embedded_presenter_arguments(args: argparse.Namespace) -> list[str]:
    requested_pipeline_depth = getattr(args, "presentation_pipeline_depth", None)
    presentation_pipeline_depth = (
        requested_pipeline_depth or DEFAULT_PRESENTATION_PIPELINE_DEPTH
    )
    return build_presenter_arguments(
        program=DEFAULT_DLL,
        debug_json=(
            None
            if getattr(args, "no_diagnostics", False)
            else getattr(args, "render_debug_events", DEFAULT_RENDER_DEBUG_EVENTS)
        ),
        max_frames=0,
        inject_input=False,
        render_stream_json=args.live_render_stream,
        live_render_stream=True,
        controller_state_json=args.live_controller_state,
        live_control_transport=_live_transport_name(args),
        screenshot=None,
        hotkey_screenshot_directory=(
            None
            if getattr(args, "no_diagnostics", False)
            else DEFAULT_HOTKEY_SCREENSHOT_DIR
        ),
        metrics_report_directory=DEFAULT_METRICS_REPORT_DIR,
        command_work_cache_trace=getattr(args, "command_work_cache_trace", None),
        vertex_shader=DEFAULT_VERTEX_SPV,
        fragment_shader=DEFAULT_FRAGMENT_SPV,
        texture_convert_shader=DEFAULT_TEXTURE_CONVERT_SPV,
        pipeline_cache=DEFAULT_PIPELINE_CACHE,
        cpu_vertex_programs=getattr(args, "cpu_vertex_programs", False),
        cpu_vertex_attributes=getattr(args, "cpu_vertex_attributes", False),
        cpu_texture_conversion=getattr(args, "cpu_texture_conversion", False),
        presentation_pipeline_depth=presentation_pipeline_depth,
    )


def _uses_embedded_runtime(args: argparse.Namespace) -> bool:
    """Keep exact/bounded audit tools isolated; normal gameplay is one process."""
    return bool(
        os.name == "nt"
        and getattr(args, "max_steps", 0) == 0
        and not getattr(args, "lossless_flip_audit", False)
        and not getattr(args, "audit_scene_records", False)
    )


def _start_confirm_injector(
    args: argparse.Namespace,
    transport: LiveControlTransport,
    stop: threading.Event,
) -> threading.Thread | None:
    target_flip = getattr(args, "inject_confirm_after_flip", None)
    if target_flip is None:
        return None

    def inject_confirm() -> None:
        while not stop.wait(0.002):
            manifest = transport.read_manifest()
            if manifest is None:
                continue
            guest_flip = int(manifest.get("guest_flip_count", 0) or 0)
            if guest_flip < target_flip:
                continue
            transport.publish_controller(buttons=0x1000)
            print(
                "Injected diagnostic confirm input at guest flip "
                f"{guest_flip}."
            )
            stop.wait(0.15)
            transport.publish_controller(buttons=0)
            last_flip = guest_flip
            last_progress = time.monotonic()
            while not stop.wait(0.01):
                next_manifest = transport.read_manifest()
                next_flip = int(
                    (next_manifest or {}).get("guest_flip_count", last_flip)
                    or last_flip
                )
                if next_flip != last_flip:
                    last_flip = next_flip
                    last_progress = time.monotonic()
                    continue
                if time.monotonic() - last_progress >= 0.5:
                    state = transport.diagnostic_state()
                    print(
                        "Guest publication stalled after diagnostic confirm: "
                        + json.dumps(state, sort_keys=True)
                    )
                    return
            return

    thread = threading.Thread(
        target=inject_confirm,
        name="b2-diagnostic-confirm-injector",
        daemon=True,
    )
    thread.start()
    return thread


def _set_current_windows_thread_description(name: str) -> None:
    if os.name != "nt":
        return
    try:
        set_thread_description = ctypes.WinDLL(
            "kernel32", use_last_error=True
        ).SetThreadDescription
        set_thread_description.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
        set_thread_description.restype = ctypes.c_long
        current_thread = ctypes.WinDLL(
            "kernel32", use_last_error=True
        ).GetCurrentThread
        current_thread.argtypes = ()
        current_thread.restype = ctypes.c_void_p
        set_thread_description(current_thread(), name)
    except (AttributeError, OSError):
        return


class _InProcessGuest:
    def __init__(self, arguments: list[str]) -> None:
        self._arguments = arguments
        self._returncode: int | None = None
        self._error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="b2-guest-runtime",
            daemon=True,
        )

    def _run(self) -> None:
        _set_current_windows_thread_description("b2-guest-runtime")
        try:
            from tools.playability.playability_probe import main as guest_main

            self._returncode = int(guest_main(self._arguments))
        except BaseException as exc:
            self._error = exc
            self._returncode = 1

    def start(self) -> None:
        self._thread.start()

    def poll(self) -> int | None:
        return self._returncode if not self._thread.is_alive() else None

    def wait(self, timeout: float | None = None) -> int:
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise subprocess.TimeoutExpired("embedded guest", timeout)
        if self._error is not None:
            raise RuntimeError(
                "embedded guest failed: "
                f"{type(self._error).__name__}: {self._error}"
            ) from self._error
        return int(self._returncode or 0)


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


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("per_process_user_time_limit", ctypes.c_int64),
        ("per_job_user_time_limit", ctypes.c_int64),
        ("limit_flags", ctypes.c_uint32),
        ("minimum_working_set_size", ctypes.c_size_t),
        ("maximum_working_set_size", ctypes.c_size_t),
        ("active_process_limit", ctypes.c_uint32),
        ("affinity", ctypes.c_size_t),
        ("priority_class", ctypes.c_uint32),
        ("scheduling_class", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("read_operation_count", ctypes.c_uint64),
        ("write_operation_count", ctypes.c_uint64),
        ("other_operation_count", ctypes.c_uint64),
        ("read_transfer_count", ctypes.c_uint64),
        ("write_transfer_count", ctypes.c_uint64),
        ("other_transfer_count", ctypes.c_uint64),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("basic_limit_information", _JobObjectBasicLimitInformation),
        ("io_info", _IoCounters),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory_used", ctypes.c_size_t),
        ("peak_job_memory_used", ctypes.c_size_t),
    ]


class _WindowsKillOnCloseJob:
    """Own child processes even if the Python launcher exits abruptly."""

    _EXTENDED_LIMIT_INFORMATION_CLASS = 9
    _LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Windows process jobs are available only on Windows")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateJobObjectW.argtypes = (
            ctypes.c_void_p,
            ctypes.c_wchar_p,
        )
        self._kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        self._kernel32.SetInformationJobObject.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        )
        self._kernel32.SetInformationJobObject.restype = ctypes.c_bool
        self._kernel32.AssignProcessToJobObject.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        self._kernel32.AssignProcessToJobObject.restype = ctypes.c_bool
        self._kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        self._kernel32.CloseHandle.restype = ctypes.c_bool
        handle = self._kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._handle: int | None = int(handle)
        limits = _JobObjectExtendedLimitInformation()
        limits.basic_limit_information.limit_flags = self._LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
            ctypes.c_void_p(self._handle),
            self._EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def assign(self, process: subprocess.Popen[bytes]) -> None:
        if self._handle is None:
            raise RuntimeError("Windows process job is already closed")
        process_handle = getattr(process, "_handle", None)
        if process_handle is None:
            raise RuntimeError("child process does not expose a Windows handle")
        if not self._kernel32.AssignProcessToJobObject(
            ctypes.c_void_p(self._handle),
            ctypes.c_void_p(int(process_handle)),
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        handle = getattr(self, "_handle", None)
        self._handle = None
        if handle is not None:
            self._kernel32.CloseHandle(ctypes.c_void_p(handle))


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


def _finalize_performance_diagnostics(args: argparse.Namespace) -> None:
    try:
        report = build_performance_debug_report(
            args.json_output,
            render_debug_report_path=args.render_debug_report,
        )
        write_performance_debug_report(report, args.performance_debug_report)
        hot_path_tables = []
        if getattr(args, "profile_hot_paths", False):
            hot_path_tables = write_hot_path_tables(
                report,
                args.performance_debug_report.with_name("hot-path-tables"),
            )
        print(
            "Performance diagnostics: "
            f"{report['status']}; report={args.performance_debug_report}"
        )
        if hot_path_tables:
            print(f"Hot-path tables: {hot_path_tables[0].parent}")
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"Could not finalize performance diagnostics: {exc}")

def _finalize_diagnostics(args: argparse.Namespace) -> None:
    if getattr(args, "no_diagnostics", False):
        return
    try:
        summary = json.loads(args.json_output.read_text(encoding="utf-8"))
        actual_run_id = summary.get("identity", {}).get("run_id")
        if actual_run_id != getattr(args, "run_id", None):
            raise ProjectIdentityError(
                f"probe summary run_id is {actual_run_id!r}; "
                f"active run_id is {getattr(args, 'run_id', None)!r}"
            )
    except (OSError, TypeError, ValueError, json.JSONDecodeError, ProjectIdentityError) as exc:
        args.stale_artifact_rejected = True
        print(f"Rejected stale probe summary; post-run reports were not generated: {exc}")
        return
    _finalize_render_diagnostics(args)
    _finalize_performance_diagnostics(args)


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


def _startup_wait_status(
    cache_path: Path,
    elapsed_seconds: float,
    *,
    guest_active: bool = True,
    live_transport: LiveControlTransport | None = None,
    command_transport: LiveCommandTransport | None = None,
    resource_transport: LiveResourceTransport | None = None,
) -> str:
    cache_bytes = cache_path.stat().st_size if cache_path.is_file() else 0
    native_dll_count = sum(1 for _ in cache_path.parent.glob("native-loop-*.dll"))
    transport_status = ""
    if (
        live_transport is not None
        and command_transport is not None
        and resource_transport is not None
    ):
        control = live_transport.diagnostic_state()
        write_cursor, read_cursor = command_transport.cursors()
        slots = resource_transport.slot_metadata()
        transport_status = (
            f", manifest={control['manifest_sequence']}"
            f"/{control['manifest_size']}, commands={write_cursor}/{read_cursor}, "
            f"resources={slots[0][2]}/{slots[1][2]}, "
            f"scheduler={control['scheduler_phase']}:"
            f"0x{control['scheduler_worker_handle']:08X}/"
            f"0x{control['scheduler_eip']:08X}@"
            f"{control['scheduler_main_steps']}/"
            f"{control['scheduler_worker_steps']}, sync="
            f"{control['semaphore_count']}/"
            f"{control['worker_lifecycle_count']}:"
            f"0x{control['wait_handle']:08X}/"
            f"0x{control['current_worker_handle']:08X}, regs="
            f"eax:{control['scheduler_eax']:08X}/"
            f"ecx:{control['scheduler_ecx']:08X}/"
            f"edx:{control['scheduler_edx']:08X}/"
            f"ebx:{control['scheduler_ebx']:08X}/"
            f"esp:{control['scheduler_esp']:08X}/"
            f"ebp:{control['scheduler_ebp']:08X}/"
            f"esi:{control['scheduler_esi']:08X}/"
            f"edi:{control['scheduler_edi']:08X}"
        )
    state = (
        "Guest is still preparing"
        if guest_active
        else "Guest preparation ended without publishing a frame"
    )
    return (
        f"{state} after {elapsed_seconds:.0f}s "
        f"(decoded store {cache_bytes / (1024 * 1024):.1f} MiB, "
        f"native DLLs {native_dll_count}{transport_status})."
    )


def _request_current_process_window_close() -> bool:
    if sys.platform != "win32":
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(
        ctypes.c_bool,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    user32.EnumWindows.argtypes = [callback_type, ctypes.c_void_p]
    user32.GetWindowThreadProcessId.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user32.PostMessageW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    current_process_id = int(kernel32.GetCurrentProcessId())
    window_handles: list[int] = []

    @callback_type
    def find_window(hwnd: int, _parameter: int) -> bool:
        process_id = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if process_id.value == current_process_id and user32.IsWindowVisible(hwnd):
            window_handles.append(int(hwnd or 0))
        return True

    user32.EnumWindows(find_window, 0)
    return any(
        bool(user32.PostMessageW(hwnd, 0x0010, 0, 0))
        for hwnd in window_handles
    )


def _run_live_test_embedded(args: argparse.Namespace) -> int:
    """Run normal gameplay as one process with a guest thread and presenter DLL."""
    run_started = time.monotonic()
    diagnostics_enabled = not getattr(args, "no_diagnostics", False)
    for path in (
        args.live_render_stream,
        args.live_controller_state,
        *(
            (
                args.json_output,
                args.render_debug_events,
                args.render_debug_report,
                args.performance_debug_report,
            )
            if diagnostics_enabled
            else ()
        ),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
    for directory in (
        args.save_data_root,
        args.dashboard_root,
        args.cache_root,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    live_transport = LiveControlTransport.create(args.live_transport_name)
    live_transport.configure_hot_path_profile(
        getattr(args, "profile_hot_paths", False)
    )
    live_command_transport = LiveCommandTransport.create(args.live_transport_name)
    live_resource_transport = LiveResourceTransport.create(args.live_transport_name)
    guest = _InProcessGuest(build_guest_command(args)[3:])
    monitor_stop = threading.Event()
    input_injector: threading.Thread | None = None

    def monitor_guest() -> None:
        while not monitor_stop.wait(0.05):
            if guest.poll() is None:
                continue
            while not monitor_stop.wait(0.05):
                if _request_current_process_window_close():
                    return

    monitor = threading.Thread(
        target=monitor_guest,
        name="b2-presenter-stop-monitor",
        daemon=True,
    )
    try:
        print("Starting embedded native guest loop...")
        guest.start()
        deadline = time.monotonic() + args.startup_timeout_seconds
        next_startup_status = time.monotonic() + 30.0
        while not live_transport.manifest_available():
            returncode = guest.poll()
            if returncode is not None:
                try:
                    returncode = guest.wait()
                except RuntimeError as exc:
                    print(f"Guest runner exited before publishing a frame: {exc}")
                    return 1
                print(
                    _startup_wait_status(
                        args.decoded_block_store,
                        time.monotonic() - run_started,
                        guest_active=False,
                        live_transport=live_transport,
                        command_transport=live_command_transport,
                        resource_transport=live_resource_transport,
                    )
                )
                print(f"Embedded guest exit code: {returncode}.")
                return returncode or 1
            if time.monotonic() >= deadline:
                live_transport.request_stop()
                print("Timed out waiting for the first embedded guest frame.")
                return 1
            if time.monotonic() >= next_startup_status:
                print(
                    _startup_wait_status(
                        args.decoded_block_store,
                        time.monotonic() - run_started,
                        live_transport=live_transport,
                        command_transport=live_command_transport,
                        resource_transport=live_resource_transport,
                    )
                )
                next_startup_status = time.monotonic() + 30.0
            time.sleep(0.05)

        print("Starting embedded Vulkan presenter. Close the window or press Escape to stop.")
        monitor.start()
        input_injector = _start_confirm_injector(
            args, live_transport, monitor_stop
        )
        presenter_returncode = run_embedded_presenter(
            build_embedded_presenter_arguments(args),
            library=DEFAULT_DLL,
        )
        monitor_stop.set()
        live_transport.request_stop()
        try:
            guest_returncode = guest.wait(timeout=30.0)
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            print(f"Embedded guest did not stop cleanly: {exc}")
            guest_returncode = 1
        result = guest_returncode or presenter_returncode
        if guest_returncode != 0:
            print(f"Embedded guest exited with code {guest_returncode}.")
        _finalize_diagnostics(args)
        _summarize_scene_record_audit(args)
        return int(result)
    except KeyboardInterrupt:
        live_transport.request_stop()
        print("Stopping live test...")
        return 130
    finally:
        monitor_stop.set()
        if input_injector is not None:
            input_injector.join(timeout=1.0)
        live_transport.request_stop()
        live_resource_transport.close()
        live_command_transport.close()
        live_transport.close()
        if args.live_controller_state.exists():
            args.live_controller_state.unlink()


def _run_live_test_processes(args: argparse.Namespace) -> int:
    run_started = time.monotonic()
    diagnostics_enabled = not getattr(args, "no_diagnostics", False)
    if diagnostics_enabled:
        args.runner_log.parent.mkdir(parents=True, exist_ok=True)
    args.live_render_stream.parent.mkdir(parents=True, exist_ok=True)
    args.live_controller_state.parent.mkdir(parents=True, exist_ok=True)
    if diagnostics_enabled:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.render_debug_events.parent.mkdir(parents=True, exist_ok=True)
        args.render_debug_report.parent.mkdir(parents=True, exist_ok=True)
        args.performance_debug_report.parent.mkdir(parents=True, exist_ok=True)
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
    if diagnostics_enabled and args.json_output.exists():
        args.json_output.unlink()
    if diagnostics_enabled:
        for stale in (
            args.render_debug_events,
            args.render_debug_report,
            args.performance_debug_report,
        ):
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
    process_job: _WindowsKillOnCloseJob | None = None
    presenter_started: float | None = None
    live_transport = LiveControlTransport.create(args.live_transport_name)
    live_transport.configure_hot_path_profile(
        getattr(args, "profile_hot_paths", False)
    )
    live_command_transport = LiveCommandTransport.create(
        args.live_transport_name
    )
    live_resource_transport = LiveResourceTransport.create(
        args.live_transport_name
    )
    input_injector_stop = threading.Event()
    input_injector: threading.Thread | None = None
    runner_output = (
        args.runner_log.open("wb")
        if diagnostics_enabled
        else contextlib.nullcontext(subprocess.DEVNULL)
    )
    with runner_output as runner_log:
        try:
            print("Starting native guest loop...")
            if sys.platform == "win32":
                process_job = _WindowsKillOnCloseJob()
            guest = subprocess.Popen(
                build_guest_command(args),
                cwd=REPO_ROOT,
                stdout=runner_log,
                stderr=subprocess.STDOUT,
            )
            if process_job is not None:
                process_job.assign(guest)
            deadline = time.monotonic() + args.startup_timeout_seconds
            next_startup_status = time.monotonic() + 30.0
            while not (
                args.live_render_stream.is_file()
                if args.lossless_flip_audit
                else live_transport.manifest_available()
            ):
                returncode = guest.poll()
                if returncode is not None:
                    print(f"Guest runner exited before publishing a frame (code {returncode}).")
                    print(
                        _startup_wait_status(
                            args.decoded_block_store,
                            time.monotonic() - run_started,
                            guest_active=False,
                            live_transport=live_transport,
                            command_transport=live_command_transport,
                            resource_transport=live_resource_transport,
                        )
                    )
                    print(f"See {args.runner_log}")
                    return returncode or 1
                if time.monotonic() >= deadline:
                    print(f"Timed out waiting for the first guest frame. See {args.runner_log}")
                    return 1
                if time.monotonic() >= next_startup_status:
                    print(
                        _startup_wait_status(
                            args.decoded_block_store,
                            time.monotonic() - run_started,
                            live_transport=live_transport,
                            command_transport=live_command_transport,
                            resource_transport=live_resource_transport,
                        )
                    )
                    next_startup_status = time.monotonic() + 30.0
                time.sleep(0.05)

            print("Starting Vulkan presenter. Close the window or press Escape to stop.")
            presenter_started = time.monotonic()
            presenter = subprocess.Popen(
                build_presenter_command(args),
                cwd=REPO_ROOT,
                stdout=runner_log,
                stderr=subprocess.STDOUT,
            )
            if process_job is not None:
                process_job.assign(presenter)
            input_injector = _start_confirm_injector(
                args, live_transport, input_injector_stop
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
                    live_transport.request_stop()
                    guest_returncode, forced_guest_stop = _wait_for_guest_shutdown(guest)
                    if forced_guest_stop:
                        _finalize_diagnostics(args)
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
                live_transport.request_stop()
                guest_returncode, forced_guest_stop = _wait_for_guest_shutdown(guest)
                if forced_guest_stop:
                    _finalize_diagnostics(args)
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
                _finalize_diagnostics(args)
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
            _finalize_diagnostics(args)
            _summarize_scene_record_audit(args)
            return _finalize_lossless_flip_audit(args, presenter_returncode)
        except KeyboardInterrupt:
            print("Stopping live test...")
            return 130
        finally:
            input_injector_stop.set()
            if input_injector is not None:
                input_injector.join(timeout=1.0)
            _stop_process(presenter)
            _stop_process(guest)
            if process_job is not None:
                process_job.close()
            live_resource_transport.close()
            live_command_transport.close()
            live_transport.close()
            if args.live_controller_state.exists():
                args.live_controller_state.unlink()


def _native_cache_state(args: argparse.Namespace) -> dict[str, object]:
    native_build_dir = args.decoded_block_store.parent
    native_dlls = sorted(native_build_dir.glob("native-loop-*.dll"))
    pipeline_cache = REPO_ROOT / "build" / "local" / "first-frame" / "vulkan-pipeline-cache.bin"
    presenter = REPO_ROOT / "build" / "local" / "first-frame" / "b2_first_frame.exe"
    presenter_library = DEFAULT_DLL
    return {
        "decoded_blocks": file_identity(args.decoded_block_store),
        "native_module_manifest": file_identity(
            native_build_dir / "native-module-manifest.sqlite3"
        ),
        "native_module_count": len(native_dlls),
        "native_module_bytes": sum(path.stat().st_size for path in native_dlls),
        "presenter": file_identity(presenter),
        "presenter_library": file_identity(presenter_library),
        "vulkan_pipeline_cache": file_identity(pipeline_cache),
        "warm": bool(
            args.decoded_block_store.is_file()
            and native_dlls
            and presenter.is_file()
            and presenter_library.is_file()
            and pipeline_cache.is_file()
        ),
    }


def _live_run_manifest(
    args: argparse.Namespace,
    target_verification: dict[str, object],
) -> dict[str, object]:
    return {
        "format": "b2-recomp-live-run-manifest",
        "schema_version": RUN_MANIFEST_SCHEMA_VERSION,
        "public_safe": False,
        "run_id": args.run_id,
        "started_utc": utc_now(),
        "finished_utc": None,
        "status": "starting",
        "returncode": None,
        "elapsed_seconds": None,
        "repository": git_identity(REPO_ROOT),
        "target": target_verification,
        "generated_code": generated_code_identity(),
        "machine": machine_identity(),
        "cache": _native_cache_state(args),
        "presenter_build": getattr(args, "presenter_build_validation", None),
        "configuration": {
            "runtime_process_model": (
                "embedded_single_process"
                if _uses_embedded_runtime(args)
                else "isolated_diagnostic_processes"
            ),
            "diagnostics_enabled": not getattr(args, "no_diagnostics", False),
            "hot_path_profiling_enabled": getattr(args, "profile_hot_paths", False),
            "command_work_cache_trace": (
                str(args.command_work_cache_trace)
                if getattr(args, "command_work_cache_trace", None) is not None
                else None
            ),
            "aot_optimization_mode": getattr(
                args,
                "aot_ab_mode",
                DEFAULT_AOT_OPTIMIZATION_MODE,
            ),
            "developer_live_compilation_enabled": getattr(
                args, "developer_live_compile", False
            ),
            "live_control_transport": {
                "format": "b2-recomp-live-control",
                "schema_version": LIVE_CONTROL_SCHEMA_VERSION,
                "name": getattr(args, "live_transport_name", None),
                "command_mapping_bytes": LIVE_COMMAND_SIZE,
                "resource_mapping_bytes": LIVE_RESOURCE_SIZE,
            },
            "lossless_flip_audit": getattr(args, "lossless_flip_audit", False),
            "world_matrix_audit": getattr(args, "audit_world_matrices", False),
            "traffic_mesh_audit": getattr(args, "audit_traffic_meshes", False),
            "scene_record_audit": getattr(args, "audit_scene_records", False),
            "skip_host_build": bool(args.skip_host_build),
            "native_slice_steps": args.native_slice_steps,
            "max_steps": args.max_steps,
            "presentation_pipeline_depth": args.presentation_pipeline_depth,
            "cpu_vertex_programs": getattr(args, "cpu_vertex_programs", False),
            "cpu_vertex_attributes": getattr(args, "cpu_vertex_attributes", False),
            "cpu_texture_conversion": getattr(args, "cpu_texture_conversion", False),
            "unsupported_xbe_override": bool(
                getattr(args, "allow_unsupported_xbe", False)
            ),
            "stale_artifact_override": bool(
                getattr(args, "allow_stale_artifacts", False)
            ),
        },
        "outputs": {
            "probe_summary": (
                str(args.json_output.resolve())
                if not getattr(args, "no_diagnostics", False)
                else None
            ),
            "runner_log": (
                str(args.runner_log.resolve())
                if not getattr(args, "no_diagnostics", False)
                and not _uses_embedded_runtime(args)
                else None
            ),
        },
    }


def run_live_test(args: argparse.Namespace) -> int:
    started = time.monotonic()
    try:
        target_verification = verify_supported_xbe(
            args.xbe,
            manifest_path=getattr(args, "supported_targets", DEFAULT_SUPPORTED_TARGETS),
            allow_unsupported=getattr(args, "allow_unsupported_xbe", False),
        )
    except ProjectIdentityError as exc:
        print(f"Target identity rejected: {exc}", file=sys.stderr)
        return 2
    if target_verification["override_used"]:
        print(
            "WARNING: unsupported XBE override is active; this run is not valid "
            "for compatibility or performance claims.",
            file=sys.stderr,
        )
    args.run_id = getattr(args, "run_id", None) or new_run_id()
    args.live_transport_name = _live_transport_name(args)
    args.run_manifest = getattr(args, "run_manifest", None) or DEFAULT_RUN_MANIFEST
    args.presenter_build_manifest = (
        getattr(args, "presenter_build_manifest", None) or DEFAULT_BUILD_MANIFEST
    )
    embedded_runtime = _uses_embedded_runtime(args)
    if not args.skip_host_build and embedded_runtime:
        toolchain = discover_toolchain()
        compile_first_frame(toolchain=toolchain)
        compile_presenter_library(toolchain=toolchain)
        compile_shaders(toolchain=toolchain)
        write_presenter_build_manifest(
            path=args.presenter_build_manifest,
            source=DEFAULT_SOURCE,
            output=DEFAULT_EXE,
            library=DEFAULT_DLL,
            toolchain=toolchain,
            include_shaders=True,
        )
        args.presenter_build_validation = validate_presenter_build_manifest(
            args.presenter_build_manifest,
            executable=DEFAULT_EXE,
            library=DEFAULT_DLL,
            source=DEFAULT_SOURCE,
            require_shaders=True,
        )
    elif args.skip_host_build:
        try:
            args.presenter_build_validation = validate_presenter_build_manifest(
                args.presenter_build_manifest,
                executable=DEFAULT_EXE,
                library=DEFAULT_DLL,
                source=DEFAULT_SOURCE,
                require_shaders=True,
                allow_stale=getattr(args, "allow_stale_artifacts", False),
            )
        except FirstFrameSmokeError as exc:
            args.presenter_build_validation = {
                "status": "rejected",
                "valid": False,
                "override_used": False,
                "manifest": str(args.presenter_build_manifest.resolve()),
                "problems": [str(exc)],
            }
            manifest = _live_run_manifest(args, target_verification)
            manifest.update(
                {
                    "status": "rejected_stale_presenter",
                    "returncode": 2,
                    "finished_utc": utc_now(),
                    "elapsed_seconds": round(time.monotonic() - started, 6),
                }
            )
            write_json_atomic(args.run_manifest, manifest)
            print(f"Presenter identity rejected: {exc}", file=sys.stderr)
            print(f"Run manifest: {args.run_manifest}")
            return 2
        if args.presenter_build_validation["override_used"]:
            print(
                "WARNING: stale presenter artifact override is active; this run "
                "is not valid for compatibility or performance claims.",
                file=sys.stderr,
            )
    else:
        args.presenter_build_validation = {
            "status": "scheduled_rebuild",
            "valid": None,
            "override_used": False,
            "manifest": str(args.presenter_build_manifest.resolve()),
        }
    manifest = _live_run_manifest(args, target_verification)
    write_json_atomic(args.run_manifest, manifest)
    try:
        result = (
            _run_live_test_embedded(args)
            if embedded_runtime
            else _run_live_test_processes(args)
        )
    except Exception as exc:
        manifest.update(
            {
                "status": "failed_exception",
                "finished_utc": utc_now(),
                "elapsed_seconds": round(time.monotonic() - started, 6),
                "error": f"{type(exc).__name__}: {exc}",
                "cache_after": _native_cache_state(args),
            }
        )
        write_json_atomic(args.run_manifest, manifest)
        raise
    if getattr(args, "stale_artifact_rejected", False) and result == 0:
        result = 1
    manifest.update(
        {
            "status": "completed" if result == 0 else "failed",
            "returncode": result,
            "finished_utc": utc_now(),
            "elapsed_seconds": round(time.monotonic() - started, 6),
            "cache_after": _native_cache_state(args),
            "stale_artifact_rejected": bool(
                getattr(args, "stale_artifact_rejected", False)
            ),
        }
    )
    try:
        manifest["presenter_build_after"] = validate_presenter_build_manifest(
            args.presenter_build_manifest,
            executable=DEFAULT_EXE,
            library=DEFAULT_DLL,
            source=DEFAULT_SOURCE,
            require_shaders=True,
            allow_stale=True,
        )
    except FirstFrameSmokeError as exc:  # pragma: no cover - allow_stale avoids this
        manifest["presenter_build_after"] = {
            "status": "unavailable",
            "problems": [str(exc)],
        }
    write_json_atomic(args.run_manifest, manifest)
    print(f"Run manifest: {args.run_manifest}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the native guest and live Vulkan presenter together."
    )
    parser.add_argument("--xbe", type=Path, default=DEFAULT_XBE)
    parser.add_argument(
        "--supported-targets",
        type=Path,
        default=DEFAULT_SUPPORTED_TARGETS,
        help="Checked-in exact XBE identity manifest.",
    )
    parser.add_argument(
        "--allow-unsupported-xbe",
        action="store_true",
        help=(
            "Developer-only override for an unknown XBE; marks all output as "
            "unsupported and unsuitable for compatibility or performance claims."
        ),
    )
    parser.add_argument(
        "--run-manifest",
        type=Path,
        default=DEFAULT_RUN_MANIFEST,
        help="Self-identifying manifest written for every live run.",
    )
    parser.add_argument(
        "--presenter-build-manifest",
        type=Path,
        default=DEFAULT_BUILD_MANIFEST,
        help="Content-addressed presenter build identity manifest.",
    )
    parser.add_argument(
        "--allow-stale-artifacts",
        action="store_true",
        help=(
            "Developer-only override for stale presenter artifacts; marks the "
            "run as unsupported."
        ),
    )
    parser.add_argument("--extracted-root", type=Path, default=DEFAULT_EXTRACTED_ROOT)
    parser.add_argument("--save-data-root", type=Path, default=DEFAULT_SAVE_DATA_ROOT)
    parser.add_argument("--dashboard-root", type=Path, default=DEFAULT_DASHBOARD_ROOT)
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument(
        "--decoded-block-store",
        type=Path,
        default=DEFAULT_DECODED_BLOCK_STORE,
    )
    parser.add_argument(
        "--dynamic-block-cache",
        dest="decoded_block_store",
        type=Path,
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )
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
        "--performance-debug-report",
        type=Path,
        default=DEFAULT_PERFORMANCE_DEBUG_REPORT,
        help="Post-run guest throughput and hot-path report against the 60 FPS target.",
    )
    parser.add_argument(
        "--command-work-cache-trace",
        type=Path,
        help=(
            "Diagnostic-only presenter JSONL trace for offline command-work "
            "cache capacity simulation."
        ),
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
        "--inject-confirm-after-flip",
        type=int,
        help=(
            "Developer diagnostic: inject one A/confirm pulse after the named "
            "guest flip."
        ),
    )
    parser.add_argument(
        "--profile-hot-paths",
        action="store_true",
        help=(
            "Arm an F10-controlled native capture window for exact module "
            "edges and exclusive dispatcher timing; use only for profiling "
            "runs because active capture adds diagnostic accounting."
        ),
    )
    parser.add_argument(
        "--aot-ab-mode",
        choices=AOT_OPTIMIZATION_MODES,
        default=DEFAULT_AOT_OPTIMIZATION_MODE,
        help=(
            "Developer profiling split for preferred AOT fusion and "
            "registerized guest state; non-default modes require "
            "--profile-hot-paths."
        ),
    )
    parser.add_argument(
        "--developer-live-compile",
        action="store_true",
        help=(
            "Developer-only: compile and adopt newly discovered frontier blocks "
            "during play. Normal runs persist discoveries for the next AOT build."
        ),
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
        "--audit-traffic-meshes",
        action="store_true",
        help=(
            "Capture traffic/world mesh index submissions in the native "
            "observer without leaving the normal renderer path."
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
    parser.add_argument(
        "--startup-timeout-seconds",
        type=float,
        default=DEFAULT_STARTUP_TIMEOUT_SECONDS,
        help=(
            "Maximum wait for the first guest frame; the default allows a "
            "one-time decoded-store migration or native partition rebuild."
        ),
    )
    parser.add_argument("--skip-host-build", action="store_true")
    parser.add_argument(
        "--no-diagnostics",
        action="store_true",
        help=(
            "Disable guest summaries, presenter event logging, screenshots, runner "
            "logging, and post-run reports for a minimal-overhead manual run."
        ),
    )
    parser.add_argument(
        "--cpu-vertex-programs",
        action="store_true",
        help="Force the CPU vertex-program interpreter for presenter A/B runs.",
    )
    parser.add_argument(
        "--cpu-vertex-attributes",
        action="store_true",
        help="Force CPU indexed-attribute decoding while retaining GPU programs.",
    )
    parser.add_argument(
        "--cpu-texture-conversion",
        action="store_true",
        help="Force the CPU texture converter for presenter A/B runs.",
    )
    parser.add_argument(
        "--presentation-pipeline-depth",
        type=int,
        choices=(1, 2),
        default=None,
        help=(
            "Presenter handshake depth; normal live runs default to 2 so guest "
            "computation overlaps interpretation and Vulkan preparation. "
            "Lossless flip audits default to the required lock-step depth 1."
        ),
    )
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
    if args.presentation_pipeline_depth is None:
        args.presentation_pipeline_depth = (
            1
            if args.lossless_flip_audit
            else DEFAULT_PRESENTATION_PIPELINE_DEPTH
        )
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
    if (
        args.aot_ab_mode != DEFAULT_AOT_OPTIMIZATION_MODE
        and not args.profile_hot_paths
    ):
        parser.error("non-default --aot-ab-mode requires --profile-hot-paths")
    if args.lossless_flip_audit and args.presentation_pipeline_depth != 1:
        parser.error(
            "--presentation-pipeline-depth 2 cannot be combined with "
            "--lossless-flip-audit"
        )
    if args.audit_world_matrix_address is not None and not (
        0 <= args.audit_world_matrix_address <= 0xFFFFFFC0
    ):
        parser.error("--audit-world-matrix-address must fit a 64-byte 32-bit range")
    if args.flip_audit_max_flips and not args.lossless_flip_audit:
        parser.error("--flip-audit-max-flips requires --lossless-flip-audit")
    if args.no_diagnostics and (
        args.profile_hot_paths
        or args.command_work_cache_trace is not None
        or args.audit_world_matrices
        or args.audit_world_matrix_address is not None
        or args.audit_traffic_meshes
        or args.audit_scene_records
        or args.lossless_flip_audit
    ):
        parser.error("--no-diagnostics cannot be combined with profiling or audits")
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
