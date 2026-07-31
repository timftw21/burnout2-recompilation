#!/usr/bin/env python3
"""Discover unittest cases, balance them by duration, and run isolated shards."""

from __future__ import annotations

import argparse
import fnmatch
import io
import json
import math
import os
import shutil
import subprocess
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
DEFAULT_HISTORY = REPO_ROOT / "build" / "local" / "dev-check" / "test-durations.json"
DEFAULT_WORK_ROOT = REPO_ROOT / "build" / "local" / "dev-check" / "test-work"
DEFAULT_BUDGETS = REPO_ROOT / "tools" / "test_runtime_budgets.json"
TEST_HISTORY_SCHEMA = 2
LEGACY_TEST_HISTORY_SCHEMA = 1
FAILURE_RUN_LIMIT = 8

# Add only tests proven to depend on process-global external state. Ordinary
# module globals are isolated because every shard has its own Python process.
SERIAL_TEST_PATTERNS: tuple[str, ...] = ()


@dataclass(frozen=True)
class TestShard:
    index: int
    test_ids: tuple[str, ...]
    predicted_seconds: float
    serial: bool = False


@dataclass(frozen=True)
class ShardExecution:
    shard: TestShard
    returncode: int
    duration_seconds: float
    output: str
    result_path: Path
    command: tuple[str, ...]


@dataclass(frozen=True)
class DurationStatistics:
    runs: int
    mean_seconds: float
    p50_seconds: float
    p95_seconds: float
    last_seconds: float


@dataclass(frozen=True)
class RuntimeBudgetConfig:
    default_test_seconds: float
    history_sample_limit: int
    minimum_samples: int
    p95_ratio: float
    minimum_delta_seconds: float
    overrides: tuple[tuple[str, float], ...] = ()
    repeat_only: tuple[str, ...] = ()
    shared_fixture_groups: tuple[str, ...] = ()


@dataclass(frozen=True)
class BudgetFinding:
    identifier: str
    duration_seconds: float
    budget_seconds: float
    historical_p95_seconds: float | None
    kind: str


def flatten_suite(suite: unittest.TestSuite) -> list[unittest.TestCase]:
    tests: list[unittest.TestCase] = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            tests.extend(flatten_suite(item))
        else:
            tests.append(item)
    return tests


def discover_test_ids(
    test_names: Sequence[str],
    *,
    start_directory: str = "tests",
    root: Path = REPO_ROOT,
) -> list[str]:
    loader = unittest.TestLoader()
    if test_names:
        suite = loader.loadTestsFromNames(list(test_names))
    else:
        suite = loader.discover(
            str(root / start_directory),
            top_level_dir=str(root),
        )
    if loader.errors:
        raise RuntimeError(
            "unittest discovery failed:\n" + "\n".join(str(error) for error in loader.errors)
        )
    identifiers = sorted({test.id() for test in flatten_suite(suite)})
    if not identifiers:
        raise RuntimeError("unittest discovery selected no tests")
    return identifiers


