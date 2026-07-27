#!/usr/bin/env python3
"""Build and run the Windows/Vulkan first interactive frame smoke test."""

from __future__ import annotations

import argparse
import _ctypes
import ctypes
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.project_identity import (
    file_identity,
    git_identity,
    sha256_bytes,
    utc_now,
    write_json_atomic,
)
from tools.native_toolchain import toolchain_validation_errors, validate_native_toolchain


DEFAULT_SOURCE = REPO_ROOT / "runtime" / "host" / "vulkan_first_frame.cpp"
DEFAULT_PRESENTER_MODULES = (
    REPO_ROOT / "runtime" / "host" / "nv2a_command_processor.cpp",
    REPO_ROOT / "runtime" / "host" / "vulkan_presenter.cpp",
    REPO_ROOT / "runtime" / "host" / "vulkan_renderer.cpp",
    REPO_ROOT / "runtime" / "host" / "vulkan_resources.cpp",
    REPO_ROOT / "runtime" / "host" / "presenter_diagnostics.cpp",
    REPO_ROOT / "runtime" / "host" / "live_presenter_transport.cpp",
    REPO_ROOT / "runtime" / "host" / "presenter_debug_log.cpp",
    REPO_ROOT / "runtime" / "host" / "presenter_metrics.cpp",
    REPO_ROOT / "runtime" / "host" / "presenter_options.cpp",
    REPO_ROOT / "runtime" / "platform" / "sdl" / "sdl_audio.cpp",
    REPO_ROOT / "runtime" / "platform" / "sdl" / "sdl_audio_c_api.cpp",
    REPO_ROOT / "runtime" / "platform" / "sdl" / "sdl_platform.cpp",
)
DEFAULT_PRESENTER_HEADERS = (
    REPO_ROOT / "runtime" / "host" / "dirty_ranges.h",
    REPO_ROOT / "runtime" / "host" / "frame_metrics.h",
    REPO_ROOT / "runtime" / "host" / "live_presenter_transport.h",
    REPO_ROOT / "runtime" / "host" / "live_transport_layout.h",
    REPO_ROOT / "runtime" / "host" / "native_pipeline_state.h",
    REPO_ROOT / "runtime" / "host" / "nv2a_vertex_program.h",
    REPO_ROOT / "runtime" / "host" / "presenter_debug_log.h",
    REPO_ROOT / "runtime" / "host" / "presenter_metrics.h",
    REPO_ROOT / "runtime" / "host" / "presenter_options.h",
    REPO_ROOT / "runtime" / "host" / "vulkan_presenter.h",
    REPO_ROOT / "runtime" / "host" / "vulkan_presenter_internal.h",
    REPO_ROOT / "runtime" / "host" / "vulkan_presenter_runtime.h",
    REPO_ROOT / "runtime" / "nv2a" / "texture_layout.h",
    REPO_ROOT / "runtime" / "platform" / "sdl" / "sdl_audio.h",
    REPO_ROOT / "runtime" / "platform" / "sdl" / "sdl_audio_c_api.h",
    REPO_ROOT / "runtime" / "platform" / "sdl" / "sdl_platform.h",
)
DEFAULT_BUILD_DIR = REPO_ROOT / "build" / "local" / "first-frame"
DEFAULT_EXE = DEFAULT_BUILD_DIR / "b2_first_frame.exe"
DEFAULT_DLL = DEFAULT_BUILD_DIR / "b2_presenter.dll"
DEFAULT_DEBUG_JSON = REPO_ROOT / "reports" / "local" / "first-frame" / "events.jsonl"
DEFAULT_SUMMARY_JSON = REPO_ROOT / "reports" / "local" / "first-frame" / "summary.json"
DEFAULT_SCREENSHOT = REPO_ROOT / "reports" / "local" / "first-frame" / "frame.bmp"
DEFAULT_HOTKEY_SCREENSHOT_DIR = REPO_ROOT / "reports" / "local" / "screenshots"
DEFAULT_METRICS_REPORT_DIR = REPO_ROOT / "reports" / "local"
DEFAULT_RENDER_STREAM_JSON = REPO_ROOT / "reports" / "local" / "render" / "recovered-d3d-stream.json"
DEFAULT_VERTEX_SHADER = REPO_ROOT / "runtime" / "host" / "shaders" / "nv2a_inline.vert"
DEFAULT_FRAGMENT_SHADER = REPO_ROOT / "runtime" / "host" / "shaders" / "nv2a_inline.frag"
DEFAULT_TEXTURE_CONVERT_SHADER = (
    REPO_ROOT / "runtime" / "host" / "shaders" / "nv2a_texture_convert.comp"
)
DEFAULT_VERTEX_SPV = DEFAULT_BUILD_DIR / "nv2a_inline.vert.spv"
DEFAULT_FRAGMENT_SPV = DEFAULT_BUILD_DIR / "nv2a_inline.frag.spv"
DEFAULT_TEXTURE_CONVERT_SPV = DEFAULT_BUILD_DIR / "nv2a_texture_convert.comp.spv"
DEFAULT_PIPELINE_CACHE = DEFAULT_BUILD_DIR / "vulkan-pipeline-cache.bin"
DEFAULT_BUILD_MANIFEST = DEFAULT_EXE.with_suffix(".build.json")
DEFAULT_VULKAN_SDK = Path("C:/VulkanSDK/1.4.341.1")
DEFAULT_LLVM_BIN = Path("C:/Program Files/LLVM/bin")
BUILD_MANIFEST_SCHEMA_VERSION = 2


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

    @property
    def sdl3_lib(self) -> Path:
        return self.lib_dir / "SDL3.lib"

    @property
    def sdl3_dll(self) -> Path:
        return self.vulkan_sdk / "Bin" / "SDL3.dll"


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
    if not (sdk_candidate / "Include" / "SDL3" / "SDL.h").exists():
        raise FirstFrameSmokeError(f"SDL3 headers not found under {sdk_candidate}")
    if not (sdk_candidate / "Lib" / "SDL3.lib").exists():
        raise FirstFrameSmokeError(f"SDL3 import library not found under {sdk_candidate}")
    if not (sdk_candidate / "Bin" / "SDL3.dll").exists():
        raise FirstFrameSmokeError(f"SDL3 runtime not found under {sdk_candidate}")
    validation = validate_native_toolchain(
        clangxx=clang_candidate,
        vulkan_sdk=sdk_candidate,
        include_build_tools=False,
        include_presenter_tools=True,
    )
    if not validation["passed"]:
        errors = toolchain_validation_errors(validation)
        raise FirstFrameSmokeError("native toolchain lock rejected: " + "; ".join(errors))
    return Toolchain(clang_candidate, sdk_candidate)


