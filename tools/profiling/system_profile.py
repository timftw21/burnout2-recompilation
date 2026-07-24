#!/usr/bin/env python3
"""Capture self-identifying ETW or RenderDoc profiles of normal gameplay."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import cast


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.project_identity import git_identity, sha256_file, utc_now


PROFILE_ROOT = REPO_ROOT / "reports" / "local" / "profiling"
LIVE_TEST = REPO_ROOT / "tools" / "playability" / "live_test.py"
LIVE_RUN_MANIFEST = REPO_ROOT / "reports" / "local" / "playability" / "run-manifest.json"
WPT_ROOT = Path("C:/Program Files (x86)/Windows Kits/10/Windows Performance Toolkit")
DENIED_LIVE_ARGUMENTS = frozenset(
    {
        "--allow-stale-artifacts",
        "--allow-unsupported-xbe",
        "--audit-scene-records",
        "--audit-world-matrices",
        "--audit-world-matrix-address",
        "--developer-live-compile",
        "--lossless-flip-audit",
        "--max-steps",
        "--profile-hot-paths",
    }
)


class SystemProfileError(RuntimeError):
    """Raised when a system profile would be contaminated or incomplete."""


@dataclass(frozen=True)
class ToolInfo:
    name: str
    path: Path | None

    @property
    def available(self) -> bool:
        return self.path is not None and self.path.is_file()

    def identity(self) -> dict[str, object]:
        result: dict[str, object] = {
            "available": self.available,
            "path": str(self.path) if self.path is not None else None,
        }
        if self.available and self.path is not None:
            stat = self.path.stat()
            result.update(
                {
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "sha256": sha256_file(self.path),
                }
            )
        return result


@dataclass(frozen=True)
class ProfilingTools:
    wpr: ToolInfo
    wpa: ToolInfo
    xperf: ToolInfo
    gpuview: ToolInfo
    renderdoccmd: ToolInfo
    qrenderdoc: ToolInfo

    def as_dict(self) -> dict[str, object]:
        return {
            tool.name: tool.identity()
            for tool in (
                self.wpr,
                self.wpa,
                self.xperf,
                self.gpuview,
                self.renderdoccmd,
                self.qrenderdoc,
            )
        }


def _first_existing(candidates: list[Path | None]) -> Path | None:
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return candidate.resolve()
    return None


def _which(name: str) -> Path | None:
    result = shutil.which(name)
    return Path(result) if result is not None else None


def discover_tools() -> ProfilingTools:
    renderdoc_home = os.environ.get("RENDERDOC_HOME")
    renderdoc_roots = [
        Path(renderdoc_home) if renderdoc_home else None,
        REPO_ROOT / "data" / "local" / "tools" / "renderdoc",
        Path("C:/Program Files/RenderDoc"),
    ]
    renderdoccmd = _first_existing(
        [_which("renderdoccmd")]
        + [root / "renderdoccmd.exe" if root is not None else None for root in renderdoc_roots]
    )
    qrenderdoc = _first_existing(
        [_which("qrenderdoc")]
        + [root / "qrenderdoc.exe" if root is not None else None for root in renderdoc_roots]
    )
    return ProfilingTools(
        wpr=ToolInfo("wpr", _first_existing([_which("wpr"), WPT_ROOT / "wpr.exe"])),
        wpa=ToolInfo("wpa", _first_existing([_which("wpa"), WPT_ROOT / "wpa.exe"])),
        xperf=ToolInfo(
            "xperf", _first_existing([_which("xperf"), WPT_ROOT / "xperf.exe"])
        ),
        gpuview=ToolInfo(
            "gpuview",
            _first_existing(
                [_which("GPUView"), WPT_ROOT / "gpuview" / "GPUView.exe"]
            ),
        ),
        renderdoccmd=ToolInfo("renderdoccmd", renderdoccmd),
        qrenderdoc=ToolInfo("qrenderdoc", qrenderdoc),
    )


def is_elevated() -> bool:
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def validate_live_arguments(arguments: list[str]) -> None:
    for argument in arguments:
        option = argument.split("=", 1)[0]
        if option in DENIED_LIVE_ARGUMENTS:
            raise SystemProfileError(
                f"{option} contaminates the representative system-profiler workload"
            )


def build_live_command(
    *,
    skip_host_build: bool,
    live_arguments: list[str],
) -> list[str]:
    if live_arguments and live_arguments[0] == "--":
        live_arguments = live_arguments[1:]
    validate_live_arguments(live_arguments)
    command = [sys.executable, str(LIVE_TEST), "--no-diagnostics"]
    if skip_host_build:
        command.append("--skip-host-build")
    command.extend(live_arguments)
    return command


def build_wpr_start_command(
    wpr: Path,
    temporary_directory: Path,
    *,
    include_gpu: bool,
) -> list[str]:
    command = [str(wpr), "-start", "CPU"]
    if include_gpu:
        command.extend(["-start", "GPU"])
    command.extend(["-filemode", "-recordtempto", str(temporary_directory)])
    return command


def build_renderdoc_command(
    renderdoccmd: Path,
    capture_prefix: Path,
    live_command: list[str],
) -> list[str]:
    return [
        str(renderdoccmd),
        "capture",
        "--working-dir",
        str(REPO_ROOT),
        "--capture-file",
        str(capture_prefix),
        "--wait-for-exit",
        *live_command,
    ]


def _new_capture_directory(kind: str, requested: Path | None) -> Path:
    result = (
        requested.resolve()
        if requested is not None
        else PROFILE_ROOT / f"{kind}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    )
    if result.exists() and any(result.iterdir()):
        raise SystemProfileError(f"capture directory is not empty: {result}")
    result.mkdir(parents=True, exist_ok=True)
    return result


def _run_manifest_identity(capture_started_ns: int) -> dict[str, object] | None:
    try:
        stat = LIVE_RUN_MANIFEST.stat()
        if stat.st_mtime_ns < capture_started_ns:
            return None
        payload = json.loads(LIVE_RUN_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return {
        "path": str(LIVE_RUN_MANIFEST),
        "sha256": sha256_file(LIVE_RUN_MANIFEST),
        "run_id": payload.get("run_id"),
        "status": payload.get("status"),
        "returncode": payload.get("returncode"),
        "target": payload.get("target"),
        "presenter_build": payload.get("presenter_build"),
        "configuration": payload.get("configuration"),
        "cache": payload.get("cache"),
        "cache_after": payload.get("cache_after"),
    }


def _write_manifest(directory: Path, manifest: dict[str, object]) -> Path:
    path = directory / "capture-manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _completed_output(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _wpr_is_recording(wpr: Path) -> bool:
    completed = _completed_output([str(wpr), "-status"])
    output = (completed.stdout + completed.stderr).lower()
    if completed.returncode != 0:
        raise SystemProfileError(f"cannot query WPR status: {output.strip()}")
    return "wpr is not recording" not in output


def _postprocess_etl(xperf: Path, etl: Path, directory: Path) -> list[dict[str, object]]:
    reports = (
        ("processes.txt", ["process", "-thread", "-image", "-withcmdline"]),
        ("cpu-samples.txt", ["profile", "-detail"]),
        ("cpu-utilization.txt", ["profile", "-util"]),
        ("context-switch-process.txt", ["cswitch", "-process"]),
        ("context-switch-thread.txt", ["cswitch", "-thread"]),
        ("trace-statistics.txt", ["tracestats"]),
    )
    results: list[dict[str, object]] = []
    for file_name, action in reports:
        output = directory / file_name
        command = [
            str(xperf),
            "-i",
            str(etl),
            "-target",
            "machine",
            "-o",
            str(output),
            "-a",
            *action,
        ]
        completed = _completed_output(command)
        results.append(
            {
                "name": file_name,
                "command": command,
                "returncode": completed.returncode,
                "stderr": completed.stderr.strip() or None,
                "path": str(output) if output.exists() else None,
            }
        )
    return results


def _dry_run_plan(kind: str, command: list[str], tools: ProfilingTools) -> int:
    print(
        json.dumps(
            {
                "format": "b2-recomp-system-profile-plan",
                "schema_version": 1,
                "kind": kind,
                "command": command,
                "elevated": is_elevated(),
                "tools": tools.as_dict(),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def capture_etw(args: argparse.Namespace, tools: ProfilingTools) -> int:
    if not tools.wpr.available or tools.wpr.path is None:
        raise SystemProfileError("Windows Performance Recorder is not installed")
    if not tools.xperf.available or tools.xperf.path is None:
        raise SystemProfileError("xperf is required for deterministic ETW summaries")
    live_command = build_live_command(
        skip_host_build=not args.rebuild_presenter,
        live_arguments=list(args.live_arguments),
    )
    preview_directory = args.output_dir or PROFILE_ROOT / "etw-<timestamp>"
    start_preview = build_wpr_start_command(
        tools.wpr.path,
        preview_directory,
        include_gpu=not args.cpu_only,
    )
    if args.dry_run:
        return _dry_run_plan("etw", [*start_preview, "--then--", *live_command], tools)
    if not is_elevated():
        raise SystemProfileError(
            "ETW CPU/GPU kernel profiling requires an Administrator PowerShell"
        )
    if _wpr_is_recording(tools.wpr.path):
        raise SystemProfileError("WPR is already recording; refusing to replace that session")

    directory = _new_capture_directory("etw", args.output_dir)
    etl = directory / "b2-gameplay.etl"
    capture_started_ns = time.time_ns()
    started_utc = utc_now()
    started = time.perf_counter()
    start_command = build_wpr_start_command(
        tools.wpr.path,
        directory,
        include_gpu=not args.cpu_only,
    )
    start_result = _completed_output(start_command)
    if start_result.returncode != 0:
        manifest = {
            "format": "b2-recomp-system-profile",
            "schema_version": 1,
            "kind": "etw",
            "status": "wpr-start-failed",
            "started_utc": started_utc,
            "start_command": start_command,
            "returncode": start_result.returncode,
            "stdout": start_result.stdout.strip() or None,
            "stderr": start_result.stderr.strip() or None,
            "tools": tools.as_dict(),
        }
        _write_manifest(directory, manifest)
        raise SystemProfileError("WPR failed to start; see capture-manifest.json")

    live_returncode = 130
    stop_result: subprocess.CompletedProcess[str] | None = None
    try:
        live_returncode = subprocess.run(live_command, cwd=REPO_ROOT, check=False).returncode
    finally:
        stop_result = _completed_output(
            [
                str(tools.wpr.path),
                "-stop",
                str(etl),
                "b2_recomp warm gameplay CPU/GPU scheduling profile",
                "-compress",
            ]
        )
        if stop_result.returncode != 0:
            _completed_output([str(tools.wpr.path), "-cancel"])

    postprocess = (
        _postprocess_etl(tools.xperf.path, etl, directory) if etl.is_file() else []
    )
    manifest = {
        "format": "b2-recomp-system-profile",
        "schema_version": 1,
        "kind": "etw",
        "status": "complete" if stop_result.returncode == 0 else "wpr-stop-failed",
        "started_utc": started_utc,
        "completed_utc": utc_now(),
        "elapsed_seconds": time.perf_counter() - started,
        "repository": cast(dict[str, object], git_identity(REPO_ROOT)),
        "live_command": live_command,
        "live_returncode": live_returncode,
        "live_run_manifest": _run_manifest_identity(capture_started_ns),
        "profiles": ["CPU", *(("GPU",) if not args.cpu_only else ())],
        "trace": {
            "path": str(etl) if etl.is_file() else None,
            "bytes": etl.stat().st_size if etl.is_file() else None,
            "sha256": sha256_file(etl) if etl.is_file() else None,
            "contains_system_wide_data": True,
        },
        "postprocess": postprocess,
        "stop_returncode": stop_result.returncode,
        "stop_stdout": stop_result.stdout.strip() or None,
        "stop_stderr": stop_result.stderr.strip() or None,
        "tools": tools.as_dict(),
    }
    manifest_path = _write_manifest(directory, manifest)
    print(f"ETW capture manifest: {manifest_path}")
    if stop_result.returncode != 0:
        return stop_result.returncode
    return live_returncode


def capture_renderdoc(args: argparse.Namespace, tools: ProfilingTools) -> int:
    if not tools.renderdoccmd.available or tools.renderdoccmd.path is None:
        raise SystemProfileError(
            "RenderDoc is not installed; system_profile.py doctor shows searched locations"
        )
    live_command = build_live_command(
        skip_host_build=not args.rebuild_presenter,
        live_arguments=list(args.live_arguments),
    )
    preview_directory = args.output_dir or PROFILE_ROOT / "renderdoc-<timestamp>"
    command = build_renderdoc_command(
        tools.renderdoccmd.path,
        preview_directory / "b2-frame",
        live_command,
    )
    if args.dry_run:
        return _dry_run_plan("renderdoc", command, tools)

    directory = _new_capture_directory("renderdoc", args.output_dir)
    capture_started_ns = time.time_ns()
    started = time.perf_counter()
    command = build_renderdoc_command(
        tools.renderdoccmd.path,
        directory / "b2-frame",
        live_command,
    )
    print("Press F12 once on representative gameplay, then close the presenter.")
    completed = subprocess.run(command, cwd=REPO_ROOT, check=False)
    captures = sorted(directory.glob("*.rdc"))
    manifest = {
        "format": "b2-recomp-system-profile",
        "schema_version": 1,
        "kind": "renderdoc",
        "status": "complete" if completed.returncode == 0 and captures else "no-capture",
        "started_utc": datetime.fromtimestamp(
            capture_started_ns / 1_000_000_000, timezone.utc
        ).isoformat(timespec="milliseconds"),
        "completed_utc": utc_now(),
        "elapsed_seconds": time.perf_counter() - started,
        "repository": cast(dict[str, object], git_identity(REPO_ROOT)),
        "command": command,
        "returncode": completed.returncode,
        "live_run_manifest": _run_manifest_identity(capture_started_ns),
        "captures": [
            {
                "path": str(capture),
                "bytes": capture.stat().st_size,
                "sha256": sha256_file(capture),
            }
            for capture in captures
        ],
        "tools": tools.as_dict(),
    }
    manifest_path = _write_manifest(directory, manifest)
    print(f"RenderDoc capture manifest: {manifest_path}")
    return completed.returncode if completed.returncode != 0 or captures else 1


def doctor(args: argparse.Namespace, tools: ProfilingTools) -> int:
    wpr_recording: bool | None = None
    if tools.wpr.available and tools.wpr.path is not None:
        try:
            wpr_recording = _wpr_is_recording(tools.wpr.path)
        except SystemProfileError:
            pass
    report = {
        "format": "b2-recomp-system-profile-doctor",
        "schema_version": 1,
        "elevated": is_elevated(),
        "wpr_recording": wpr_recording,
        "etw_ready": bool(
            tools.wpr.available and tools.wpa.available and tools.xperf.available
        ),
        "renderdoc_ready": bool(
            tools.renderdoccmd.available and tools.qrenderdoc.available
        ),
        "tools": tools.as_dict(),
    }
    rendered = json.dumps(report, indent=2 if args.pretty else None, sort_keys=True)
    print(rendered)
    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(rendered + "\n", encoding="utf-8")
    return 0


def _add_capture_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--rebuild-presenter", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "live_arguments",
        nargs=argparse.REMAINDER,
        help="additional clean live_test.py arguments after --",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor_parser = subparsers.add_parser("doctor", help="inventory supported profilers")
    doctor_parser.add_argument("--json-output", type=Path)
    doctor_parser.add_argument("--pretty", action="store_true")

    etw_parser = subparsers.add_parser("capture-etw", help="capture WPR CPU/GPU ETW data")
    _add_capture_arguments(etw_parser)
    etw_parser.add_argument(
        "--cpu-only",
        action="store_true",
        help="omit the GPU scheduling provider",
    )

    renderdoc_parser = subparsers.add_parser(
        "capture-renderdoc", help="launch the normal runtime through RenderDoc"
    )
    _add_capture_arguments(renderdoc_parser)

    args = parser.parse_args(argv)
    tools = discover_tools()
    try:
        if args.command == "doctor":
            return doctor(args, tools)
        if args.command == "capture-etw":
            return capture_etw(args, tools)
        if args.command == "capture-renderdoc":
            return capture_renderdoc(args, tools)
    except SystemProfileError as exc:
        parser.error(str(exc))
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
