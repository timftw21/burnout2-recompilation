#!/usr/bin/env python3
"""Configure, build, and test native targets with the pinned CMake/Ninja tools."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
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
    ninja: Path
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
        ninja=ninja_directory / f"ninja{executable_suffix}",
        binary_directories=(cmake_directory, ninja_directory),
    )
    for executable in (tools.cmake, tools.ctest):
        if not executable.is_file():
            raise NativeBuildError(f"pinned native tool is missing: {executable}")
    if not tools.ninja.is_file():
        raise NativeBuildError(f"pinned native tool is missing: {tools.ninja}")
    return tools


def _regex_union(expressions: Sequence[str]) -> str | None:
    if not expressions:
        return None
    if len(expressions) == 1:
        return expressions[0]
    return "|".join(f"({expression})" for expression in expressions)


def _compiled_source_metrics(preset: str, started_ns: int) -> tuple[int, int]:
    database = REPO_ROOT / "build" / "cmake" / preset / "compile_commands.json"
    try:
        commands = json.loads(database.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0, 0
    sources: set[Path] = set()
    for command in commands:
        if not isinstance(command, dict):
            continue
        output_value = command.get("output")
        source_value = command.get("file")
        directory_value = command.get("directory")
        if (
            not isinstance(output_value, str)
            or not isinstance(source_value, str)
            or not isinstance(directory_value, str)
        ):
            continue
        directory = Path(directory_value)
        output = Path(output_value)
        if not output.is_absolute():
            output = directory / output
        source = Path(source_value)
        if not source.is_absolute():
            source = directory / source
        try:
            if output.stat().st_mtime_ns >= started_ns:
                sources.add(source.resolve())
        except OSError:
            continue
    total = 0
    for source in sources:
        try:
            total += source.stat().st_size
        except OSError:
            continue
    return total, len(sources)


def build_commands(
    tools: NativeTools,
    *,
    preset: str,
    build_presenter: bool,
    clean_first: bool,
    skip_tests: bool,
    targets: Sequence[str] = (),
    test_regexes: Sequence[str] = (),
    test_labels: Sequence[str] = (),
    parallel: int | None = None,
    configure_only: bool = False,
    build_only: bool = False,
    compiler_launcher: Path | None = None,
) -> list[list[str]]:
    if configure_only and build_only:
        raise ValueError("configure_only and build_only are mutually exclusive")
    configure = [str(tools.cmake), "--preset", preset]
    if build_presenter:
        configure.append("-DB2R_BUILD_PRESENTER=ON")
    if compiler_launcher is not None:
        configure.append(f"-DCMAKE_CXX_COMPILER_LAUNCHER={compiler_launcher}")
    build_directory = Path("build") / "cmake" / preset
    build = [str(tools.ninja), "-C", str(build_directory)]
    if targets:
        build.extend(targets)
    if parallel is not None:
        build.extend(("-j", str(parallel)))
    commands: list[list[str]] = []
    if not build_only:
        commands.append(configure)
    if not configure_only:
        if clean_first:
            commands.append([str(tools.ninja), "-C", str(build_directory), "clean"])
        commands.append(build)
    if not configure_only and not build_only and not skip_tests:
        ctest = [str(tools.ctest), "--preset", preset]
        if parallel is not None:
            ctest.extend(("--parallel", str(parallel)))
        test_regex = _regex_union(test_regexes)
        if test_regex is not None:
            ctest.extend(("--tests-regex", test_regex))
        test_label = _regex_union(test_labels)
        if test_label is not None:
            ctest.extend(("--label-regex", test_label))
        commands.append(ctest)
    return commands


def run_native_build(
    *,
    preset: str,
    build_presenter: bool = False,
    clean_first: bool = False,
    skip_tests: bool = False,
    targets: Sequence[str] = (),
    test_regexes: Sequence[str] = (),
    test_labels: Sequence[str] = (),
    parallel: int | None = None,
    configure_only: bool = False,
    build_only: bool = False,
    compiler_cache: str = "auto",
    skip_toolchain_validation: bool = False,
) -> int:
    from tools.compiler_cache import (
        CompilerCacheError,
        cache_environment,
        resolve_compiler_cache,
        write_stats,
    )
    from tools.native_toolchain import toolchain_validation_errors, validate_native_toolchain

    if not skip_toolchain_validation:
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
    try:
        compiler_launcher = resolve_compiler_cache(compiler_cache)
    except CompilerCacheError as exc:
        raise NativeBuildError(str(exc)) from exc
    if compiler_launcher is not None:
        environment = cache_environment(environment)
        if not configure_only:
            subprocess.run(
                (str(compiler_launcher), "--zero-stats"),
                cwd=REPO_ROOT,
                env=environment,
                check=False,
            )
    result = 0
    build_started_ns = 0
    for command in build_commands(
        tools,
        preset=preset,
        build_presenter=build_presenter,
        clean_first=clean_first,
        skip_tests=skip_tests,
        targets=targets,
        test_regexes=test_regexes,
        test_labels=test_labels,
        parallel=parallel,
        configure_only=configure_only,
        build_only=build_only,
        compiler_launcher=compiler_launcher,
    ):
        print(f"+ {subprocess.list2cmdline(command)}", flush=True)
        if command and Path(command[0]) == tools.ninja and build_started_ns == 0:
            build_started_ns = time.time_ns()
        completed = subprocess.run(command, cwd=REPO_ROOT, env=environment, check=False)
        if completed.returncode != 0:
            result = completed.returncode
            break
    if compiler_launcher is not None and not configure_only:
        stats_path = REPO_ROOT / "build" / "local" / "compiler-cache" / f"native-{preset}.json"
        try:
            source_bytes, source_count = _compiled_source_metrics(preset, build_started_ns)
            stats = write_stats(
                compiler_launcher,
                environment,
                stats_path,
                source_bytes_compiled=source_bytes,
                compiled_source_count=source_count,
            )
            telemetry = stats.get("b2_recomp", {})
            print(
                "compiler-cache telemetry: "
                f"hits={telemetry.get('cache_hit_count', 0)} "
                f"misses={telemetry.get('cache_miss_count', 0)} "
                f"source_bytes={source_bytes} report={stats_path}",
                flush=True,
            )
        except (CompilerCacheError, OSError, ValueError) as exc:
            print(f"warning: compiler-cache telemetry unavailable: {exc}", file=sys.stderr)
    return result


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
    parser.add_argument(
        "--target",
        action="append",
        default=[],
        help="Build only this CMake target (repeatable).",
    )
    parser.add_argument(
        "--test-regex",
        action="append",
        default=[],
        help="Run only matching CTest names (repeatable; entries are ORed).",
    )
    parser.add_argument(
        "--label",
        "--test-label",
        dest="test_label",
        action="append",
        default=[],
        help="Run only matching CTest labels (repeatable; entries are ORed).",
    )
    parser.add_argument("--parallel", type=int, help="Parallel Ninja/CTest jobs.")
    parser.add_argument(
        "--compiler-cache",
        choices=("auto", "required", "off"),
        default="auto",
        help="Use the repository-pinned sccache launcher when available.",
    )
    phase = parser.add_mutually_exclusive_group()
    phase.add_argument(
        "--configure-only",
        action="store_true",
        help="Generate the selected build tree without building or testing.",
    )
    phase.add_argument(
        "--build-only",
        action="store_true",
        help="Run Ninja in an already-configured build tree without CMake or CTest.",
    )
    parser.add_argument(
        "--skip-toolchain-validation",
        action="store_true",
        help="Skip validation already completed by a parent validation graph.",
    )
    args = parser.parse_args(argv)
    if args.parallel is not None and args.parallel <= 0:
        parser.error("--parallel must be positive")
    try:
        return run_native_build(
            preset=args.preset,
            build_presenter=args.presenter,
            clean_first=args.clean_first,
            skip_tests=args.skip_tests,
            targets=args.target,
            test_regexes=args.test_regex,
            test_labels=args.test_label,
            parallel=args.parallel,
            configure_only=args.configure_only,
            build_only=args.build_only,
            compiler_cache=args.compiler_cache,
            skip_toolchain_validation=args.skip_toolchain_validation,
        )
    except NativeBuildError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    sys.exit(main())
