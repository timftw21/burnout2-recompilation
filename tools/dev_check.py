#!/usr/bin/env python3
"""Select and run the smallest conservative validation graph for a worktree diff."""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.validation_runner import ValidationNode, run_validation_graph
from tools.python_test_shards import DEFAULT_HISTORY, load_duration_history


PYTHON = sys.executable
PYTHON_PACKAGE_IDENTITIES = ("capstone", "cmake", "mypy", "ninja", "ruff")
DEFAULT_CACHE = REPO_ROOT / "build" / "local" / "dev-check" / "validation-cache.json"
BASE_NODE_NAMES = (
    "diff_check",
    "compileall",
    "ruff",
    "mypy",
    "native_toolchain",
    "maintenance",
    "preflight",
    "capsule_synthetic",
    "differential_synthetic",
    "ia32_phase0",
    "ia32_phase1",
    "ia32_phase2",
    "ia32_phase3",
    "ia32_phase4",
    "ia32_phase5",
    "ia32_phase6",
    "ia32_phase7",
    "aot_representative",
    "python_tests",
)


@dataclass(frozen=True)
class SelectionRule:
    name: str
    patterns: tuple[str, ...]
    python_tests: tuple[str, ...] = ()
    nodes: tuple[str, ...] = ()
    native_targets: tuple[str, ...] = ()
    ctest_labels: tuple[str, ...] = ()
    ctest_regexes: tuple[str, ...] = ()
    all_python_tests: bool = False


@dataclass
class CheckSelection:
    changed_paths: tuple[str, ...]
    nodes: set[str] = field(default_factory=lambda: {"diff_check"})
    python_tests: set[str] = field(default_factory=set)
    native_targets: set[str] = field(default_factory=set)
    ctest_labels: set[str] = field(default_factory=set)
    ctest_regexes: set[str] = field(default_factory=set)
    reasons: dict[str, list[str]] = field(default_factory=dict)
    all_python_tests: bool = False

    def add_reason(self, item: str, reason: str) -> None:
        reasons = self.reasons.setdefault(item, [])
        if reason not in reasons:
            reasons.append(reason)

    def add_node(self, name: str, reason: str) -> None:
        self.nodes.add(name)
        self.add_reason(f"node:{name}", reason)

    def add_python_test(self, name: str, reason: str) -> None:
        self.nodes.add("python_tests")
        self.python_tests.add(name)
        self.add_reason(f"python:{name}", reason)

    def add_native_target(self, name: str, reason: str) -> None:
        self.nodes.update({"native_toolchain", "native_debug"})
        self.native_targets.add(name)
        self.add_reason(f"native-target:{name}", reason)

    def add_ctest_label(self, label: str, reason: str) -> None:
        self.ctest_labels.add(label)
        self.add_reason(f"ctest-label:{label}", reason)

    def add_ctest_regex(self, expression: str, reason: str) -> None:
        self.ctest_regexes.add(expression)
        self.add_reason(f"ctest-regex:{expression}", reason)


