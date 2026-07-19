#!/usr/bin/env python3
"""Build and run the Windows/Vulkan first interactive frame smoke test."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
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
DEFAULT_SCREENSHOT = REPO_ROOT / "reports" / "local" / "first-frame" / "frame.bmp"
DEFAULT_HOTKEY_SCREENSHOT_DIR = REPO_ROOT / "reports" / "local" / "screenshots"
DEFAULT_RENDER_STREAM_JSON = REPO_ROOT / "reports" / "local" / "render" / "recovered-d3d-stream.json"
DEFAULT_VERTEX_SHADER = REPO_ROOT / "runtime" / "host" / "shaders" / "nv2a_inline.vert"
DEFAULT_FRAGMENT_SHADER = REPO_ROOT / "runtime" / "host" / "shaders" / "nv2a_inline.frag"
DEFAULT_VERTEX_SPV = DEFAULT_BUILD_DIR / "nv2a_inline.vert.spv"
DEFAULT_FRAGMENT_SPV = DEFAULT_BUILD_DIR / "nv2a_inline.frag.spv"
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
        "-O2",
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


def compile_shaders(
    *,
    toolchain: Toolchain,
    vertex_source: Path = DEFAULT_VERTEX_SHADER,
    fragment_source: Path = DEFAULT_FRAGMENT_SHADER,
    vertex_output: Path = DEFAULT_VERTEX_SPV,
    fragment_output: Path = DEFAULT_FRAGMENT_SPV,
) -> dict[str, Any]:
    glslc = toolchain.vulkan_sdk / "Bin" / "glslc.exe"
    if not glslc.exists():
        raise FirstFrameSmokeError(f"glslc not found: {glslc}")
    commands: list[list[str]] = []
    for source, output in (
        (vertex_source, vertex_output),
        (fragment_source, fragment_output),
    ):
        output.parent.mkdir(parents=True, exist_ok=True)
        command = [str(glslc), str(source), "-o", str(output)]
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise FirstFrameSmokeError(
                "shader compile failed\n"
                f"command: {' '.join(command)}\n"
                f"stdout:\n{completed.stdout}\n"
                f"stderr:\n{completed.stderr}"
            )
        commands.append(command)
    return {
        "returncode": 0,
        "commands": commands,
        "vertex_output": str(vertex_output),
        "fragment_output": str(fragment_output),
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
    render_stream_json: Path | None = None,
    screenshot: Path | None = None,
    hotkey_screenshot_directory: Path | None = DEFAULT_HOTKEY_SCREENSHOT_DIR,
    vertex_shader: Path = DEFAULT_VERTEX_SPV,
    fragment_shader: Path = DEFAULT_FRAGMENT_SPV,
    live_render_stream: bool = False,
    controller_state_json: Path | None = None,
    strict_render_validation: bool = False,
    flip_audit_ack: Path | None = None,
    flip_audit_frame_directory: Path | None = None,
    flip_audit_max_flips: int = 0,
    flip_audit_health_interval: int = 30,
    analyze_render_stream: bool = False,
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
    if analyze_render_stream:
        command.append("--analyze-render-stream")
    if inject_input and not analyze_render_stream:
        command.append("--inject-input")
    if render_stream_json is not None:
        command.extend([
            (
                "--live-render-stream-json"
                if live_render_stream or analyze_render_stream
                else "--render-stream-json"
            ),
            str(render_stream_json),
        ])
    if controller_state_json is not None and not analyze_render_stream:
        controller_state_json.parent.mkdir(parents=True, exist_ok=True)
        command.extend(["--controller-state-json", str(controller_state_json)])
    if strict_render_validation and not analyze_render_stream:
        command.append("--strict-render-validation")
    if (flip_audit_ack is None) != (flip_audit_frame_directory is None):
        raise FirstFrameSmokeError(
            "flip audit acknowledgement and frame directory must be used together"
        )
    if (
        flip_audit_ack is not None
        and flip_audit_frame_directory is not None
        and not analyze_render_stream
    ):
        flip_audit_ack.parent.mkdir(parents=True, exist_ok=True)
        flip_audit_frame_directory.mkdir(parents=True, exist_ok=True)
        command.extend(["--flip-audit-ack", str(flip_audit_ack)])
        command.extend(
            ["--flip-audit-frame-directory", str(flip_audit_frame_directory)]
        )
        if flip_audit_max_flips > 0:
            command.extend(["--flip-audit-max-flips", str(flip_audit_max_flips)])
        command.extend(
            ["--flip-audit-health-interval", str(flip_audit_health_interval)]
        )
    if screenshot is not None and not analyze_render_stream:
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        if screenshot.exists():
            screenshot.unlink()
        command.extend(["--screenshot", str(screenshot)])
    if hotkey_screenshot_directory is not None and not analyze_render_stream:
        hotkey_screenshot_directory.mkdir(parents=True, exist_ok=True)
        command.extend([
            "--hotkey-screenshot-directory",
            str(hotkey_screenshot_directory),
        ])
    if not analyze_render_stream:
        command.extend(["--vertex-shader", str(vertex_shader)])
        command.extend(["--fragment-shader", str(fragment_shader)])

    env = os.environ.copy()
    llvm_bin = str(DEFAULT_LLVM_BIN)
    if DEFAULT_LLVM_BIN.exists() and llvm_bin not in env.get("PATH", ""):
        env["PATH"] = llvm_bin + os.pathsep + env.get("PATH", "")

    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=timeout_seconds if timeout_seconds > 0 else None,
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
        "screenshot": str(screenshot) if screenshot is not None else None,
        "hotkey_screenshot_directory": (
            str(hotkey_screenshot_directory)
            if hotkey_screenshot_directory is not None
            else None
        ),
        "analyze_render_stream": analyze_render_stream,
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


def inspect_bmp(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    if len(data) < 54 or data[:2] != b"BM":
        raise FirstFrameSmokeError(f"invalid BMP screenshot: {path}")
    pixel_offset = struct.unpack_from("<I", data, 10)[0]
    width, signed_height = struct.unpack_from("<ii", data, 18)
    planes, bits_per_pixel = struct.unpack_from("<HH", data, 26)
    if width <= 0 or signed_height == 0 or planes != 1 or bits_per_pixel != 32:
        raise FirstFrameSmokeError(f"unsupported BMP screenshot layout: {path}")
    height = abs(signed_height)
    expected_bytes = width * height * 4
    if pixel_offset + expected_bytes > len(data):
        raise FirstFrameSmokeError(f"truncated BMP screenshot: {path}")
    return {
        "path": str(path),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "width": width,
        "height": height,
        "bits_per_pixel": bits_per_pixel,
    }


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
    input_processed_events = [
        event for event in events if event.get("event") == "input_injection_processed"
    ]
    translated_work_events = [
        event for event in events if event.get("event") == "translated_render_work_recorded"
    ]
    recovered_d3d_events = [
        event for event in events if event.get("event") == "recovered_d3d_command_stream_loaded"
    ]
    interpreted_d3d_events = [
        event for event in events if event.get("event") == "d3d8_stream_interpreted"
    ]
    readback_events = [
        event for event in events if event.get("event") == "frame_readback_captured"
    ]
    hotkey_capture_events = [
        event
        for event in events
        if event.get("event") == "hotkey_render_capture_retained"
    ]
    flip_audit_events = [
        event
        for event in events
        if event.get("event") == "lossless_flip_audited"
        and bool(event.get("readback_captured", False))
    ]
    frontend_text_events = [
        event for event in events if event.get("event") == "frontend_text_draw_recorded"
    ]
    native_resource_events = [
        event for event in events if event.get("event") == "nv2a_native_resources_created"
    ]
    render_validation_events = [
        event for event in events if event.get("event") == "render_validation"
    ]
    vertex_transform_events = [
        event
        for event in events
        if event.get("event") == "nv2a_vertex_transform_diagnostics"
    ]
    stream_analysis_events = [
        event
        for event in events
        if event.get("event") == "render_stream_analysis_complete"
    ]
    screenshot_path = run_result.get("screenshot")
    screenshot = (
        inspect_bmp(Path(screenshot_path))
        if screenshot_path is not None and Path(screenshot_path).exists()
        else None
    )
    translated_command_counts = [
        int(event.get("translated_commands", 0))
        for event in frame_events
        if str(event.get("translated_commands", "")).isdigit()
    ]
    source_d3d_command_counts = [
        int(event.get("source_d3d_commands", 0))
        for event in frame_events
        if str(event.get("source_d3d_commands", "")).isdigit()
    ]
    target_frame_us: list[int] = []
    pacing_sleep_us: list[int] = []
    for event in frame_events:
        try:
            target_frame_us.append(
                int(event["target_frame_us"])
                if event.get("target_frame_us") is not None
                else int(float(event["target_frame_ms"]) * 1000)
            )
        except (KeyError, TypeError, ValueError):
            pass
        try:
            pacing_sleep_us.append(
                int(event["pacing_sleep_us"])
                if event.get("pacing_sleep_us") is not None
                else int(float(event["pacing_sleep_ms"]) * 1000)
            )
        except (KeyError, TypeError, ValueError):
            pass
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
            "stderr": run_result.get("stderr") or None,
            "debug_json": run_result["debug_json"],
            "event_counts": dict(sorted(counts.items())),
            "main_loop_entered": counts.get("main_loop_enter", 0) == 1,
            "main_loop_exited": counts.get("main_loop_exit", 0) == 1,
            "render_stream_analysis_completed": len(stream_analysis_events) == 1,
            "vertex_transform_diagnostics": (
                vertex_transform_events[-1] if vertex_transform_events else None
            ),
            "frames_presented": len(frame_events),
            "input_events": len(input_events),
            "input_keydown_events": sum(
                1 for event in input_events if event.get("message") == "keydown"
            ),
            "input_keyup_events": sum(
                1 for event in input_events if event.get("message") == "keyup"
            ),
            "injected_input_processed_before_first_frame": any(
                str(event.get("before_frame")) == "1"
                and str(event.get("input_events", "")).isdigit()
                and int(event.get("input_events", 0)) >= 2
                for event in input_processed_events
            ),
            "injected_input_processed_messages": max(
                (
                    int(event.get("messages", 0))
                    for event in input_processed_events
                    if str(event.get("messages", "")).isdigit()
                ),
                default=0,
            ),
            "visible_frame_presented": len(frame_events) > 0,
            "pixel_readback_captured": (
                bool(readback_events) and screenshot is not None
            )
            or bool(flip_audit_events),
            "lossless_flip_readback_count": len(flip_audit_events),
            "hotkey_screenshot_count": sum(
                1 for event in readback_events if event.get("trigger") == "f12"
            ),
            "hotkey_screenshot_outputs": [
                event.get("output")
                for event in readback_events
                if event.get("trigger") == "f12" and event.get("output")
            ],
            "hotkey_render_capture_count": sum(
                1 for event in hotkey_capture_events if event.get("manifest")
            ),
            "hotkey_render_capture_manifests": [
                event.get("manifest")
                for event in hotkey_capture_events
                if event.get("manifest")
            ],
            "recovered_frontend_text_drawn": len(frontend_text_events) == 1,
            "recovered_frontend_text": next(
                (event.get("text") for event in frontend_text_events if event.get("text")),
                None,
            ),
            "frontend_text_rectangles": max(
                (
                    int(event.get("rectangles", 0))
                    for event in frontend_text_events
                    if str(event.get("rectangles", "")).isdigit()
                ),
                default=0,
            ),
            "nv2a_native_vertices": max(
                (int(event.get("vertices", 0)) for event in native_resource_events),
                default=0,
            ),
            "nv2a_native_draws": max(
                (int(event.get("draws", 0)) for event in native_resource_events),
                default=0,
            ),
            "nv2a_presented_draws": max(
                (int(event.get("presented_draws", 0)) for event in native_resource_events),
                default=0,
            ),
            "nv2a_guest_flips": max(
                (int(event.get("guest_flips", 0)) for event in native_resource_events),
                default=0,
            ),
            "nv2a_native_textures": max(
                (int(event.get("textures", 0)) for event in native_resource_events),
                default=0,
            ),
            "render_validation_passed": bool(render_validation_events) and all(
                event.get("passed") in (True, "true", 1, "1")
                for event in render_validation_events
            ),
            "unsupported_texture_resource_count": max(
                (
                    int(event.get("unsupported_texture_resource_count", 0))
                    for event in render_validation_events
                ),
                default=0,
            ),
            "unmatched_presented_texture_draw_count": max(
                (
                    int(event.get("unmatched_presented_texture_draw_count", 0))
                    for event in render_validation_events
                ),
                default=0,
            ),
            "unsupported_presented_primitive_count": max(
                (
                    int(event.get("unsupported_presented_primitive_count", 0))
                    for event in render_validation_events
                ),
                default=0,
            ),
            "missing_presented_indexed_resource_draw_count": max(
                (
                    int(
                        event.get(
                            "missing_presented_indexed_resource_draw_count",
                            0,
                        )
                    )
                    for event in render_validation_events
                ),
                default=0,
            ),
            "vertex_buffer_resource_count": max(
                (
                    int(event.get("vertex_buffer_resource_count", 0))
                    for event in render_validation_events
                ),
                default=0,
            ),
            "offscreen_render_target_draw_count": max(
                (
                    int(event.get("offscreen_render_target_draw_count", 0))
                    for event in render_validation_events
                ),
                default=0,
            ),
            "materialized_indexed_draw_count": max(
                (
                    int(event.get("materialized_indexed_draws", 0))
                    for event in native_resource_events
                ),
                default=0,
            ),
            "materialized_indexed_vertex_count": max(
                (
                    int(event.get("materialized_indexed_vertices", 0))
                    for event in native_resource_events
                ),
                default=0,
            ),
            "unsupported_draw_arrays_count": max(
                (
                    int(event.get("unsupported_draw_arrays_count", 0))
                    for event in render_validation_events
                ),
                default=0,
            ),
            "nv2a_presented_half_quad_recovered": any(
                event.get("presented_half_quad_recovered") in (True, "true", 1, "1")
                for event in native_resource_events
            ),
            "nv2a_presented_overscan_height_recovered": any(
                event.get("presented_overscan_height_recovered")
                in (True, "true", 1, "1")
                for event in native_resource_events
            ),
            "screenshot": screenshot,
            "readback_unique_colors": max(
                (
                    int(event.get("unique_colors", 0))
                    for event in readback_events
                    if str(event.get("unique_colors", "")).isdigit()
                ),
                default=0,
            ),
            "readback_dominant_rgba": next(
                (
                    int(event["dominant_rgba"])
                    for event in readback_events
                    if str(event.get("dominant_rgba", "")).isdigit()
                ),
                None,
            ),
            "readback_dominant_count": max(
                (
                    int(event.get("dominant_count", 0))
                    for event in readback_events
                    if str(event.get("dominant_count", "")).isdigit()
                ),
                default=0,
            ),
            "translated_renderer_work": len(translated_work_events) > 0,
            "translated_command_count": max(translated_command_counts, default=0),
            "recovered_d3d_command_stream": len(recovered_d3d_events) > 0,
            "recovered_d3d_command_count": max(source_d3d_command_counts, default=0),
            "recovered_d3d_stream_source": next(
                (
                    event.get("source")
                    for event in recovered_d3d_events
                    if event.get("source") is not None
                ),
                None,
            ),
            "recovered_d3d_mmio_writes": max(
                (
                    int(event.get("mmio_writes", 0))
                    for event in recovered_d3d_events
                    if str(event.get("mmio_writes", "")).isdigit()
                ),
                default=0,
            ),
            "recovered_d3d_push_buffer_writes": max(
                (
                    int(event.get("push_buffer_writes", 0))
                    for event in recovered_d3d_events
                    if str(event.get("push_buffer_writes", "")).isdigit()
                ),
                default=0,
            ),
            "d3d8_method_interpretation": len(interpreted_d3d_events) > 0,
            "d3d8_translation_semantics": next(
                (
                    event.get("translation_semantics")
                    for event in interpreted_d3d_events
                    if event.get("translation_semantics") is not None
                ),
                None,
            ),
            "d3d8_push_buffer_words": max(
                (
                    int(event.get("push_buffer_words", 0))
                    for event in interpreted_d3d_events
                    if str(event.get("push_buffer_words", "")).isdigit()
                ),
                default=0,
            ),
            "d3d8_method_packets": max(
                (
                    int(event.get("method_packets", 0))
                    for event in interpreted_d3d_events
                    if str(event.get("method_packets", "")).isdigit()
                ),
                default=0,
            ),
            "d3d8_interpreted_methods": max(
                (
                    int(event.get("interpreted_methods", 0))
                    for event in interpreted_d3d_events
                    if str(event.get("interpreted_methods", "")).isdigit()
                ),
                default=0,
            ),
            "d3d8_zero_count_method_words": max(
                (
                    int(event.get("zero_count_method_words", 0))
                    for event in interpreted_d3d_events
                    if str(event.get("zero_count_method_words", "")).isdigit()
                ),
                default=0,
            ),
            "d3d8_surface_payload_samples": max(
                (
                    int(event.get("surface_payload_samples", 0))
                    for event in interpreted_d3d_events
                    if str(event.get("surface_payload_samples", "")).isdigit()
                ),
                default=0,
            ),
            "d3d8_surface_payload_dominant_count": max(
                (
                    int(event.get("surface_payload_dominant_count", 0))
                    for event in interpreted_d3d_events
                    if str(event.get("surface_payload_dominant_count", "")).isdigit()
                ),
                default=0,
            ),
            "d3d8_surface_payload_color_valid": any(
                str(event.get("surface_payload_color_valid", "")).casefold() == "true"
                for event in interpreted_d3d_events
            ),
            "d3d8_diagnostic_clear_source": next(
                (
                    event.get("diagnostic_clear_source")
                    for event in interpreted_d3d_events
                    if event.get("diagnostic_clear_source") is not None
                ),
                None,
            ),
            "d3d8_clear_color_valid": any(
                str(event.get("clear_color_valid", "")).casefold() == "true"
                for event in interpreted_d3d_events
            ),
            "frame_pacing": {
                "target_frame_us": max(target_frame_us, default=None),
                "target_frame_ms": (
                    max(target_frame_us) / 1000.0 if target_frame_us else None
                ),
                "events_with_pacing": len(pacing_sleep_us),
                "max_sleep_us": max(pacing_sleep_us, default=None),
                "max_sleep_ms": (
                    max(pacing_sleep_us) / 1000.0 if pacing_sleep_us else None
                ),
            },
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
    parser.add_argument("--screenshot-output", type=Path, default=DEFAULT_SCREENSHOT)
    parser.add_argument(
        "--no-automatic-screenshot",
        action="store_true",
        help="Disable the automatic first-frame BMP; F12 captures remain enabled.",
    )
    parser.add_argument(
        "--hotkey-screenshot-directory",
        type=Path,
        default=DEFAULT_HOTKEY_SCREENSHOT_DIR,
        help="Directory for timestamped BMP captures created with F12.",
    )
    parser.add_argument("--clangxx", type=Path)
    parser.add_argument("--vulkan-sdk", type=Path)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument(
        "--max-frames",
        type=int,
        default=3,
        help="Frames to present; 0 runs until the window is closed or Escape is pressed.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=30,
        help="Host-process timeout; 0 disables the timeout for manual runs.",
    )
    parser.add_argument(
        "--render-stream-json",
        type=Path,
        help="Optional normalized recovered D3D stream JSON to replay.",
    )
    parser.add_argument(
        "--live-render-stream",
        action="store_true",
        help="Hot-reload --render-stream-json snapshots published by the resumable runner.",
    )
    parser.add_argument(
        "--analyze-render-stream",
        action="store_true",
        help="Interpret and diagnose a frozen live render manifest without Vulkan or a window.",
    )
    parser.add_argument(
        "--controller-state-json",
        type=Path,
        help="Publish keyboard-mapped Xbox controller state for the resumable runner.",
    )
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--no-inject-input", action="store_true")
    parser.add_argument(
        "--strict-render-validation",
        action="store_true",
        help="Fail when a presented textured draw lacks a supported matching resource.",
    )
    parser.add_argument("--flip-audit-ack", type=Path)
    parser.add_argument("--flip-audit-frame-directory", type=Path)
    parser.add_argument("--flip-audit-max-flips", type=int, default=0)
    parser.add_argument(
        "--flip-audit-health-interval",
        type=int,
        default=30,
        help=(
            "Read back and analyze every Nth audited flip in addition to "
            "candidate-triggered checks; 0 uses candidates only."
        ),
    )
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    if args.live_render_stream and args.render_stream_json is None:
        parser.error("--live-render-stream requires --render-stream-json")
    if args.analyze_render_stream and args.render_stream_json is None:
        parser.error("--analyze-render-stream requires --render-stream-json")
    if (args.flip_audit_ack is None) != (args.flip_audit_frame_directory is None):
        parser.error(
            "--flip-audit-ack and --flip-audit-frame-directory must be used together"
        )
    if args.flip_audit_max_flips < 0:
        parser.error("--flip-audit-max-flips must not be negative")
    if args.flip_audit_health_interval < 0:
        parser.error("--flip-audit-health-interval must not be negative")

    compile_result = None
    shader_result = None
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
        if not args.analyze_render_stream:
            shader_result = compile_shaders(toolchain=toolchain)
            compile_result["shaders"] = shader_result

    run_result = run_first_frame(
        executable=args.exe,
        debug_json=args.debug_json,
        width=args.width,
        height=args.height,
        max_frames=args.max_frames,
        inject_input=not args.no_inject_input and not args.analyze_render_stream,
        timeout_seconds=args.timeout_seconds,
        render_stream_json=args.render_stream_json,
        live_render_stream=args.live_render_stream,
        controller_state_json=args.controller_state_json,
        strict_render_validation=args.strict_render_validation,
        flip_audit_ack=args.flip_audit_ack,
        flip_audit_frame_directory=args.flip_audit_frame_directory,
        flip_audit_max_flips=args.flip_audit_max_flips,
        flip_audit_health_interval=args.flip_audit_health_interval,
        screenshot=(
            None
            if args.no_automatic_screenshot or args.analyze_render_stream
            else args.screenshot_output
        ),
        hotkey_screenshot_directory=(
            None if args.analyze_render_stream else args.hotkey_screenshot_directory
        ),
        analyze_render_stream=args.analyze_render_stream,
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
