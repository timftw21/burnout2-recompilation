#!/usr/bin/env python3
"""Configure, build, and test native targets with the pinned CMake/Ninja tools."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
PRESETS = ("debug", "release", "profiling", "sanitizer")


class NativeBuildError(RuntimeError):
    """Raised when the pinned native toolchain cannot be resolved."""


@dataclass(frozen=True)
class NativeTools:
    cmake: Path
    ctest: Path
    binary_directories: tuple[Path, ...]


def resolve_native_tools() -> NativeTools:
    try:
        import cmake
        import ninja
    except ImportError as exc:
        raise NativeBuildError(
            "pinned CMake/Ninja tools are missing; install requirements-dev.lock"
        ) from exc

    executable_suffix = ".exe" if os.name == "nt" else ""
    cmake_directory = Path(cmake.CMAKE_BIN_DIR)
    ninja_directory = Path(ninja.BIN_DIR)
    tools = NativeTools(
        cmake=cmake_directory / f"cmake{executable_suffix}",
        ctest=cmake_directory / f"ctest{executable_suffix}",
        binary_directories=(cmake_directory, ninja_directory),
    )
    for executable in (tools.cmake, tools.ctest):
        if not executable.is_file():
            raise NativeBuildError(f"pinned native tool is missing: {executable}")
    ninja_executable = ninja_directory / f"ninja{executable_suffix}"
    if not ninja_executable.is_file():
        raise NativeBuildError(f"pinned native tool is missing: {ninja_executable}")
    return tools


def build_commands(
    tools: NativeTools,
    *,
    preset: str,
    build_presenter: bool,
    clean_first: bool,
    skip_tests: bool,
) -> list[list[str]]:
    configure = [str(tools.cmake), "--preset", preset]
    if build_presenter:
        configure.append("-DB2R_BUILD_PRESENTER=ON")
    build = [str(tools.cmake), "--build", "--preset", preset]
    if clean_first:
        build.append("--clean-first")
    commands = [configure, build]
    if not skip_tests:
        commands.append([str(tools.ctest), "--preset", preset])
    return commands


def run_native_build(
    *,
    preset: str,
    build_presenter: bool = False,
    clean_first: bool = False,
    skip_tests: bool = False,
) -> int:
    from tools.native_toolchain import toolchain_validation_errors, validate_native_toolchain

    validation = validate_native_toolchain(
        include_build_tools=True,
        include_presenter_tools=build_presenter,
    )
    if not validation["passed"]:
        errors = toolchain_validation_errors(validation)
        raise NativeBuildError("native toolchain lock rejected: " + "; ".join(errors))
    tools = resolve_native_tools()
    environment = os.environ.copy()
    pinned_path = os.pathsep.join(str(path) for path in tools.binary_directories)
    environment["PATH"] = pinned_path + os.pathsep + environment.get("PATH", "")
    for command in build_commands(
        tools,
        preset=preset,
        build_presenter=build_presenter,
        clean_first=clean_first,
        skip_tests=skip_tests,
    ):
        print(f"+ {subprocess.list2cmdline(command)}", flush=True)
        completed = subprocess.run(command, cwd=REPO_ROOT, env=environment, check=False)
        if completed.returncode != 0:
            return completed.returncode
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Configure, build, and test native targets with pinned tools."
    )
    parser.add_argument("--preset", choices=PRESETS, default="debug")
    parser.add_argument(
        "--presenter",
        action="store_true",
        help="Also build the Windows Vulkan/SDL3 executable and embedded DLL.",
    )
    parser.add_argument("--clean-first", action="store_true")
    parser.add_argument("--skip-tests", action="store_true")
    args = parser.parse_args(argv)
    try:
        return run_native_build(
            preset=args.preset,
            build_presenter=args.presenter,
            clean_first=args.clean_first,
            skip_tests=args.skip_tests,
        )
    except NativeBuildError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    sys.exit(main())