RULES: tuple[SelectionRule, ...] = (
    SelectionRule(
        "validation infrastructure",
        (
            "tools/dev_check.py",
            "tools/quality_gate.py",
            "tools/validation_runner.py",
            "tools/python_test_shards.py",
            "tools/test_runtime_budgets.json",
            "tools/validation_closeout.py",
            "tools/validation_command.py",
        ),
        python_tests=(
            "tests.unit.test_dev_check",
            "tests.unit.test_validation_runner",
            "tests.unit.test_python_test_shards",
            "tests.unit.test_validation_closeout",
        ),
        nodes=("mypy",),
    ),
    SelectionRule(
        "native build wrapper",
        (
            "tools/native_build.py",
            "tools/compiler_cache.py",
            "tools/compiler_cache.lock.json",
            "tests/unit/test_native_build.py",
            "tests/unit/test_compiler_cache.py",
        ),
        python_tests=("tests.unit.test_native_build", "tests.unit.test_compiler_cache"),
        nodes=("mypy", "native_toolchain"),
    ),
    SelectionRule(
        "native toolchain",
        ("tools/native_toolchain.py", "tools/native_toolchain.lock.json"),
        python_tests=("tests.unit.test_native_toolchain",),
        nodes=(
            "mypy",
            "native_toolchain",
            "ia32_phase0",
            "ia32_phase1",
            "ia32_phase2",
            "ia32_phase3",
            "ia32_phase4",
            "ia32_phase5",
            "ia32_phase6",
            "ia32_phase7",
        ),
    ),
    SelectionRule(
        "replay capsules and differential execution",
        (
            "tools/playability/replay_capsule.py",
            "tools/playability/differential_replay.py",
            "tests/unit/test_replay_capsule.py",
            "tests/unit/test_differential_replay.py",
        ),
        python_tests=(
            "tests.unit.test_replay_capsule",
            "tests.unit.test_differential_replay",
        ),
        nodes=(
            "mypy",
            "capsule_synthetic",
            "differential_synthetic",
            "ia32_phase0",
            "ia32_phase1",
            "ia32_phase2",
            "ia32_phase3",
            "ia32_phase4",
            "ia32_phase5",
            "ia32_phase6",
            "ia32_phase7",
        ),
    ),
    SelectionRule(
        "native debug metadata",
        (
            "tools/recomp/debug_metadata.py",
            "tests/unit/test_debug_metadata.py",
        ),
        python_tests=("tests.unit.test_debug_metadata",),
        nodes=("mypy", "aot_representative"),
    ),
    SelectionRule(
        "x86 lifter",
        ("tools/recomp/x86_lifter.py",),
        python_tests=(
            "tests.unit.test_recomp_x86",
            "tests.unit.test_representative_aot",
            "tests.unit.test_replay_capsule",
        ),
        nodes=(
            "aot_representative",
            "ia32_phase0",
            "ia32_phase1",
            "ia32_phase2",
            "ia32_phase3",
            "ia32_phase4",
            "ia32_phase5",
            "ia32_phase6",
            "ia32_phase7",
        ),
    ),
    SelectionRule(
        "native executor",
        ("tools/recomp/native_executor.py",),
        python_tests=(
            "tests.unit.test_native_executor",
            "tests.unit.test_representative_aot",
            "tests.unit.test_debug_metadata",
            "tests.unit.test_differential_replay",
        ),
        nodes=("aot_representative",),
    ),
    SelectionRule(
        "same-ISA IA-32 backend and proof contracts",
        (
            "tools/recomp/ia32_native_backend.py",
            "tools/recomp/ia32_proof_contract.py",
            "tests/unit/test_ia32_native_backend.py",
            "tests/unit/test_ia32_proof_contract.py",
            "tests/unit/test_ia32_persistent_worker.py",
            "tests/unit/test_ia32_decoded_store_artifact.py",
            "tests/unit/test_ia32_architecture_memory.py",
            "tests/unit/test_ia32_host_abi.py",
            "tests/unit/test_ia32_resident_scheduler.py",
            "tests/unit/test_ia32_coverage_growth.py",
            "tests/unit/test_ia32_launcher_cutover.py",
            "tools/xbe/synthetic_fixture.py",
        ),
        python_tests=(
            "tests.unit.test_ia32_native_backend",
            "tests.unit.test_ia32_proof_contract",
            "tests.unit.test_ia32_persistent_worker",
            "tests.unit.test_ia32_decoded_store_artifact",
            "tests.unit.test_ia32_architecture_memory",
            "tests.unit.test_ia32_host_abi",
            "tests.unit.test_ia32_resident_scheduler",
            "tests.unit.test_ia32_coverage_growth",
            "tests.unit.test_ia32_launcher_cutover",
        ),
        nodes=(
            "mypy",
            "native_toolchain",
            "capsule_synthetic",
            "ia32_phase0",
            "ia32_phase1",
            "ia32_phase2",
            "ia32_phase3",
            "ia32_phase4",
            "ia32_phase5",
            "ia32_phase6",
            "ia32_phase7",
        ),
    ),
    SelectionRule(
        "recompiler support",
        (
            "tools/recomp/__init__.py",
            "tools/recomp/audit_x86_coverage.py",
            "tools/recomp/recompile_range.py",
        ),
        python_tests=(
            "tests.unit.test_recomp_x86",
            "tests.unit.test_native_executor",
            "tests.unit.test_representative_aot",
        ),
        nodes=("aot_representative",),
    ),
    SelectionRule(
        "generated-code fixtures",
        ("tests/fixtures/recomp/**", "tests/data/recomp/**"),
        python_tests=(
            "tests.unit.test_recomp_x86",
            "tests.unit.test_native_executor",
            "tests.unit.test_playability_probe",
        ),
    ),
    SelectionRule(
        "Xbox runtime shims",
        ("runtime/xbox/*.py",),
        python_tests=(
            "tests.unit.test_runtime_shims",
            "tests.unit.test_native_executor",
            "tests.unit.test_playability_probe",
        ),
    ),
    SelectionRule(
        "host command processor",
        ("runtime/host/nv2a_command_processor.cpp",),
        python_tests=(
            "tests.unit.test_command_work_cache_trace",
            "tests.unit.test_first_frame_smoke",
            "tests.unit.test_render_d3d8_stream",
            "tests.unit.test_render_debug_report",
        ),
        native_targets=("b2r_host_core_tests",),
        ctest_labels=("renderer",),
    ),
    SelectionRule(
        "host transport primitives",
        ("runtime/host/live_transport_layout.h", "runtime/host/dirty_ranges.h"),
        native_targets=("b2r_host_core_tests",),
        ctest_regexes=(
            r"^native\.host_core\.transport_layout_and_sequences$",
            r"^native\.host_core\.dirty_range_lifetime$",
        ),
    ),
    SelectionRule(
        "host command-work cache",
        ("runtime/host/command_work_cache.h", "tests/native/host_core_tests.cpp"),
        native_targets=("b2r_host_core_tests",),
        ctest_regexes=(r"^native\.host_core\.command_work_cache_layout_reuse$",),
    ),
    SelectionRule(
        "host pipeline primitives",
        ("runtime/host/frame_metrics.h", "runtime/host/native_pipeline_state.h"),
        native_targets=("b2r_host_core_tests",),
        ctest_regexes=(
            r"^native\.host_core\.native_pipeline_primitives$",
            r"^native\.host_core\.pipeline_key_and_fps_sampler$",
        ),
    ),
    SelectionRule(
        "NV2A vertex program",
        ("runtime/host/nv2a_vertex_program.h",),
        native_targets=("b2r_native_tests",),
        ctest_labels=("nv2a_vertex_program",),
    ),
    SelectionRule(
        "native host runtime",
        ("runtime/host/*", "runtime/platform/sdl/*", "runtime/nv2a/*"),
        python_tests=("tests.unit.test_first_frame_smoke",),
        native_targets=("b2r_host_core_tests",),
        ctest_labels=("renderer",),
    ),
    SelectionRule(
        "native test sources",
        (
            "tests/native/nv2a_vertex_program_tests.cpp",
            "tests/native/nv2a_vertex_program_execution_tests.cpp",
            "tests/native/nv2a_vertex_program_test_main.cpp",
            "tests/native/nv2a_vertex_program_test_cases.h",
        ),
        native_targets=("b2r_native_tests",),
        ctest_labels=("nv2a_vertex_program",),
    ),
    SelectionRule(
        "native host test sources",
        (
            "tests/native/host_transport_tests.cpp",
            "tests/native/host_texture_tests.cpp",
            "tests/native/host_pipeline_tests.cpp",
            "tests/native/host_core_test_main.cpp",
            "tests/native/host_core_test_cases.h",
            "tests/native/native_test_harness.h",
        ),
        native_targets=("b2r_host_core_tests",),
        ctest_labels=("host_core",),
    ),
    SelectionRule(
        "native build configuration",
        ("CMakeLists.txt", "CMakePresets.json", "*.cmake", "**/*.cmake", "cmake/**"),
        nodes=("native_toolchain",),
        native_targets=("b2r_native_tests", "b2r_host_core_tests"),
    ),
    SelectionRule(
        "global Python configuration",
        ("pyproject.toml", "requirements*.lock"),
        nodes=("mypy", "preflight", "native_toolchain"),
        all_python_tests=True,
    ),
)


