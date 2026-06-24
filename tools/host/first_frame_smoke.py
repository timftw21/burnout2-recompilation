#!/usr/bin/env python3
"""Build and run the Windows/Vulkan first interactive frame smoke test."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE = REPO_ROOT / "runtime" / "host" / "vulkan_first_frame.cpp"
DEFAULT_BUILD_DIR = REPO_ROOT / "build" / "local" / "first-frame"
DEFAULT_EXE = DEFAULT_BUILD_DIR / "b2_first_frame.exe"
DEFAULT_DEBUG_JSON = REPO_ROOT / "reports" / "local" / "first-frame" / "events.jsonl"
DEFAULT_SUMMARY_JSON = REPO_ROOT / "reports" / "local" / "first-frame" / "summary.json"
DEFAULT_VULKAN_SDK = Path("C:/VulkanSDK/1.4.341.1")
DEFAULT_LLVM_BIN = Path("C:/Program Files/LLVM/bin")


class FirstFrameSmokeError(RuntimeError):
    """Raised when the first-frame smoke workflow cannot complete."""


@dataclass(frozen=True)
class Toolchain:
    clangxx: Path
    vulkan_sdk: Path

    @property
    def include_dir(self) -> Path:
        return self.vulkan_sdk / "Include"

    @property
    def lib_dir(self) -> Path:
        return self.vulkan_sdk / "Lib"

    @property
    def vulkan_lib(self) -> Path:
        return self.lib_dir / "vulkan-1.lib"


def discover_toolchain(
    *,
    clangxx: Path | None = None,
    vulkan_sdk: Path | None = None,
) -> Toolchain:
    clang_candidate = clangxx or _find_clangxx()
    sdk_candidate = vulkan_sdk or _find_vulkan_sdk()
    if not clang_candidate.exists():
        raise FirstFrameSmokeError(f"clang++ not found: {clang_candidate}")
    if not (sdk_candidate / "Include" / "vulkan" / "vulkan.h").exists():
        raise FirstFrameSmokeError(f"Vulkan headers not found under {sdk_candidate}")
    if not (sdk_candidate / "Lib" / "vulkan-1.lib").exists():
        raise FirstFrameSmokeError(f"Vulkan import library not found under {sdk_candidate}")
    return Toolchain(clang_candidate, sdk_candidate)


def build_command(
    *,
    source: Path,
    output: Path,
    toolchain: Toolchain,
) -> list[str]:
    return [
        str(toolchain.clangxx),
        "-std=c++17",
        "-DUNICODE",
        "-D_UNICODE",
        f"-I{toolchain.include_dir}",
        str(source),
        f"-L{toolchain.lib_dir}",
        "-lvulkan-1",
        "-luser32",
        "-lgdi32",
        "-lshell32",
        "-o",
        str(output),
    ]


def compile_first_frame(
    *,
    source: Path = DEFAULT_SOURCE,
    output: Path = DEFAULT_EXE,
    toolchain: Toolchain | None = None,
) -> dict[str, Any]:
    active_toolchain = toolchain or discover_toolchain()
    output.parent.mkdir(parents=True, exist_ok=True)
    command = build_command(source=source, output=output, toolchain=active_toolchain)
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise FirstFrameSmokeError(
            "first-frame compile failed\n"
            f"command: {' '.join(command)}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    return {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "output": str(output),
    }


def run_first_frame(
    *,
    executable: Path = DEFAULT_EXE,
    debug_json: Path = DEFAULT_DEBUG_JSON,
    width: int = 640,
    height: int = 480,
    max_frames: int = 3,
    inject_input: bool = True,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    if not executable.exists():
        raise FirstFrameSmokeError(f"first-frame executable is missing: {executable}")
    debug_json.parent.mkdir(parents=True, exist_ok=True)
    if debug_json.exists():
        debug_json.unlink()
    command = [
        str(executable),
        "--width",
        str(width),
        "--height",
        str(height),
        "--max-frames",
        str(max_frames),
        "--debug-json",
        str(debug_json),
    ]
    if inject_input:
        command.append("--inject-input")

    env = os.environ.copy()
    llvm_bin = str(DEFAULT_LLVM_BIN)
    if DEFAULT_LLVM_BIN.exists() and llvm_bin not in env.get("PATH", ""):
        env["PATH"] = llvm_bin + os.pathsep + env.get("PATH", "")

    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
        env=env,
    )
    events = read_debug_events(debug_json)
    return {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "debug_json": str(debug_json),
        "events": events,
    }


def read_debug_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise FirstFrameSmokeError(
                f"invalid JSONL event at {path}:{line_number}: {exc}"
            ) from exc
    return events


def summarize_smoke(
    *,
    compile_result: dict[str, Any] | None,
    run_result: dict[str, Any],
    summary_output: Path | None = None,
) -> dict[str, Any]:
    events = run_result["events"]
    counts = Counter(event.get("event") for event in events)
    frame_events = [
        event for event in events if event.get("event") == "frame_presented"
    ]
    input_events = [
        event for event in events if event.get("event") == "input_event"
    ]
    selected_device = next(
        (event for event in events if event.get("event") == "physical_device_selected"),
        None,
    )
    summary = {
        "format": "b2-recomp-first-frame-smoke",
        "public_safe": False,
        "target_platform": "windows",
        "renderer_backend": "vulkan",
        "build": {
            "compiled": compile_result is not None,
            "returncode": compile_result["returncode"] if compile_result else None,
            "output": compile_result["output"] if compile_result else str(DEFAULT_EXE),
        },
        "run": {
            "returncode": run_result["returncode"],
            "debug_json": run_result["debug_json"],
            "event_counts": dict(sorted(counts.items())),
            "main_loop_entered": counts.get("main_loop_enter", 0) == 1,
            "main_loop_exited": counts.get("main_loop_exit", 0) == 1,
            "frames_presented": len(frame_events),
            "input_events": len(input_events),
            "visible_frame_presented": len(frame_events) > 0,
            "selected_device": selected_device,
        },
    }
    if summary_output is not None:
        summary_output.parent.mkdir(parents=True, exist_ok=True)
        summary_output.write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    return summary


def _find_clangxx() -> Path:
    env_path = shutil.which("clang++")
    if env_path:
        return Path(env_path)
    candidate = DEFAULT_LLVM_BIN / "clang++.exe"
    return candidate


def _find_vulkan_sdk() -> Path:
    env_sdk = os.environ.get("VULKAN_SDK")
    if env_sdk:
        return Path(env_sdk)
    if DEFAULT_VULKAN_SDK.exists():
        return DEFAULT_VULKAN_SDK
    sdk_root = Path("C:/VulkanSDK")
    if sdk_root.exists():
        versions = sorted(
            [path for path in sdk_root.iterdir() if path.is_dir()],
            reverse=True,
        )
        if versions:
            return versions[0]
    return DEFAULT_VULKAN_SDK


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build and run the Vulkan first interactive frame smoke test."
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--exe", type=Path, default=DEFAULT_EXE)
    parser.add_argument("--debug-json", type=Path, default=DEFAULT_DEBUG_JSON)
    parser.add_argument("--summary-output", type=Path, default=DEFAULT_SUMMARY_JSON)
    parser.add_argument("--clangxx", type=Path)
    parser.add_argument("--vulkan-sdk", type=Path)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--max-frames", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--no-inject-input", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    compile_result = None
    if not args.skip_build:
        toolchain = discover_toolchain(
            clangxx=args.clangxx,
            vulkan_sdk=args.vulkan_sdk,
        )
        compile_result = compile_first_frame(
            source=args.source,
            output=args.exe,
            toolchain=toolchain,
        )

    run_result = run_first_frame(
        executable=args.exe,
        debug_json=args.debug_json,
        width=args.width,
        height=args.height,
        max_frames=args.max_frames,
        inject_input=not args.no_inject_input,
        timeout_seconds=args.timeout_seconds,
    )
    summary = summarize_smoke(
        compile_result=compile_result,
        run_result=run_result,
        summary_output=args.summary_output,
    )
    print(json.dumps(summary, indent=2 if args.pretty else None, sort_keys=True))
    return 0 if run_result["returncode"] == 0 else run_result["returncode"]


if __name__ == "__main__":
    raise SystemExit(main())
