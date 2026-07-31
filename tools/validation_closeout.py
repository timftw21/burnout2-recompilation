#!/usr/bin/env python3
"""Launch and monitor the non-blocking exhaustive validation matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.validation_runner import ValidationNode, run_validation_graph


DEFAULT_CLOSEOUT_ROOT = REPO_ROOT / "reports" / "local" / "validation" / "closeout"
DEFAULT_TIMING_HISTORY = (
    REPO_ROOT / "build" / "local" / "dev-check" / "closeout-timings.json"
)


def _save_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _git_output(*arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unknown"


def _git_bytes(*arguments: str) -> bytes:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.stdout if completed.returncode == 0 else b""


def worktree_identity() -> tuple[str, str, str]:
    revision = _git_output("rev-parse", "HEAD")
    branch = _git_output("branch", "--show-current") or "detached"
    digest = hashlib.sha256(revision.encode("utf-8"))
    digest.update(_git_bytes("diff", "--binary", "HEAD"))
    untracked = _git_bytes("ls-files", "--others", "--exclude-standard", "-z")
    for encoded_name in sorted(name for name in untracked.split(b"\0") if name):
        digest.update(encoded_name)
        try:
            digest.update((REPO_ROOT / os.fsdecode(encoded_name)).read_bytes())
        except OSError:
            digest.update(b"<missing>")
    return revision, branch, digest.hexdigest()


def _request_superseded_closeouts(root: Path, *, branch: str) -> list[str]:
    superseded: list[str] = []
    if not root.is_dir():
        return superseded
    for manifest_path in sorted(root.glob("*/manifest.json")):
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (
            not isinstance(payload, dict)
            or payload.get("branch") != branch
            or payload.get("status") not in {"queued", "running"}
        ):
            continue
        request_path = manifest_path.parent / "cancel.requested"
        request_path.write_text("superseded by a newer closeout\n", encoding="utf-8")
        superseded.append(manifest_path.parent.name)
    return superseded


def launch_closeout(
    *,
    jobs: int,
    replay_capsules: Sequence[Path] = (),
    replay_max_steps: int = 10_000_000,
    closeout_root: Path = DEFAULT_CLOSEOUT_ROOT,
) -> Path:
    revision, branch, identity = worktree_identity()
    superseded = _request_superseded_closeouts(closeout_root, branch=branch)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    run_directory = closeout_root / f"{stamp}-{identity[:12]}"
    run_directory.mkdir(parents=True, exist_ok=False)
    manifest_path = run_directory / "manifest.json"
    manifest: dict[str, object] = {
        "schema": 1,
        "status": "queued",
        "created_utc": datetime.now(UTC).isoformat(),
        "revision": revision,
        "branch": branch,
        "worktree_identity": identity,
        "jobs": jobs,
        "replay_capsules": [str(path.resolve()) for path in replay_capsules],
        "replay_max_steps": replay_max_steps,
        "superseded_runs": superseded,
    }
    _save_json(manifest_path, manifest)
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "run",
        "--run-directory",
        str(run_directory),
        "--jobs",
        str(jobs),
        "--replay-max-steps",
        str(replay_max_steps),
    ]
    for capsule in replay_capsules:
        command.extend(("--replay-capsule", str(capsule.resolve())))
    log_path = run_directory / "closeout.log"
    with log_path.open("w", encoding="utf-8") as log_stream:
        creationflags = 0
        if os.name == "nt":
            creationflags = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        process = subprocess.Popen(  # noqa: S603 - exact project-owned command
            command,
            cwd=REPO_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
            close_fds=True,
        )
    try:
        current_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        current_manifest = manifest
    if not isinstance(current_manifest, dict):
        current_manifest = manifest
    current_manifest["pid"] = process.pid
    current_manifest["command"] = subprocess.list2cmdline(command)
    _save_json(manifest_path, current_manifest)
    return run_directory


def build_closeout_nodes(
    *,
    jobs: int,
    run_directory: Path,
    replay_capsules: Sequence[Path],
    replay_max_steps: int,
) -> dict[str, ValidationNode]:
    native_parallel = max(1, jobs // 3)
    nodes = {
        "asset_free": ValidationNode(
            "asset_free",
            (sys.executable, "tools/dev_check.py", "--all", "--jobs", str(jobs)),
            "complete cached asset-free gate",
            cacheable=False,
            exclusive=True,
            priority=0,
            budget_seconds=300.0,
        )
    }
    for priority, preset in enumerate(("debug", "release", "sanitizer"), start=10):
        nodes[f"native_{preset}"] = ValidationNode(
            f"native_{preset}",
            (
                sys.executable,
                "tools/native_build.py",
                "--preset",
                preset,
                "--parallel",
                str(native_parallel),
            ),
            f"complete {preset} native build and CTest",
            dependencies=("asset_free",),
            cacheable=False,
            priority=priority,
            budget_seconds=600.0,
        )
    nodes["strict_presenter"] = ValidationNode(
        "strict_presenter",
        (
            sys.executable,
            "tools/native_build.py",
            "--preset",
            "release",
            "--presenter",
            "--parallel",
            str(native_parallel),
        ),
        "strict release presenter build and CTest",
        dependencies=("native_release",),
        cacheable=False,
        priority=30,
        budget_seconds=600.0,
    )
    for index, capsule in enumerate(replay_capsules):
        name = f"long_replay_{index:02d}"
        nodes[name] = ValidationNode(
            name,
            (
                sys.executable,
                "tools/playability/differential_replay.py",
                "execute",
                str(capsule),
                "--experimental",
                "interpreter",
                "--max-steps",
                str(replay_max_steps),
                "--report",
                str(run_directory / f"{name}.json"),
            ),
            "long deterministic local replay",
            dependencies=("asset_free",),
            cacheable=False,
            priority=20,
            budget_seconds=600.0,
        )
    return nodes


def run_closeout(
    run_directory: Path,
    *,
    jobs: int,
    replay_capsules: Sequence[Path],
    replay_max_steps: int,
) -> int:
    manifest_path = run_directory / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"invalid closeout manifest: {manifest_path}") from exc
    if not isinstance(manifest, dict):
        raise RuntimeError(f"invalid closeout manifest: {manifest_path}")
    manifest["status"] = "running"
    manifest["started_utc"] = datetime.now(UTC).isoformat()
    _save_json(manifest_path, manifest)
    nodes = build_closeout_nodes(
        jobs=jobs,
        run_directory=run_directory,
        replay_capsules=replay_capsules,
        replay_max_steps=replay_max_steps,
    )
    summary = run_validation_graph(
        nodes,
        tuple(nodes),
        cache_path=run_directory / "stage-cache.json",
        root=REPO_ROOT,
        jobs=min(3, max(1, jobs)),
        no_cache=True,
        timing_path=DEFAULT_TIMING_HISTORY,
        cancellation_path=run_directory / "cancel.requested",
    )
    cancelled = (run_directory / "cancel.requested").exists()
    manifest.update(
        {
            "status": "superseded" if cancelled else ("passed" if summary.passed else "failed"),
            "completed_utc": datetime.now(UTC).isoformat(),
            "elapsed_seconds": round(summary.elapsed_seconds, 6),
            "timing_history": str(summary.timing_path),
            "failure_capsule": (
                str(summary.failure_capsule) if summary.failure_capsule is not None else None
            ),
            "results": {
                name: {
                    "status": result.status,
                    "returncode": result.returncode,
                    "duration_seconds": round(result.duration_seconds, 6),
                }
                for name, result in summary.results.items()
            },
        }
    )
    _save_json(manifest_path, manifest)
    return 0 if summary.passed else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    launch = subparsers.add_parser("launch", help="Start a detached closeout and return.")
    launch.add_argument("--jobs", type=int, default=min(6, os.cpu_count() or 1))
    launch.add_argument("--replay-capsule", type=Path, action="append", default=[])
    launch.add_argument("--replay-max-steps", type=int, default=10_000_000)
    run = subparsers.add_parser("run", help="Execute one queued closeout in the foreground.")
    run.add_argument("--run-directory", type=Path, required=True)
    run.add_argument("--jobs", type=int, required=True)
    run.add_argument("--replay-capsule", type=Path, action="append", default=[])
    run.add_argument("--replay-max-steps", type=int, default=10_000_000)
    status = subparsers.add_parser("status", help="Print one closeout manifest.")
    status.add_argument("run_directory", type=Path)
    args = parser.parse_args(argv)
    if args.command == "status":
        print((args.run_directory / "manifest.json").read_text(encoding="utf-8"), end="")
        return 0
    if args.jobs <= 0:
        parser.error("--jobs must be positive")
    if args.replay_max_steps <= 0:
        parser.error("--replay-max-steps must be positive")
    if args.command == "launch":
        run_directory = launch_closeout(
            jobs=args.jobs,
            replay_capsules=args.replay_capsule,
            replay_max_steps=args.replay_max_steps,
        )
        print(f"Closeout launched: {run_directory}")
        print(
            "Status: "
            + subprocess.list2cmdline(
                (
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "status",
                    str(run_directory),
                )
            )
        )
        return 0
    return run_closeout(
        args.run_directory,
        jobs=args.jobs,
        replay_capsules=args.replay_capsule,
        replay_max_steps=args.replay_max_steps,
    )


if __name__ == "__main__":
    raise SystemExit(main())