def _percentile(samples: Sequence[float], percentile: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    rank = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[rank]


def load_duration_statistics(path: Path) -> dict[str, DurationStatistics]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    schema = payload.get("schema")
    if schema not in {LEGACY_TEST_HISTORY_SCHEMA, TEST_HISTORY_SCHEMA}:
        return {}
    tests = payload.get("tests")
    if not isinstance(tests, dict):
        return {}
    durations: dict[str, DurationStatistics] = {}
    for identifier, record in tests.items():
        if not isinstance(record, dict):
            continue
        mean = record.get("mean_seconds")
        last = record.get("last_seconds")
        runs = record.get("runs")
        samples = record.get("samples_seconds")
        numeric_samples = (
            [float(value) for value in samples if isinstance(value, (int, float)) and value >= 0]
            if isinstance(samples, list)
            else []
        )
        if not numeric_samples and isinstance(last, (int, float)) and last >= 0:
            numeric_samples = [float(last)]
        if not isinstance(mean, (int, float)) or mean < 0 or not numeric_samples:
            continue
        numeric_runs = int(runs) if isinstance(runs, int) and runs > 0 else len(numeric_samples)
        durations[str(identifier)] = DurationStatistics(
            runs=numeric_runs,
            mean_seconds=float(mean),
            p50_seconds=_percentile(numeric_samples, 0.50),
            p95_seconds=_percentile(numeric_samples, 0.95),
            last_seconds=float(last) if isinstance(last, (int, float)) else numeric_samples[-1],
        )
    return durations


def load_duration_history(path: Path) -> dict[str, float]:
    return {
        identifier: statistics.p50_seconds
        for identifier, statistics in load_duration_statistics(path).items()
    }


def load_runtime_budget_config(path: Path) -> RuntimeBudgetConfig:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        raise RuntimeError(f"unsupported runtime-budget schema in {path}")
    regression = payload.get("material_regression")
    if not isinstance(regression, dict):
        raise RuntimeError(f"missing material_regression object in {path}")

    def patterns(field_name: str) -> tuple[str, ...]:
        values = payload.get(field_name, [])
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise RuntimeError(f"{field_name} must be a string list in {path}")
        return tuple(values)

    raw_overrides = payload.get("test_overrides", [])
    if not isinstance(raw_overrides, list):
        raise RuntimeError(f"test_overrides must be a list in {path}")
    overrides: list[tuple[str, float]] = []
    for record in raw_overrides:
        if not isinstance(record, dict):
            raise RuntimeError(f"invalid test override in {path}")
        pattern = record.get("pattern")
        seconds = record.get("seconds")
        if not isinstance(pattern, str) or not isinstance(seconds, (int, float)) or seconds <= 0:
            raise RuntimeError(f"invalid test override in {path}")
        overrides.append((pattern, float(seconds)))
    default_seconds = payload.get("default_test_seconds")
    sample_limit = payload.get("history_sample_limit")
    minimum_samples = regression.get("minimum_samples")
    p95_ratio = regression.get("p95_ratio")
    minimum_delta = regression.get("minimum_delta_seconds")
    if (
        not isinstance(default_seconds, (int, float))
        or default_seconds <= 0
        or not isinstance(sample_limit, int)
        or sample_limit <= 0
        or not isinstance(minimum_samples, int)
        or minimum_samples <= 0
        or not isinstance(p95_ratio, (int, float))
        or p95_ratio <= 1.0
        or not isinstance(minimum_delta, (int, float))
        or minimum_delta < 0
    ):
        raise RuntimeError(f"invalid runtime-budget values in {path}")
    return RuntimeBudgetConfig(
        default_test_seconds=float(default_seconds),
        history_sample_limit=sample_limit,
        minimum_samples=minimum_samples,
        p95_ratio=float(p95_ratio),
        minimum_delta_seconds=float(minimum_delta),
        overrides=tuple(overrides),
        repeat_only=patterns("repeat_only"),
        shared_fixture_groups=patterns("shared_fixture_groups"),
    )


def test_budget_seconds(identifier: str, config: RuntimeBudgetConfig) -> float:
    for pattern, seconds in config.overrides:
        if fnmatch.fnmatchcase(identifier, pattern):
            return seconds
    return config.default_test_seconds


def evaluate_runtime_budgets(
    durations: dict[str, float],
    *,
    history: dict[str, DurationStatistics],
    config: RuntimeBudgetConfig,
) -> list[BudgetFinding]:
    findings: list[BudgetFinding] = []
    for identifier, duration in sorted(durations.items()):
        budget = test_budget_seconds(identifier, config)
        prior = history.get(identifier)
        if duration > budget:
            findings.append(
                BudgetFinding(
                    identifier,
                    duration,
                    budget,
                    prior.p95_seconds if prior is not None else None,
                    "budget",
                )
            )
            continue
        if (
            prior is not None
            and prior.runs >= config.minimum_samples
            and duration
            > max(
                prior.p95_seconds * config.p95_ratio,
                prior.p95_seconds + config.minimum_delta_seconds,
            )
        ):
            findings.append(
                BudgetFinding(
                    identifier,
                    duration,
                    budget,
                    prior.p95_seconds,
                    "regression",
                )
            )
    return findings


def balance_test_ids(
    test_ids: Sequence[str],
    *,
    jobs: int,
    durations: dict[str, float],
    serial_patterns: Sequence[str] = SERIAL_TEST_PATTERNS,
    shared_fixture_patterns: Sequence[str] = (),
) -> list[TestShard]:
    """Balance atomic fixture groups, then extract process-global serial tests."""

    serial_ids = [
        identifier
        for identifier in test_ids
        if any(fnmatch.fnmatchcase(identifier, pattern) for pattern in serial_patterns)
    ]
    serial_id_set = set(serial_ids)
    parallel_ids = [identifier for identifier in test_ids if identifier not in serial_id_set]
    known = sorted(durations.values())
    default_duration = known[len(known) // 2] if known else 0.01
    shard_count = min(max(1, jobs), max(1, len(parallel_ids)))
    assignments: list[list[str]] = [[] for _ in range(shard_count)]
    totals = [0.0 for _ in range(shard_count)]
    grouped: set[str] = set()
    bundles: list[tuple[str, ...]] = []
    for pattern in shared_fixture_patterns:
        bundle = tuple(
            sorted(identifier for identifier in parallel_ids if fnmatch.fnmatchcase(identifier, pattern))
        )
        if bundle:
            overlap = grouped.intersection(bundle)
            if overlap:
                raise ValueError(
                    f"shared fixture patterns overlap for {sorted(overlap)[0]}"
                )
            grouped.update(bundle)
            bundles.append(bundle)
    bundles.extend((identifier,) for identifier in parallel_ids if identifier not in grouped)
    for bundle in sorted(
        bundles,
        key=lambda items: (
            -sum(durations.get(item, default_duration) for item in items),
            items,
        ),
    ):
        target = min(range(shard_count), key=lambda index: (totals[index], index))
        assignments[target].extend(bundle)
        totals[target] += sum(durations.get(identifier, default_duration) for identifier in bundle)
    shards = [
        TestShard(index, tuple(sorted(identifiers)), totals[index])
        for index, identifiers in enumerate(assignments)
        if identifiers
    ]
    if serial_ids:
        shards.append(
            TestShard(
                len(shards),
                tuple(sorted(serial_ids)),
                sum(durations.get(identifier, default_duration) for identifier in serial_ids),
                serial=True,
            )
        )
    return shards


class TimingTextResult(unittest.TextTestResult):
    """Text result that records each fully-qualified unittest case duration."""

    test_durations: dict[str, float]
    _test_started: float

    def startTestRun(self) -> None:
        self.test_durations: dict[str, float] = {}
        self._test_started = 0.0
        super().startTestRun()

    def startTest(self, test: unittest.TestCase) -> None:
        self._test_started = time.perf_counter()
        super().startTest(test)

    def stopTest(self, test: unittest.TestCase) -> None:
        self.test_durations[test.id()] = time.perf_counter() - self._test_started
        super().stopTest(test)


class TimingTextRunner(unittest.TextTestRunner):
    def _makeResult(self) -> TimingTextResult:
        return TimingTextResult(self.stream, self.descriptions, self.verbosity)


def _worker_main(shard_file: Path, result_path: Path) -> int:
    identifiers = json.loads(shard_file.read_text(encoding="utf-8"))
    if not isinstance(identifiers, list) or not all(
        isinstance(identifier, str) for identifier in identifiers
    ):
        raise RuntimeError("invalid unittest shard file")
    suite = unittest.TestLoader().loadTestsFromNames(identifiers)
    stream = io.StringIO()
    started = time.perf_counter()
    runner = TimingTextRunner(
        stream=stream,
        verbosity=1,
    )
    result = runner.run(suite)
    elapsed = time.perf_counter() - started
    assert isinstance(result, TimingTextResult)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(
            {
                "successful": result.wasSuccessful(),
                "tests_run": result.testsRun,
                "failures": len(result.failures),
                "errors": len(result.errors),
                "skipped": len(result.skipped),
                "duration_seconds": elapsed,
                "test_durations": result.test_durations,
                "failed_test_ids": sorted(
                    {
                        test.id()
                        for test, _traceback in (*result.failures, *result.errors)
                    }
                ),
                "runner_output": stream.getvalue(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    if not result.wasSuccessful():
        print(stream.getvalue(), end="")
        return 1
    return 0


def _run_shard(shard: TestShard, *, run_root: Path) -> ShardExecution:
    shard_root = run_root / f"shard-{shard.index:03d}"
    temp_root = shard_root / "tmp"
    shard_file = shard_root / "tests.json"
    result_path = shard_root / "result.json"
    temp_root.mkdir(parents=True, exist_ok=True)
    shard_file.write_text(
        json.dumps(list(shard.test_ids), indent=2) + "\n",
        encoding="utf-8",
    )
    command = (
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker-shard",
        str(shard_file),
        "--worker-result",
        str(result_path),
    )
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONHASHSEED": "0",
            "B2R_TEST_SHARD": str(shard.index),
            "TEMP": str(temp_root),
            "TMP": str(temp_root),
            "TMPDIR": str(temp_root),
        }
    )
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return ShardExecution(
        shard=shard,
        returncode=completed.returncode,
        duration_seconds=time.perf_counter() - started,
        output=completed.stdout,
        result_path=result_path,
        command=command,
    )


def update_duration_history(
    path: Path,
    executions: Sequence[ShardExecution],
    *,
    sample_limit: int = 64,
) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {"schema": TEST_HISTORY_SCHEMA, "tests": {}}
    if not isinstance(payload, dict):
        payload = {"schema": TEST_HISTORY_SCHEMA, "tests": {}}
    if payload.get("schema") not in {
        LEGACY_TEST_HISTORY_SCHEMA,
        TEST_HISTORY_SCHEMA,
    } or not isinstance(payload.get("tests"), dict):
        payload = {"schema": TEST_HISTORY_SCHEMA, "tests": {}}
    payload["schema"] = TEST_HISTORY_SCHEMA
    tests = payload["tests"]
    assert isinstance(tests, dict)
    for execution in executions:
        try:
            result = json.loads(execution.result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        durations = result.get("test_durations")
        if not isinstance(durations, dict):
            continue
        for identifier, duration in durations.items():
            if not isinstance(duration, (int, float)) or duration < 0:
                continue
            prior = tests.get(identifier)
            prior_runs = int(prior.get("runs", 0)) if isinstance(prior, dict) else 0
            prior_mean = float(prior.get("mean_seconds", 0.0)) if isinstance(prior, dict) else 0.0
            prior_samples = prior.get("samples_seconds", []) if isinstance(prior, dict) else []
            samples = (
                [
                    float(value)
                    for value in prior_samples
                    if isinstance(value, (int, float)) and value >= 0
                ]
                if isinstance(prior_samples, list)
                else []
            )
            if not samples and isinstance(prior, dict):
                prior_last = prior.get("last_seconds")
                if isinstance(prior_last, (int, float)) and prior_last >= 0:
                    samples.append(float(prior_last))
            runs = prior_runs + 1
            mean = prior_mean + (float(duration) - prior_mean) / runs
            samples.append(float(duration))
            samples = samples[-sample_limit:]
            tests[identifier] = {
                "runs": runs,
                "mean_seconds": round(mean, 9),
                "p50_seconds": round(_percentile(samples, 0.50), 9),
                "p95_seconds": round(_percentile(samples, 0.95), 9),
                "last_seconds": round(float(duration), 9),
                "samples_seconds": [round(value, 9) for value in samples],
            }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def execution_test_durations(executions: Sequence[ShardExecution]) -> dict[str, float]:
    durations: dict[str, float] = {}
    for execution in executions:
        try:
            result = json.loads(execution.result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        measured = result.get("test_durations")
        if not isinstance(measured, dict):
            continue
        for identifier, duration in measured.items():
            if isinstance(identifier, str) and isinstance(duration, (int, float)):
                durations[identifier] = max(durations.get(identifier, 0.0), float(duration))
    return durations


def run_shards(
    shards: Sequence[TestShard],
    *,
    work_root: Path,
    jobs: int,
) -> list[ShardExecution]:
    resolved_work_root = work_root if work_root.is_absolute() else REPO_ROOT / work_root
    run_root = resolved_work_root.resolve() / f"run-{os.getpid()}-{time.time_ns()}"
    parallel = [shard for shard in shards if not shard.serial]
    serial = [shard for shard in shards if shard.serial]
    executions: list[ShardExecution] = []
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as executor:
        futures = {
            executor.submit(_run_shard, shard, run_root=run_root): shard for shard in parallel
        }
        for future in as_completed(futures):
            executions.append(future.result())
    for shard in serial:
        executions.append(_run_shard(shard, run_root=run_root))
    return sorted(executions, key=lambda execution: execution.shard.index)


def _execution_run_root(execution: ShardExecution) -> Path:
    return execution.result_path.parent.parent


def _failed_test_ids(execution: ShardExecution) -> list[str]:
    try:
        payload = json.loads(execution.result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return list(execution.shard.test_ids)
    identifiers = payload.get("failed_test_ids")
    if isinstance(identifiers, list) and all(isinstance(identifier, str) for identifier in identifiers):
        return identifiers or list(execution.shard.test_ids)
    return list(execution.shard.test_ids)


def _prune_work_runs(work_root: Path, *, preserve: Path | None = None) -> None:
    resolved = work_root if work_root.is_absolute() else REPO_ROOT / work_root
    try:
        runs = sorted(
            (path for path in resolved.iterdir() if path.is_dir() and path.name.startswith("run-")),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
    except OSError:
        return
    kept = 0
    for run in runs:
        if preserve is not None and run.resolve() == preserve.resolve():
            kept += 1
            continue
        if kept < FAILURE_RUN_LIMIT:
            kept += 1
            continue
        try:
            run.resolve().relative_to(resolved.resolve())
        except (OSError, ValueError):
            continue
        shutil.rmtree(run, ignore_errors=True)


def _focused_test_command(
    identifier: str,
    *,
    history_path: Path,
    work_root: Path,
    budget_path: Path,
) -> tuple[str, ...]:
    return (
        sys.executable,
        str(Path(__file__).resolve()),
        "--test",
        identifier,
        "--jobs",
        "1",
        "--history",
        str(history_path),
        "--work-root",
        str(work_root),
        "--budget-file",
        str(budget_path),
    )


def write_failure_summary(
    executions: Sequence[ShardExecution],
    findings: Sequence[BudgetFinding],
    *,
    history_path: Path,
    work_root: Path,
    budget_path: Path,
) -> Path | None:
    failed = [execution for execution in executions if execution.returncode != 0]
    if not failed and not findings:
        return None
    if failed:
        first_execution = failed[0]
    else:
        first_execution = next(
            execution
            for execution in executions
            if findings[0].identifier in execution.shard.test_ids
        )
    failed_identifiers = _failed_test_ids(first_execution) if failed else []
    first_identifier = failed_identifiers[0] if failed_identifiers else findings[0].identifier
    run_root = _execution_run_root(first_execution)
    rerun = _focused_test_command(
        first_identifier,
        history_path=history_path,
        work_root=work_root,
        budget_path=budget_path,
    )
    summary_path = run_root / "failure.json"
    summary_path.write_text(
        json.dumps(
            {
                "schema": 1,
                "failed_test": first_identifier,
                "failed_test_ids": failed_identifiers,
                "rerun_command": subprocess.list2cmdline(rerun),
                "seed": {"PYTHONHASHSEED": "0"},
                "temporary_artifact_location": str(first_execution.result_path.parent / "tmp"),
                "shard_result": str(first_execution.result_path),
                "history_path": str(history_path),
                "budget_path": str(budget_path),
                "budget_findings": [
                    {
                        "test": finding.identifier,
                        "kind": finding.kind,
                        "duration_seconds": finding.duration_seconds,
                        "budget_seconds": finding.budget_seconds,
                        "historical_p95_seconds": finding.historical_p95_seconds,
                    }
                    for finding in findings
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    _prune_work_runs(work_root, preserve=run_root)
    return summary_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", action="append", default=[])
    parser.add_argument("--start-directory", default="tests")
    parser.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    parser.add_argument("--work-root", type=Path, default=DEFAULT_WORK_ROOT)
    parser.add_argument("--budget-file", type=Path, default=DEFAULT_BUDGETS)
    parser.add_argument("--no-runtime-budgets", action="store_true")
    parser.add_argument(
        "--repeat-quarantine",
        type=int,
        default=0,
        metavar="COUNT",
        help="Run only configured repeat-only tests COUNT times outside the normal gate.",
    )
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--worker-shard", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--worker-result", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker_shard is not None:
        if args.worker_result is None:
            parser.error("--worker-result is required with --worker-shard")
        return _worker_main(args.worker_shard, args.worker_result)
    if args.jobs <= 0:
        parser.error("--jobs must be positive")
    validation_artifact_root = os.environ.get("B2R_VALIDATION_ARTIFACT_DIR")
    if validation_artifact_root and args.work_root == DEFAULT_WORK_ROOT:
        args.work_root = Path(validation_artifact_root) / "python-shards"
    if args.repeat_quarantine < 0:
        parser.error("--repeat-quarantine must not be negative")
    config = load_runtime_budget_config(args.budget_file)
    identifiers = discover_test_ids(
        args.test,
        start_directory=args.start_directory,
    )
    quarantine_ids = {
        identifier
        for identifier in identifiers
        if any(fnmatch.fnmatchcase(identifier, pattern) for pattern in config.repeat_only)
    }
    if args.repeat_quarantine:
        if args.test:
            parser.error("--repeat-quarantine cannot be combined with --test")
        identifiers = sorted(quarantine_ids)
        if not identifiers:
            print("No repeat-only quarantine tests are configured.")
            return 0
    else:
        identifiers = [identifier for identifier in identifiers if identifier not in quarantine_ids]
        if not identifiers:
            raise RuntimeError("all selected tests are repeat-only quarantine tests")
    duplicate_count = len(identifiers) - len(set(identifiers))
    identifiers = sorted(set(identifiers))
    if duplicate_count:
        print(f"Collapsed {duplicate_count} redundant unittest selections.", flush=True)
    historical_statistics = load_duration_statistics(args.history)
    durations = load_duration_history(args.history)
    shards = balance_test_ids(
        identifiers,
        jobs=args.jobs,
        durations=durations,
        shared_fixture_patterns=config.shared_fixture_groups,
    )
    if args.list:
        for shard in shards:
            kind = "serial" if shard.serial else "parallel"
            print(
                f"shard {shard.index:03d} {kind}: {len(shard.test_ids)} tests, "
                f"predicted {shard.predicted_seconds:.3f}s"
            )
            for identifier in shard.test_ids:
                print(f"  {identifier}")
        return 0
    print(
        f"Running {len(identifiers)} unittest cases in "
        f"{sum(not shard.serial for shard in shards)} parallel shards"
        + (
            f" plus {sum(shard.serial for shard in shards)} serial shard"
            if any(shard.serial for shard in shards)
            else ""
        ),
        flush=True,
    )
    repeat_count = args.repeat_quarantine or 1
    executions: list[ShardExecution] = []
    for iteration in range(repeat_count):
        if repeat_count > 1:
            print(f"Repeat-only quarantine iteration {iteration + 1}/{repeat_count}", flush=True)
        iteration_executions = run_shards(shards, work_root=args.work_root, jobs=args.jobs)
        executions.extend(iteration_executions)
        if any(execution.returncode != 0 for execution in iteration_executions):
            break
    update_duration_history(
        args.history,
        executions,
        sample_limit=config.history_sample_limit,
    )
    measured_durations = execution_test_durations(executions)
    failed = [execution for execution in executions if execution.returncode != 0]
    findings = (
        []
        if args.no_runtime_budgets
        else evaluate_runtime_budgets(
            measured_durations,
            history=historical_statistics,
            config=config,
        )
    )
    for execution in executions:
        if execution.returncode == 0:
            print(
                f"[passed] shard {execution.shard.index:03d}: "
                f"{len(execution.shard.test_ids)} tests in {execution.duration_seconds:.2f}s",
                flush=True,
            )
            continue
        print(
            f"[failed] shard {execution.shard.index:03d}: "
            f"{len(execution.shard.test_ids)} tests in {execution.duration_seconds:.2f}s\n"
            f"{execution.output.rstrip()}",
            flush=True,
        )
        for identifier in _failed_test_ids(execution):
            focused = _focused_test_command(
                identifier,
                history_path=args.history,
                work_root=args.work_root,
                budget_path=args.budget_file,
            )
            print(f"Rerun test: {subprocess.list2cmdline(focused)}", flush=True)
    for finding in findings:
        prefix = "budget-failed" if finding.kind == "budget" else "timing-regression"
        history_text = (
            f", historical p95 {finding.historical_p95_seconds:.3f}s"
            if finding.historical_p95_seconds is not None
            else ""
        )
        print(
            f"[{prefix}] {finding.identifier}: {finding.duration_seconds:.3f}s, "
            f"budget {finding.budget_seconds:.3f}s{history_text}",
            flush=True,
        )
    slowest = sorted(measured_durations.items(), key=lambda item: item[1], reverse=True)[:5]
    if slowest:
        print("Slowest unittest cases:", flush=True)
        for identifier, duration in slowest:
            updated = load_duration_statistics(args.history).get(identifier)
            percentile_text = (
                f" (p50 {updated.p50_seconds:.3f}s, p95 {updated.p95_seconds:.3f}s)"
                if updated is not None
                else ""
            )
            print(f"  {duration:.3f}s {identifier}{percentile_text}", flush=True)
    hard_findings = [finding for finding in findings if finding.kind == "budget"]
    failure_summary = write_failure_summary(
        executions,
        hard_findings,
        history_path=args.history,
        work_root=args.work_root,
        budget_path=args.budget_file,
    )
    if failure_summary is not None:
        print(f"B2R_TEST_FAILURE={failure_summary}", flush=True)
    else:
        run_roots = {_execution_run_root(execution) for execution in executions}
        for run_root in run_roots:
            resolved_work_root = (
                args.work_root if args.work_root.is_absolute() else REPO_ROOT / args.work_root
            )
            try:
                run_root.resolve().relative_to(resolved_work_root.resolve())
            except (OSError, ValueError):
                continue
            shutil.rmtree(run_root, ignore_errors=True)
    return 1 if failed or hard_findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