def build_command(
    *,
    source: Path,
    output: Path,
    toolchain: Toolchain,
    shared_library: bool = False,
) -> list[str]:
    command = [
        str(toolchain.clangxx),
        "-std=c++17",
        "-O2",
        "-DUNICODE",
        "-D_UNICODE",
    ]
    if shared_library:
        command.append("-shared")
    sources = [source]
    if source.resolve() == DEFAULT_SOURCE.resolve():
        sources.extend(DEFAULT_PRESENTER_MODULES)
    command.extend([
        f"-I{toolchain.include_dir}",
        *(str(path) for path in sources),
        f"-L{toolchain.lib_dir}",
        "-lvulkan-1",
        "-lSDL3",
        "-luser32",
        "-lgdi32",
        "-lshell32",
        "-o",
        str(output),
    ])
    return command


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
    sdl3_runtime = output.parent / "SDL3.dll"
    shutil.copy2(active_toolchain.sdl3_dll, sdl3_runtime)
    return {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "output": str(output),
        "sdl3_runtime": str(sdl3_runtime),
    }


def compile_presenter_library(
    *,
    source: Path = DEFAULT_SOURCE,
    output: Path = DEFAULT_DLL,
    toolchain: Toolchain | None = None,
) -> dict[str, Any]:
    active_toolchain = toolchain or discover_toolchain()
    output.parent.mkdir(parents=True, exist_ok=True)
    command = build_command(
        source=source,
        output=output,
        toolchain=active_toolchain,
        shared_library=True,
    )
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise FirstFrameSmokeError(
            "embedded presenter compile failed\n"
            f"command: {' '.join(command)}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    sdl3_runtime = output.parent / "SDL3.dll"
    shutil.copy2(active_toolchain.sdl3_dll, sdl3_runtime)
    return {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "output": str(output),
        "sdl3_runtime": str(sdl3_runtime),
    }


def compile_shaders(
    *,
    toolchain: Toolchain,
    vertex_source: Path = DEFAULT_VERTEX_SHADER,
    fragment_source: Path = DEFAULT_FRAGMENT_SHADER,
    texture_convert_source: Path = DEFAULT_TEXTURE_CONVERT_SHADER,
    vertex_output: Path = DEFAULT_VERTEX_SPV,
    fragment_output: Path = DEFAULT_FRAGMENT_SPV,
    texture_convert_output: Path = DEFAULT_TEXTURE_CONVERT_SPV,
) -> dict[str, Any]:
    started = time.perf_counter()
    glslc = toolchain.vulkan_sdk / "Bin" / "glslc.exe"
    if not glslc.exists():
        raise FirstFrameSmokeError(f"glslc not found: {glslc}")
    shader_jobs: list[list[str]] = []
    cached_outputs: list[str] = []
    compiler_mtime_ns = glslc.stat().st_mtime_ns
    for source, output in (
        (vertex_source, vertex_output),
        (fragment_source, fragment_output),
        (texture_convert_source, texture_convert_output),
    ):
        if not source.is_file():
            raise FirstFrameSmokeError(f"shader source is missing: {source}")
        output.parent.mkdir(parents=True, exist_ok=True)
        command = [str(glslc), str(source), "-o", str(output)]
        output_stat = output.stat() if output.is_file() else None
        if (
            output_stat is not None
            and output_stat.st_size >= 4
            and output_stat.st_mtime_ns
            >= max(source.stat().st_mtime_ns, compiler_mtime_ns)
            and output.read_bytes()[:4] == struct.pack("<I", 0x07230203)
        ):
            cached_outputs.append(str(output))
            continue
        shader_jobs.append(command)

    def compile_shader(
        command: list[str],
    ) -> tuple[list[str], subprocess.CompletedProcess[str]]:
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
        return command, completed

    # Each module is independent. Cold builds let glslc compile them in
    # parallel, while warm builds avoid launching the compiler altogether.
    if shader_jobs:
        with ThreadPoolExecutor(max_workers=len(shader_jobs)) as executor:
            completed_jobs = list(executor.map(compile_shader, shader_jobs))
    else:
        completed_jobs = []
    commands = [command for command, _completed in completed_jobs]
    return {
        "returncode": 0,
        "commands": commands,
        "compiled_count": len(commands),
        "cache_hit_count": len(cached_outputs),
        "cached_outputs": cached_outputs,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "vertex_output": str(vertex_output),
        "fragment_output": str(fragment_output),
        "texture_convert_output": str(texture_convert_output),
    }


def _command_version(executable: Path) -> str | None:
    completed = subprocess.run(
        [str(executable), "--version"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    output = completed.stdout.strip() or completed.stderr.strip()
    return output or None


def _build_manifest_id(basis: dict[str, Any]) -> str:
    encoded = json.dumps(basis, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(encoded)


def write_presenter_build_manifest(
    *,
    path: Path,
    source: Path,
    output: Path,
    library: Path | None = None,
    toolchain: Toolchain,
    include_shaders: bool,
    vertex_source: Path = DEFAULT_VERTEX_SHADER,
    fragment_source: Path = DEFAULT_FRAGMENT_SHADER,
    texture_convert_source: Path = DEFAULT_TEXTURE_CONVERT_SHADER,
    vertex_output: Path = DEFAULT_VERTEX_SPV,
    fragment_output: Path = DEFAULT_FRAGMENT_SPV,
    texture_convert_output: Path = DEFAULT_TEXTURE_CONVERT_SPV,
) -> dict[str, Any]:
    glslc = toolchain.vulkan_sdk / "Bin" / "glslc.exe"
    inputs = {
        "build_driver": file_identity(Path(__file__)),
        "presenter_source": file_identity(source),
        "clangxx": {
            **file_identity(toolchain.clangxx),
            "version": _command_version(toolchain.clangxx),
        },
        "sdl3_import_library": file_identity(toolchain.sdl3_lib),
        "sdl3_runtime_source": file_identity(toolchain.sdl3_dll),
        "vulkan_import_library": file_identity(toolchain.vulkan_lib),
    }
    if source.resolve() == DEFAULT_SOURCE.resolve():
        inputs.update(
            {
                f"presenter_input_{index:02d}_{path.stem}": file_identity(path)
                for index, path in enumerate(
                    (*DEFAULT_PRESENTER_MODULES, *DEFAULT_PRESENTER_HEADERS)
                )
            }
        )
    artifacts = {
        "presenter_executable": file_identity(output),
        "sdl3_runtime": file_identity(output.parent / "SDL3.dll"),
    }
    if library is not None:
        artifacts["presenter_library"] = file_identity(library)
    if include_shaders:
        inputs.update(
            {
                "glslc": {
                    **file_identity(glslc),
                    "version": _command_version(glslc),
                },
                "vertex_shader_source": file_identity(vertex_source),
                "fragment_shader_source": file_identity(fragment_source),
                "texture_convert_shader_source": file_identity(texture_convert_source),
            }
        )
        artifacts.update(
            {
                "vertex_shader": file_identity(vertex_output),
                "fragment_shader": file_identity(fragment_output),
                "texture_convert_shader": file_identity(texture_convert_output),
            }
        )
    basis = {
        "schema_version": BUILD_MANIFEST_SCHEMA_VERSION,
        "profile": "full" if include_shaders else "analysis_only",
        "build_command": build_command(
            source=source,
            output=output,
            toolchain=toolchain,
        ),
        "library_build_command": (
            build_command(
                source=source,
                output=library,
                toolchain=toolchain,
                shared_library=True,
            )
            if library is not None
            else None
        ),
        "shader_commands": (
            [
                [str(glslc), str(shader_source), "-o", str(shader_output)]
                for shader_source, shader_output in (
                    (vertex_source, vertex_output),
                    (fragment_source, fragment_output),
                    (texture_convert_source, texture_convert_output),
                )
            ]
            if include_shaders
            else []
        ),
        "inputs": inputs,
    }
    manifest = {
        "format": "b2-recomp-presenter-build-manifest",
        "schema_version": BUILD_MANIFEST_SCHEMA_VERSION,
        "built_utc": utc_now(),
        "build_id": _build_manifest_id(basis),
        "profile": basis["profile"],
        "repository": git_identity(REPO_ROOT),
        "build_command": basis["build_command"],
        "library_build_command": basis["library_build_command"],
        "shader_commands": basis["shader_commands"],
        "inputs": inputs,
        "artifacts": artifacts,
    }
    write_json_atomic(path, manifest)
    return manifest


def validate_presenter_build_manifest(
    path: Path,
    *,
    executable: Path = DEFAULT_EXE,
    library: Path | None = None,
    source: Path = DEFAULT_SOURCE,
    require_shaders: bool = True,
    allow_stale: bool = False,
) -> dict[str, Any]:
    problems: list[str] = []
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        manifest = None
        problems.append(f"cannot read build manifest {path}: {exc}")
    if manifest is not None and not isinstance(manifest, dict):
        problems.append("build manifest root must be an object")
        manifest = None
    if manifest is not None:
        if manifest.get("schema_version") != BUILD_MANIFEST_SCHEMA_VERSION:
            problems.append(
                f"build manifest schema is {manifest.get('schema_version')!r}; "
                f"expected {BUILD_MANIFEST_SCHEMA_VERSION}"
            )
        if require_shaders and manifest.get("profile") != "full":
            problems.append("build manifest is analysis-only; a full presenter build is required")
        inputs = manifest.get("inputs")
        artifacts = manifest.get("artifacts")
        if not isinstance(inputs, dict) or not isinstance(artifacts, dict):
            problems.append("build manifest inputs/artifacts are malformed")
            inputs = {}
            artifacts = {}
        expected_source = inputs.get("presenter_source", {}).get("path")
        if expected_source != str(source.resolve()):
            problems.append(
                f"presenter source path is {expected_source!r}; expected {str(source.resolve())!r}"
            )
        expected_executable = artifacts.get("presenter_executable", {}).get("path")
        if expected_executable != str(executable.resolve()):
            problems.append(
                "presenter executable path is "
                f"{expected_executable!r}; expected {str(executable.resolve())!r}"
            )
        if library is not None:
            expected_library = artifacts.get("presenter_library", {}).get("path")
            if expected_library != str(library.resolve()):
                problems.append(
                    "presenter library path is "
                    f"{expected_library!r}; expected {str(library.resolve())!r}"
                )
        for group_name, records in (("input", inputs), ("artifact", artifacts)):
            for role, recorded in records.items():
                if not isinstance(recorded, dict) or not recorded.get("path"):
                    problems.append(f"{group_name} {role} has no recorded path")
                    continue
                current = file_identity(Path(recorded["path"]))
                if not current["exists"]:
                    problems.append(f"{group_name} {role} is missing: {current['path']}")
                    continue
                if current["size"] != recorded.get("size"):
                    problems.append(f"{group_name} {role} size changed")
                if current["sha256"] != recorded.get("sha256"):
                    problems.append(f"{group_name} {role} SHA-256 changed")
        basis = {
            "schema_version": manifest.get("schema_version"),
            "profile": manifest.get("profile"),
            "build_command": manifest.get("build_command"),
            "library_build_command": manifest.get("library_build_command"),
            "shader_commands": manifest.get("shader_commands", []),
            "inputs": inputs,
        }
        if manifest.get("build_id") != _build_manifest_id(basis):
            problems.append("build manifest ID does not match its recorded inputs")
    result = {
        "status": (
            "valid" if not problems else "stale_override" if allow_stale else "stale"
        ),
        "valid": not problems,
        "override_used": bool(problems and allow_stale),
        "manifest": str(path.resolve()),
        "build_id": manifest.get("build_id") if manifest else None,
        "profile": manifest.get("profile") if manifest else None,
        "problems": problems,
    }
    if problems and not allow_stale:
        raise FirstFrameSmokeError(
            "stale or unidentified presenter build rejected: " + "; ".join(problems)
        )
    return result


def build_presenter_arguments(
    *,
    program: Path = DEFAULT_EXE,
    debug_json: Path | None = DEFAULT_DEBUG_JSON,
    width: int = 640,
    height: int = 480,
    max_frames: int = 3,
    inject_input: bool = True,
    render_stream_json: Path | None = None,
    screenshot: Path | None = None,
    hotkey_screenshot_directory: Path | None = DEFAULT_HOTKEY_SCREENSHOT_DIR,
    metrics_report_directory: Path | None = DEFAULT_METRICS_REPORT_DIR,
    vertex_shader: Path = DEFAULT_VERTEX_SPV,
    fragment_shader: Path = DEFAULT_FRAGMENT_SPV,
    texture_convert_shader: Path = DEFAULT_TEXTURE_CONVERT_SPV,
    pipeline_cache: Path | None = DEFAULT_PIPELINE_CACHE,
    live_render_stream: bool = False,
    controller_state_json: Path | None = None,
    live_control_transport: str | None = None,
    strict_render_validation: bool = False,
    flip_audit_ack: Path | None = None,
    flip_audit_frame_directory: Path | None = None,
    flip_audit_max_flips: int = 0,
    flip_audit_health_interval: int = 30,
    analyze_render_stream: bool = False,
    cpu_vertex_programs: bool = False,
    cpu_vertex_attributes: bool = False,
    cpu_texture_conversion: bool = False,
    presentation_pipeline_depth: int = 1,
) -> list[str]:
    if debug_json is not None:
        debug_json.parent.mkdir(parents=True, exist_ok=True)
        if debug_json.exists():
            debug_json.unlink()
    command = [
        str(program),
        "--width",
        str(width),
        "--height",
        str(height),
        "--max-frames",
        str(max_frames),
    ]
    if debug_json is not None:
        command.extend(["--debug-json", str(debug_json)])
    if analyze_render_stream:
        command.append("--analyze-render-stream")
    if cpu_vertex_programs:
        command.append("--cpu-vertex-programs")
    if cpu_vertex_attributes:
        command.append("--cpu-vertex-attributes")
    if cpu_texture_conversion:
        command.append("--cpu-texture-conversion")
    if presentation_pipeline_depth not in (1, 2):
        raise FirstFrameSmokeError("presentation pipeline depth must be 1 or 2")
    if presentation_pipeline_depth == 2:
        if not live_render_stream:
            raise FirstFrameSmokeError(
                "presentation pipeline depth 2 requires a live render stream"
            )
        command.extend(
            ["--presentation-pipeline-depth", str(presentation_pipeline_depth)]
        )
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
    if live_control_transport is not None and not analyze_render_stream:
        command.extend(["--live-control-transport", live_control_transport])
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
    if metrics_report_directory is not None and not analyze_render_stream:
        metrics_report_directory.mkdir(parents=True, exist_ok=True)
        command.extend([
            "--metrics-report-directory",
            str(metrics_report_directory),
        ])
    if not analyze_render_stream:
        if pipeline_cache is not None:
            pipeline_cache.parent.mkdir(parents=True, exist_ok=True)
            command.extend(["--pipeline-cache", str(pipeline_cache)])
        command.extend(["--vertex-shader", str(vertex_shader)])
        command.extend(["--fragment-shader", str(fragment_shader)])
        if not cpu_texture_conversion:
            command.extend(
                ["--texture-convert-shader", str(texture_convert_shader)]
            )
    return command


def run_first_frame(
    *,
    executable: Path = DEFAULT_EXE,
    debug_json: Path | None = DEFAULT_DEBUG_JSON,
    width: int = 640,
    height: int = 480,
    max_frames: int = 3,
    inject_input: bool = True,
    timeout_seconds: int = 30,
    render_stream_json: Path | None = None,
    screenshot: Path | None = None,
    hotkey_screenshot_directory: Path | None = DEFAULT_HOTKEY_SCREENSHOT_DIR,
    metrics_report_directory: Path | None = DEFAULT_METRICS_REPORT_DIR,
    vertex_shader: Path = DEFAULT_VERTEX_SPV,
    fragment_shader: Path = DEFAULT_FRAGMENT_SPV,
    texture_convert_shader: Path = DEFAULT_TEXTURE_CONVERT_SPV,
    pipeline_cache: Path | None = DEFAULT_PIPELINE_CACHE,
    live_render_stream: bool = False,
    controller_state_json: Path | None = None,
    live_control_transport: str | None = None,
    strict_render_validation: bool = False,
    flip_audit_ack: Path | None = None,
    flip_audit_frame_directory: Path | None = None,
    flip_audit_max_flips: int = 0,
    flip_audit_health_interval: int = 30,
    analyze_render_stream: bool = False,
    cpu_vertex_programs: bool = False,
    cpu_vertex_attributes: bool = False,
    cpu_texture_conversion: bool = False,
    presentation_pipeline_depth: int = 1,
) -> dict[str, Any]:
    if not executable.exists():
        raise FirstFrameSmokeError(f"first-frame executable is missing: {executable}")
    command = build_presenter_arguments(
        program=executable,
        debug_json=debug_json,
        width=width,
        height=height,
        max_frames=max_frames,
        inject_input=inject_input,
        render_stream_json=render_stream_json,
        screenshot=screenshot,
        hotkey_screenshot_directory=hotkey_screenshot_directory,
        metrics_report_directory=metrics_report_directory,
        vertex_shader=vertex_shader,
        fragment_shader=fragment_shader,
        texture_convert_shader=texture_convert_shader,
        pipeline_cache=pipeline_cache,
        live_render_stream=live_render_stream,
        controller_state_json=controller_state_json,
        live_control_transport=live_control_transport,
        strict_render_validation=strict_render_validation,
        flip_audit_ack=flip_audit_ack,
        flip_audit_frame_directory=flip_audit_frame_directory,
        flip_audit_max_flips=flip_audit_max_flips,
        flip_audit_health_interval=flip_audit_health_interval,
        analyze_render_stream=analyze_render_stream,
        cpu_vertex_programs=cpu_vertex_programs,
        cpu_vertex_attributes=cpu_vertex_attributes,
        cpu_texture_conversion=cpu_texture_conversion,
        presentation_pipeline_depth=presentation_pipeline_depth,
    )

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
    events = read_debug_events(debug_json) if debug_json is not None else []
    return {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "debug_json": str(debug_json) if debug_json is not None else None,
        "screenshot": str(screenshot) if screenshot is not None else None,
        "hotkey_screenshot_directory": (
            str(hotkey_screenshot_directory)
            if hotkey_screenshot_directory is not None
            else None
        ),
        "metrics_report_directory": (
            str(metrics_report_directory)
            if metrics_report_directory is not None
            else None
        ),
        "analyze_render_stream": analyze_render_stream,
        "events": events,
    }


def run_embedded_presenter(
    arguments: list[str],
    *,
    library: Path = DEFAULT_DLL,
) -> int:
    """Run the presenter DLL on the calling thread for the shipping live path."""
    if not library.is_file():
        raise FirstFrameSmokeError(f"embedded presenter library is missing: {library}")
    if not arguments:
        arguments = [str(library)]
    dll_directory = (
        os.add_dll_directory(str(library.parent.resolve()))
        if os.name == "nt"
        else None
    )
    presenter: ctypes.CDLL | None = None
    entry = None
    try:
        presenter = ctypes.CDLL(str(library.resolve()))
        entry = presenter.b2r_presenter_main
        entry.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_wchar_p),
        ]
        entry.restype = ctypes.c_int
        argv = (ctypes.c_wchar_p * len(arguments))(*arguments)
        return int(entry(len(arguments), argv))
    finally:
        entry = None
        if presenter is not None:
            handle = presenter._handle
            presenter = None
            if os.name == "nt":
                # Leaving SDL and the presenter loaded until ExitProcess runs
                # their detach handlers under the loader lock can deadlock.
                # Unload while Python is still fully operational instead.
                _ctypes.FreeLibrary(handle)
            else:
                _ctypes.dlclose(handle)
        if dll_directory is not None:
            dll_directory.close()


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
    build_validation: dict[str, Any] | None = None,
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
    texture_refresh_events = [
        event
        for event in events
        if event.get("event") == "nv2a_texture_resources_refreshed"
    ]
    texture_conversion_batches = [
        event
        for event in events
        if event.get("event") == "nv2a_gpu_texture_conversion_batch"
    ]
    texture_conversion_validations = [
        event
        for event in events
        if event.get("event") == "nv2a_gpu_texture_conversion_validation"
    ]
    stream_analysis_events = [
        event
        for event in events
        if event.get("event") == "render_stream_analysis_complete"
    ]
    pipeline_cache_open_events = [
        event
        for event in events
        if event.get("event") == "vulkan_pipeline_cache_opened"
    ]
    pipeline_cache_save_events = [
        event
        for event in events
        if event.get("event") == "vulkan_pipeline_cache_saved"
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
            "identity": build_validation,
        },
        "run": {
            "returncode": run_result["returncode"],
            "stderr": run_result.get("stderr") or None,
            "debug_json": run_result["debug_json"],
            "event_counts": dict(sorted(counts.items())),
            "main_loop_entered": counts.get("main_loop_enter", 0) == 1,
            "main_loop_exited": counts.get("main_loop_exit", 0) == 1,
            "render_stream_analysis_completed": len(stream_analysis_events) == 1,
            "pipeline_cache": {
                "path": (
                    pipeline_cache_open_events[-1].get("path")
                    if pipeline_cache_open_events
                    else None
                ),
                "loaded_bytes": (
                    int(pipeline_cache_open_events[-1].get("loaded_bytes", 0))
                    if pipeline_cache_open_events
                    else 0
                ),
                "initial_data_rejected": (
                    pipeline_cache_open_events[-1].get(
                        "initial_data_rejected",
                        False,
                    )
                    if pipeline_cache_open_events
                    else False
                ),
                "saved_bytes": (
                    int(pipeline_cache_save_events[-1].get("bytes", 0))
                    if pipeline_cache_save_events
                    else 0
                ),
                "written": (
                    pipeline_cache_save_events[-1].get("written", False)
                    if pipeline_cache_save_events
                    else False
                ),
            },
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
            "texture_conversion": {
                "backend": (
                    texture_refresh_events[-1].get("gpu_conversion_backend")
                    if texture_refresh_events
                    else None
                ),
                "refresh_count": len(texture_refresh_events),
                "batch_count": len(texture_conversion_batches),
                "gpu_converted_textures": sum(
                    int(event.get("gpu_converted", 0))
                    for event in texture_refresh_events
                ),
                "cpu_converted_textures": sum(
                    int(event.get("cpu_converted", 0))
                    for event in texture_refresh_events
                ),
                "dxt1_textures": sum(
                    int(event.get("dxt1_textures", 0))
                    for event in texture_conversion_batches
                ),
                "dxt5_textures": sum(
                    int(event.get("dxt5_textures", 0))
                    for event in texture_conversion_batches
                ),
                "mips": sum(
                    int(event.get("mips", 0))
                    for event in texture_conversion_batches
                ),
                "generated_mips": sum(
                    int(event.get("generated_mips", 0))
                    for event in texture_conversion_batches
                ),
                "compressed_input_bytes": sum(
                    int(event.get("input_bytes", 0))
                    for event in texture_conversion_batches
                ),
                "rgba_output_bytes": sum(
                    int(event.get("output_bytes", 0))
                    for event in texture_conversion_batches
                ),
                "conversion_us": sum(
                    int(event.get("conversion_us", 0))
                    for event in texture_conversion_batches
                ),
                "gpu_submission_us": sum(
                    int(event.get("gpu_submission_us", 0))
                    for event in texture_conversion_batches
                ),
                "validation_passed": (
                    all(
                        event.get("passed") in (True, "true", 1, "1")
                        for event in texture_conversion_validations
                    )
                    if texture_conversion_validations
                    else None
                ),
                "validation_coverage_complete": (
                    texture_conversion_validations[-1].get(
                        "coverage_complete"
                    )
                    if texture_conversion_validations
                    else None
                ),
                "validation_mismatch_bytes": sum(
                    int(event.get("mismatch_bytes", 0))
                    for event in texture_conversion_validations
                ),
            },
            "render_target_feedback_image_cache": {
                "hit_count": sum(
                    int(
                        event.get(
                            "render_target_feedback_image_cache_hits",
                            0,
                        )
                    )
                    for event in texture_refresh_events
                ),
                "miss_count": sum(
                    int(
                        event.get(
                            "render_target_feedback_image_cache_misses",
                            0,
                        )
                    )
                    for event in texture_refresh_events
                ),
                "store_count": sum(
                    int(
                        event.get(
                            "render_target_feedback_image_cache_stores",
                            0,
                        )
                    )
                    for event in texture_refresh_events
                ),
                "eviction_count": sum(
                    int(
                        event.get(
                            "render_target_feedback_image_cache_evictions",
                            0,
                        )
                    )
                    for event in texture_refresh_events
                ),
                "resident_count": (
                    int(
                        texture_refresh_events[-1].get(
                            "render_target_feedback_image_cache_resident",
                            0,
                        )
                    )
                    if texture_refresh_events
                    else 0
                ),
                "capacity": (
                    int(
                        texture_refresh_events[-1].get(
                            "render_target_feedback_image_cache_capacity",
                            0,
                        )
                    )
                    if texture_refresh_events
                    else 0
                ),
            },
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
    parser.add_argument("--dll", type=Path, default=DEFAULT_DLL)
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
    parser.add_argument(
        "--metrics-report-directory",
        type=Path,
        default=DEFAULT_METRICS_REPORT_DIR,
        help="Directory for timestamped text metric snapshots created with F11.",
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
        "--cpu-vertex-programs",
        action="store_true",
        help="Use the exact CPU vertex-program interpreter for A/B validation.",
    )
    parser.add_argument(
        "--cpu-vertex-attributes",
        action="store_true",
        help="Keep CPU indexed-attribute decoding while using GPU vertex programs.",
    )
    parser.add_argument(
        "--cpu-texture-conversion",
        action="store_true",
        help="Use the exact CPU texture converter for visual/performance A/B runs.",
    )
    parser.add_argument(
        "--presentation-pipeline-depth",
        type=int,
        choices=(1, 2),
        default=1,
        help=(
            "Presenter acknowledgement pipeline depth; 2 releases the guest "
            "after immutable command/resource loading for an opt-in A/B run."
        ),
    )
    parser.add_argument(
        "--pipeline-cache",
        type=Path,
        default=DEFAULT_PIPELINE_CACHE,
        help="Persistent Vulkan driver pipeline cache.",
    )
    parser.add_argument(
        "--controller-state-json",
        type=Path,
        help="Publish keyboard-mapped Xbox controller state for the resumable runner.",
    )
    parser.add_argument(
        "--live-control-transport",
        help=(
            "Named versioned shared-memory mapping for live controller state "
            "and presentation acknowledgements."
        ),
    )
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument(
        "--build-manifest",
        type=Path,
        help="Presenter build identity manifest; defaults beside --exe.",
    )
    parser.add_argument(
        "--allow-stale-artifacts",
        action="store_true",
        help=(
            "Developer-only override for missing or stale build identity; marks "
            "the run as unsupported."
        ),
    )
    parser.add_argument(
        "--no-diagnostics",
        action="store_true",
        help=(
            "Disable presenter JSONL/summary output and automatic or hotkey "
            "screenshots."
        ),
    )
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
    build_manifest_path = args.build_manifest or args.exe.with_suffix(".build.json")

    compile_result = None
    shader_result = None
    build_validation = None
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
            compile_result["embedded_presenter"] = compile_presenter_library(
                source=args.source,
                output=args.dll,
                toolchain=toolchain,
            )
            shader_result = compile_shaders(toolchain=toolchain)
            compile_result["shaders"] = shader_result
        write_presenter_build_manifest(
            path=build_manifest_path,
            source=args.source,
            output=args.exe,
            library=None if args.analyze_render_stream else args.dll,
            toolchain=toolchain,
            include_shaders=not args.analyze_render_stream,
        )
        build_validation = validate_presenter_build_manifest(
            build_manifest_path,
            executable=args.exe,
            library=None if args.analyze_render_stream else args.dll,
            source=args.source,
            require_shaders=not args.analyze_render_stream,
        )
    else:
        build_validation = validate_presenter_build_manifest(
            build_manifest_path,
            executable=args.exe,
            library=None if args.analyze_render_stream else args.dll,
            source=args.source,
            require_shaders=not args.analyze_render_stream,
            allow_stale=args.allow_stale_artifacts,
        )
        if build_validation["override_used"]:
            print(
                "WARNING: stale presenter artifact override is active; this run "
                "is not valid for compatibility or performance claims.",
                file=sys.stderr,
            )

    run_result = run_first_frame(
        executable=args.exe,
        debug_json=None if args.no_diagnostics else args.debug_json,
        width=args.width,
        height=args.height,
        max_frames=args.max_frames,
        inject_input=not args.no_inject_input and not args.analyze_render_stream,
        timeout_seconds=args.timeout_seconds,
        render_stream_json=args.render_stream_json,
        live_render_stream=args.live_render_stream,
        controller_state_json=args.controller_state_json,
        live_control_transport=args.live_control_transport,
        strict_render_validation=args.strict_render_validation,
        flip_audit_ack=args.flip_audit_ack,
        flip_audit_frame_directory=args.flip_audit_frame_directory,
        flip_audit_max_flips=args.flip_audit_max_flips,
        flip_audit_health_interval=args.flip_audit_health_interval,
        screenshot=(
            None
            if args.no_diagnostics
            or args.no_automatic_screenshot
            or args.analyze_render_stream
            else args.screenshot_output
        ),
        hotkey_screenshot_directory=(
            None
            if args.no_diagnostics or args.analyze_render_stream
            else args.hotkey_screenshot_directory
        ),
        metrics_report_directory=(
            None if args.analyze_render_stream else args.metrics_report_directory
        ),
        analyze_render_stream=args.analyze_render_stream,
        cpu_vertex_programs=args.cpu_vertex_programs,
        cpu_vertex_attributes=args.cpu_vertex_attributes,
        cpu_texture_conversion=args.cpu_texture_conversion,
        presentation_pipeline_depth=args.presentation_pipeline_depth,
        pipeline_cache=args.pipeline_cache,
    )
    if args.no_diagnostics:
        return int(run_result["returncode"])
    summary = summarize_smoke(
        compile_result=compile_result,
        run_result=run_result,
        summary_output=args.summary_output,
        build_validation=build_validation,
    )
    print(json.dumps(summary, indent=2 if args.pretty else None, sort_keys=True))
    return 0 if run_result["returncode"] == 0 else run_result["returncode"]


if __name__ == "__main__":
    raise SystemExit(main())