def _run_git(*arguments: str) -> list[str]:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "git path discovery failed")
    return [line.strip().replace("\\", "/") for line in completed.stdout.splitlines() if line]


def discover_changed_paths(base: str | None = None) -> tuple[str, ...]:
    paths: set[str] = set()
    if base:
        paths.update(_run_git("diff", "--name-only", "--diff-filter=ACMR", f"{base}...HEAD", "--"))
    paths.update(_run_git("diff", "--name-only", "--diff-filter=ACMR", "HEAD", "--"))
    paths.update(_run_git("ls-files", "--others", "--exclude-standard"))
    return tuple(sorted(paths))


def _matches(path: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _module_for_test_path(path: str) -> str | None:
    if not fnmatch.fnmatchcase(path, "tests/**/test_*.py"):
        return None
    return path.removesuffix(".py").replace("/", ".")


def _matching_test_module_for_tool(path: str) -> str | None:
    if not path.endswith(".py") or not (path.startswith("tools/") or path.startswith("runtime/")):
        return None
    stem = Path(path).stem
    candidates = sorted((REPO_ROOT / "tests" / "unit").glob(f"test_{stem}.py"))
    if len(candidates) == 1:
        return f"tests.unit.{candidates[0].stem}"
    return None


def select_checks(
    changed_paths: Sequence[str],
    *,
    all_checks: bool = False,
    full: bool = False,
) -> CheckSelection:
    normalized = tuple(sorted({path.replace("\\", "/") for path in changed_paths}))
    selection = CheckSelection(normalized)
    selection.add_reason("node:diff_check", "every worktree validation checks whitespace")
    if all_checks or full:
        option = "--full" if full else "--all"
        for node in BASE_NODE_NAMES:
            selection.add_node(node, f"{option} selected the complete asset-free gate")
        selection.all_python_tests = True
        selection.add_reason("python:<all>", f"{option} selected every unittest case")
        if full:
            selection.add_native_target(
                "b2r_native_tests", "--full selected debug native validation"
            )
            selection.add_native_target(
                "b2r_host_core_tests", "--full selected debug native validation"
            )
        return selection

    for path in normalized:
        matched_rule = False
        if path.endswith(".py"):
            selection.add_node("compileall", f"{path} is Python source")
            selection.add_node("ruff", f"{path} is Python source")
        test_module = _module_for_test_path(path)
        if test_module is not None:
            selection.add_python_test(test_module, f"{path} changed")
            matched_rule = True
        matching_test = _matching_test_module_for_tool(path)
        if matching_test is not None:
            selection.add_python_test(matching_test, f"{path} maps by module name")
            matched_rule = True
        for rule in RULES:
            if not _matches(path, rule.patterns):
                continue
            matched_rule = True
            reason = f"{path} matched {rule.name}"
            for node in rule.nodes:
                selection.add_node(node, reason)
            for test in rule.python_tests:
                selection.add_python_test(test, reason)
            for target in rule.native_targets:
                selection.add_native_target(target, reason)
            for label in rule.ctest_labels:
                selection.add_ctest_label(label, reason)
            for expression in rule.ctest_regexes:
                selection.add_ctest_regex(expression, reason)
            if rule.all_python_tests:
                selection.all_python_tests = True
                selection.add_node("python_tests", reason)
                selection.add_reason("python:<all>", reason)
        source_suffixes = (".py", ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".inl")
        if path.startswith(("tools/", "runtime/")) and path.endswith(source_suffixes):
            if not matched_rule:
                selection.all_python_tests = True
                selection.add_node(
                    "python_tests", f"unmapped source {path} uses conservative coverage"
                )
                selection.add_reason("python:<all>", f"unmapped source {path}")
                if path.endswith(source_suffixes[1:]):
                    reason = f"unmapped native source {path} uses conservative coverage"
                    selection.add_native_target("b2r_native_tests", reason)
                    selection.add_native_target("b2r_host_core_tests", reason)
        elif (
            not matched_rule
            and not path.startswith(("docs/", ".github/"))
            and path
            not in {
                "README.md",
                "CONTRIBUTING.md",
                "LICENSE",
                "NOTICE.md",
            }
        ):
            selection.all_python_tests = True
            selection.add_node(
                "python_tests", f"unmapped project file {path} uses conservative coverage"
            )
            selection.add_reason("python:<all>", f"unmapped project file {path}")
    return selection


def _selected_python_paths(selection: CheckSelection) -> tuple[str, ...]:
    if selection.all_python_tests:
        return ("runtime", "tools", "tests")
    paths = [path for path in selection.changed_paths if path.endswith(".py")]
    return tuple(paths or ("tools/dev_check.py",))


def _python_test_command(selection: CheckSelection, jobs: int) -> tuple[str, ...]:
    command = [
        PYTHON,
        "tools/python_test_shards.py",
        "--jobs",
        str(max(1, jobs)),
    ]
    if not selection.all_python_tests:
        for test in sorted(selection.python_tests):
            command.extend(("--test", test))
    return tuple(command)


def _native_command(selection: CheckSelection, jobs: int) -> tuple[str, ...]:
    command = [
        PYTHON,
        "tools/native_build.py",
        "--preset",
        "debug",
        "--skip-toolchain-validation",
        "--parallel",
        str(max(1, jobs)),
    ]
    for target in sorted(selection.native_targets):
        command.extend(("--target", target))
    for expression in sorted(selection.ctest_regexes):
        command.extend(("--test-regex", expression))
    for label in sorted(selection.ctest_labels):
        command.extend(("--label", re.escape(label)))
    return tuple(command)


def _native_tool_inputs() -> tuple[str, ...]:
    inputs: list[str] = []
    suffix = ".exe" if os.name == "nt" else ""
    try:
        import cmake
        import ninja
    except ImportError:
        pass
    else:
        cmake_directory = Path(cmake.CMAKE_BIN_DIR)
        ninja_directory = Path(ninja.BIN_DIR)
        inputs.extend(
            str(path)
            for path in (
                cmake_directory / f"cmake{suffix}",
                cmake_directory / f"ctest{suffix}",
                ninja_directory / f"ninja{suffix}",
            )
        )
    compiler = shutil.which("clang++")
    if compiler is not None:
        inputs.append(compiler)
    return tuple(inputs)


def build_validation_nodes(selection: CheckSelection, *, jobs: int) -> dict[str, ValidationNode]:
    python_paths = _selected_python_paths(selection)
    python_inputs = (
        "runtime/**/*.py",
        "tools/**/*.py",
        "tests/**/*.py",
        "pyproject.toml",
        "requirements.lock",
        "requirements-dev.lock",
        "tools/test_runtime_budgets.json",
    )
    native_outputs = tuple(
        f"build/cmake/debug/{target}.exe" for target in sorted(selection.native_targets)
    )
    native_tool_inputs = _native_tool_inputs()
    return {
        "diff_check": ValidationNode(
            "diff_check",
            ("git", "diff", "--check"),
            "worktree whitespace validation",
            cacheable=False,
            priority=0,
            budget_seconds=15.0,
        ),
        "compileall": ValidationNode(
            "compileall",
            (PYTHON, "-m", "compileall", "-q", *python_paths),
            "Python bytecode compilation",
            inputs=tuple(path for path in selection.changed_paths if path.endswith(".py"))
            if not selection.all_python_tests
            else ("runtime/**/*.py", "tools/**/*.py", "tests/**/*.py"),
            priority=10,
            budget_seconds=30.0,
        ),
        "ruff": ValidationNode(
            "ruff",
            (PYTHON, "-m", "ruff", "check", *python_paths),
            "Ruff static checks",
            inputs=("pyproject.toml", "requirements-dev.lock")
            + (
                tuple(path for path in selection.changed_paths if path.endswith(".py"))
                if not selection.all_python_tests
                else ("runtime/**/*.py", "tools/**/*.py", "tests/**/*.py")
            ),
            package_identities=("ruff",),
            priority=10,
            budget_seconds=30.0,
        ),
        "mypy": ValidationNode(
            "mypy",
            (PYTHON, "-m", "mypy"),
            "strict type checking",
            inputs=("pyproject.toml", "requirements-dev.lock", "tools/**/*.py"),
            environment_keys=("MYPYPATH",),
            package_identities=("mypy",),
            priority=20,
            budget_seconds=120.0,
        ),
        "native_toolchain": ValidationNode(
            "native_toolchain",
            (PYTHON, "tools/native_toolchain.py", "--aot-tools-only"),
            "pinned native build-tool and compiler validation",
            inputs=(
                "requirements-dev.lock",
                "tools/native_toolchain.py",
                "tools/native_toolchain.lock.json",
            ),
            environment_keys=("PATH",),
            package_identities=("cmake", "ninja"),
            priority=20,
            budget_seconds=30.0,
        ),
        "maintenance": ValidationNode(
            "maintenance",
            (PYTHON, "tools/project_maintenance.py", "status", "--fail-over-budget"),
            "local artifact budget validation",
            inputs=("tools/project_maintenance.py",),
            cacheable=False,
            priority=30,
            budget_seconds=15.0,
        ),
        "preflight": ValidationNode(
            "preflight",
            (PYTHON, "tools/playability/preflight_validation.py", "--synthetic-fixtures"),
            "synthetic four-layer preflight",
            inputs=python_inputs,
            dependencies=("compileall",),
            environment_keys=("PYTHONHASHSEED", "PYTHONPATH"),
            package_identities=PYTHON_PACKAGE_IDENTITIES,
            priority=40,
            budget_seconds=300.0,
        ),
        "aot_representative": ValidationNode(
            "aot_representative",
            (PYTHON, "tools/recomp/representative_aot.py"),
            "asset-free representative AOT compile corpus",
            inputs=(
                "tools/recomp/x86_lifter.py",
                "tools/recomp/native_executor.py",
                "tools/recomp/representative_aot.py",
                "tools/compiler_cache.py",
                "tools/compiler_cache.lock.json",
                "tools/native_toolchain.lock.json",
            ),
            dependencies=("compileall", "native_toolchain"),
            environment_keys=("B2R_SCCACHE", "PATH"),
            outputs=("build/local/representative-aot/native-module-manifest.sqlite3",),
            exclusive=True,
            priority=70,
            budget_seconds=120.0,
        ),
        "capsule_synthetic": ValidationNode(
            "capsule_synthetic",
            (
                PYTHON,
                "tools/playability/replay_capsule.py",
                "synthetic",
                "--output",
                "build/local/dev-check/synthetic-replay.b2rcap",
            ),
            "asset-free deterministic replay capsule",
            inputs=(
                "tools/playability/replay_capsule.py",
                "tools/recomp/x86_lifter.py",
            ),
            dependencies=("compileall",),
            outputs=("build/local/dev-check/synthetic-replay.b2rcap",),
            priority=60,
            budget_seconds=15.0,
        ),
        "differential_synthetic": ValidationNode(
            "differential_synthetic",
            (
                PYTHON,
                "tools/playability/differential_replay.py",
                "execute",
                "build/local/dev-check/synthetic-replay.b2rcap",
                "--experimental",
                "interpreter",
                "--max-steps",
                "4",
                "--report",
                "build/local/dev-check/synthetic-differential.json",
            ),
            "asset-free differential replay",
            inputs=(
                "tools/playability/differential_replay.py",
                "tools/playability/replay_capsule.py",
                "tools/recomp/x86_lifter.py",
            ),
            dependencies=("capsule_synthetic",),
            outputs=("build/local/dev-check/synthetic-differential.json",),
            priority=65,
            budget_seconds=15.0,
        ),
        "ia32_phase0": ValidationNode(
            "ia32_phase0",
            (
                PYTHON,
                "tools/recomp/ia32_proof_contract.py",
                "synthetic",
                "--build-dir",
                "build/local/ia32-phase0",
                "--report",
                "build/local/ia32-phase0/phase0-proof-report.json",
            ),
            "asset-free same-ISA IA-32 Phase-0 proofs",
            inputs=(
                "tools/recomp/ia32_proof_contract.py",
                "tools/recomp/ia32_native_backend.py",
                "tools/playability/differential_replay.py",
                "tools/playability/replay_capsule.py",
                "tools/recomp/x86_lifter.py",
                "tools/native_toolchain.py",
                "tools/native_toolchain.lock.json",
            ),
            dependencies=("compileall", "native_toolchain"),
            environment_keys=("PATH", "WindowsSdkDir"),
            outputs=("build/local/ia32-phase0/phase0-proof-report.json",),
            exclusive=True,
            priority=75,
            budget_seconds=30.0,
        ),
        "ia32_phase1": ValidationNode(
            "ia32_phase1",
            (
                PYTHON,
                "tools/recomp/ia32_proof_contract.py",
                "persistent",
                "--build-dir",
                "build/local/ia32-phase1",
                "--report",
                "build/local/ia32-phase1/phase1-proof-report.json",
            ),
            "asset-free same-ISA IA-32 Phase-1 resident-worker proofs",
            inputs=(
                "tools/recomp/ia32_proof_contract.py",
                "tools/recomp/ia32_native_backend.py",
                "tools/playability/differential_replay.py",
                "tools/playability/replay_capsule.py",
                "tools/recomp/x86_lifter.py",
                "tools/native_toolchain.py",
                "tools/native_toolchain.lock.json",
            ),
            dependencies=("ia32_phase0",),
            environment_keys=("PATH", "WindowsSdkDir"),
            outputs=("build/local/ia32-phase1/phase1-proof-report.json",),
            exclusive=True,
            priority=76,
            budget_seconds=30.0,
        ),
        "ia32_phase2": ValidationNode(
            "ia32_phase2",
            (
                PYTHON,
                "tools/recomp/ia32_proof_contract.py",
                "decoded-store",
                "--build-dir",
                "build/local/ia32-phase2",
                "--report",
                "build/local/ia32-phase2/phase2-proof-report.json",
            ),
            "asset-free same-ISA IA-32 Phase-2 decoded-store artifact proof",
            inputs=(
                "tools/recomp/ia32_proof_contract.py",
                "tools/recomp/ia32_native_backend.py",
                "tools/xbe/synthetic_fixture.py",
                "tools/loader/xbe_loader.py",
                "tools/xbe/xbe_info.py",
                "tools/playability/differential_replay.py",
                "tools/playability/replay_capsule.py",
                "tools/recomp/x86_lifter.py",
                "tools/native_toolchain.py",
                "tools/native_toolchain.lock.json",
            ),
            dependencies=("ia32_phase1",),
            environment_keys=("PATH", "WindowsSdkDir"),
            outputs=("build/local/ia32-phase2/phase2-proof-report.json",),
            exclusive=True,
            priority=77,
            budget_seconds=30.0,
        ),
        "ia32_phase3": ValidationNode(
            "ia32_phase3",
            (
                PYTHON,
                "tools/recomp/ia32_proof_contract.py",
                "architecture",
                "--build-dir",
                "build/local/ia32-phase3",
                "--report",
                "build/local/ia32-phase3/phase3-proof-report.json",
            ),
            "asset-free same-ISA IA-32 Phase-3 architecture and memory proofs",
            inputs=(
                "tools/recomp/ia32_proof_contract.py",
                "tools/recomp/ia32_native_backend.py",
                "tools/playability/differential_replay.py",
                "tools/playability/replay_capsule.py",
                "tools/recomp/x86_lifter.py",
                "tools/native_toolchain.py",
                "tools/native_toolchain.lock.json",
            ),
            dependencies=("ia32_phase2",),
            environment_keys=("PATH", "WindowsSdkDir"),
            outputs=("build/local/ia32-phase3/phase3-proof-report.json",),
            exclusive=True,
            priority=78,
            budget_seconds=30.0,
        ),
        "ia32_phase4": ValidationNode(
            "ia32_phase4",
            (
                PYTHON,
                "tools/recomp/ia32_proof_contract.py",
                "host-abi",
                "--build-dir",
                "build/local/ia32-phase4",
                "--report",
                "build/local/ia32-phase4/phase4-proof-report.json",
            ),
            "asset-free same-ISA IA-32 Phase-4 native host ABI proofs",
            inputs=(
                "tools/recomp/ia32_proof_contract.py",
                "tools/recomp/ia32_native_backend.py",
                "tools/playability/differential_replay.py",
                "tools/playability/replay_capsule.py",
                "tools/recomp/x86_lifter.py",
                "tools/native_toolchain.py",
                "tools/native_toolchain.lock.json",
            ),
            dependencies=("ia32_phase3",),
            environment_keys=("PATH", "WindowsSdkDir"),
            outputs=("build/local/ia32-phase4/phase4-proof-report.json",),
            exclusive=True,
            priority=79,
            budget_seconds=30.0,
        ),
        "ia32_phase5": ValidationNode(
            "ia32_phase5",
            (
                PYTHON,
                "tools/recomp/ia32_proof_contract.py",
                "scheduler",
                "--build-dir",
                "build/local/ia32-phase5",
                "--report",
                "build/local/ia32-phase5/phase5-proof-report.json",
            ),
            "asset-free same-ISA IA-32 Phase-5 resident scheduler proofs",
            inputs=(
                "tools/recomp/ia32_proof_contract.py",
                "tools/recomp/ia32_native_backend.py",
                "tools/playability/differential_replay.py",
                "tools/playability/replay_capsule.py",
                "tools/recomp/x86_lifter.py",
                "tools/native_toolchain.py",
                "tools/native_toolchain.lock.json",
            ),
            dependencies=("ia32_phase4",),
            environment_keys=("PATH", "WindowsSdkDir"),
            outputs=("build/local/ia32-phase5/phase5-proof-report.json",),
            exclusive=True,
            priority=80,
            budget_seconds=30.0,
        ),
        "ia32_phase6": ValidationNode(
            "ia32_phase6",
            (
                PYTHON,
                "tools/recomp/ia32_proof_contract.py",
                "coverage",
                "--build-dir",
                "build/local/ia32-phase6",
                "--report",
                "build/local/ia32-phase6/phase6-proof-report.json",
            ),
            "asset-free same-ISA IA-32 Phase-6 measured coverage-growth proof",
            inputs=(
                "tools/recomp/ia32_proof_contract.py",
                "tools/recomp/ia32_native_backend.py",
                "tools/playability/differential_replay.py",
                "tools/playability/replay_capsule.py",
                "tools/recomp/x86_lifter.py",
                "tools/native_toolchain.py",
                "tools/native_toolchain.lock.json",
            ),
            dependencies=("ia32_phase5",),
            environment_keys=("PATH", "WindowsSdkDir"),
            outputs=("build/local/ia32-phase6/phase6-proof-report.json",),
            exclusive=True,
            priority=81,
            budget_seconds=30.0,
        ),
        "ia32_phase7": ValidationNode(
            "ia32_phase7",
            (
                PYTHON,
                "tools/recomp/ia32_proof_contract.py",
                "cutover",
                "--build-dir",
                "build/local/ia32-phase7",
                "--report",
                "build/local/ia32-phase7/phase7-proof-report.json",
            ),
            "asset-free same-ISA IA-32 Phase-7 normal-launcher cutover proof",
            inputs=(
                "tools/recomp/ia32_proof_contract.py",
                "tools/recomp/ia32_native_backend.py",
                "tools/playability/differential_replay.py",
                "tools/playability/replay_capsule.py",
                "tools/recomp/x86_lifter.py",
                "tools/native_toolchain.py",
                "tools/native_toolchain.lock.json",
            ),
            dependencies=("ia32_phase6",),
            environment_keys=("PATH", "WindowsSdkDir"),
            outputs=("build/local/ia32-phase7/phase7-proof-report.json",),
            exclusive=True,
            priority=82,
            budget_seconds=30.0,
        ),
        "python_tests": ValidationNode(
            "python_tests",
            _python_test_command(selection, jobs),
            "duration-balanced isolated Python unittest shards",
            inputs=python_inputs,
            dependencies=("compileall", "ruff"),
            environment_keys=("PYTHONHASHSEED", "PYTHONPATH"),
            package_identities=PYTHON_PACKAGE_IDENTITIES,
            exclusive=True,
            priority=83,
            budget_seconds=300.0,
        ),
        "native_debug": ValidationNode(
            "native_debug",
            _native_command(selection, jobs),
            "targeted debug native build and CTest",
            inputs=(
                "CMakeLists.txt",
                "CMakePresets.json",
                "runtime/**/*.cpp",
                "runtime/**/*.h",
                "tests/native/**/*.cpp",
                "tests/native/**/*.h",
                "tools/native_build.py",
                "tools/compiler_cache.py",
                "tools/compiler_cache.lock.json",
                "tools/native_toolchain.py",
                "tools/native_toolchain.lock.json",
                *native_tool_inputs,
            ),
            dependencies=("native_toolchain",),
            environment_keys=("CC", "CFLAGS", "CXX", "CXXFLAGS", "LDFLAGS", "PATH"),
            package_identities=("cmake", "ninja"),
            outputs=native_outputs,
            exclusive=True,
            priority=90,
            budget_seconds=600.0,
        ),
    }


def explain_selection(selection: CheckSelection) -> None:
    print("Changed paths:")
    if selection.changed_paths:
        for path in selection.changed_paths:
            print(f"  {path}")
    else:
        print("  <none>")
    print("Selected validation:")
    for item in sorted(selection.reasons):
        print(f"  {item}")
        for reason in selection.reasons[item]:
            print(f"    - {reason}")


def report_slowest_python_tests() -> None:
    durations = load_duration_history(DEFAULT_HISTORY)
    slowest = sorted(durations.items(), key=lambda item: item[1], reverse=True)[:5]
    if not slowest:
        return
    print("Slowest historical unittest cases:")
    for identifier, duration in slowest:
        print(f"  {duration:.3f}s {identifier}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="Run every asset-free base node.")
    parser.add_argument("--full", action="store_true", help="Add the complete debug native gate.")
    parser.add_argument("--base", help="Also select files changed since BASE...HEAD.")
    parser.add_argument("--path", action="append", default=[], help="Explicit changed path.")
    parser.add_argument("--explain", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument(
        "--launch-closeout",
        action="store_true",
        help="After this focused gate passes, launch the exhaustive matrix in the background.",
    )
    parser.add_argument("--closeout-replay-capsule", type=Path, action="append", default=[])
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1))
    args = parser.parse_args(argv)
    if args.jobs <= 0:
        parser.error("--jobs must be positive")
    paths = tuple(args.path) if args.path else discover_changed_paths(args.base)
    selection = select_checks(
        paths,
        all_checks=args.all or args.full,
        full=args.full,
    )
    if args.explain or args.dry_run:
        explain_selection(selection)
    if args.dry_run:
        if args.launch_closeout:
            print("A passing focused gate would launch the asynchronous closeout matrix.")
        return 0
    nodes = build_validation_nodes(selection, jobs=args.jobs)
    selected = sorted(selection.nodes)
    summary = run_validation_graph(
        nodes,
        selected,
        cache_path=args.cache,
        jobs=args.jobs,
        no_cache=args.no_cache,
    )
    if "python_tests" in summary.results:
        report_slowest_python_tests()
    cached_count = sum(result.status == "cached" for result in summary.results.values())
    print(
        f"Validation {'passed' if summary.passed else 'failed'} in "
        f"{summary.elapsed_seconds:.2f}s ({cached_count} cached nodes)."
    )
    if summary.passed and args.launch_closeout:
        from tools.validation_closeout import launch_closeout

        run_directory = launch_closeout(
            jobs=args.jobs,
            replay_capsules=args.closeout_replay_capsule,
        )
        print(f"Asynchronous closeout launched: {run_directory}")
    return 0 if summary.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
