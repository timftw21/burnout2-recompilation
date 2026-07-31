from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.python_test_shards import (
    DurationStatistics,
    RuntimeBudgetConfig,
    TestShard,
    _run_shard,
    balance_test_ids,
    discover_test_ids,
    evaluate_runtime_budgets,
    load_duration_history,
    load_duration_statistics,
    update_duration_history,
)


class PythonTestShardTests(unittest.TestCase):
    def test_discovery_returns_individual_test_case_ids(self) -> None:
        identifiers = discover_test_ids(("tests.unit.test_native_build",))

        self.assertGreaterEqual(len(identifiers), 3)
        self.assertTrue(
            all(
                identifier.startswith("tests.unit.test_native_build.NativeBuildTests.test_")
                for identifier in identifiers
            )
        )

    def test_balancing_uses_duration_and_keeps_every_case_once(self) -> None:
        identifiers = ("test.slow", "test.medium", "test.fast")
        shards = balance_test_ids(
            identifiers,
            jobs=2,
            durations={"test.slow": 10.0, "test.medium": 6.0, "test.fast": 4.0},
        )

        assigned = [identifier for shard in shards for identifier in shard.test_ids]
        self.assertEqual(sorted(assigned), sorted(identifiers))
        self.assertEqual([shard.predicted_seconds for shard in shards], [10.0, 10.0])

    def test_only_explicit_patterns_create_a_serial_shard(self) -> None:
        shards = balance_test_ids(
            ("test.parallel", "test.global_state"),
            jobs=2,
            durations={},
            serial_patterns=("*.global_state",),
        )

        self.assertEqual(sum(shard.serial for shard in shards), 1)
        self.assertEqual(
            next(shard.test_ids for shard in shards if shard.serial),
            ("test.global_state",),
        )

    def test_worker_captures_success_output_and_records_case_duration(self) -> None:
        identifier = (
            "tests.unit.test_native_build.NativeBuildTests."
            "test_build_commands_support_clean_build_without_tests"
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            execution = _run_shard(
                TestShard(0, (identifier,), 0.01),
                run_root=Path(temporary_directory),
            )
            payload = json.loads(execution.result_path.read_text(encoding="utf-8"))

            self.assertEqual(execution.returncode, 0)
            self.assertEqual(execution.output, "")
            self.assertIn(identifier, payload["test_durations"])

    def test_history_accumulates_worker_measurements(self) -> None:
        identifier = (
            "tests.unit.test_native_build.NativeBuildTests."
            "test_build_commands_support_clean_build_without_tests"
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            execution = _run_shard(TestShard(0, (identifier,), 0.01), run_root=root / "run")
            history = root / "history.json"

            update_duration_history(history, (execution,))

            self.assertGreaterEqual(load_duration_history(history)[identifier], 0.0)
            statistics = load_duration_statistics(history)[identifier]
            self.assertGreaterEqual(statistics.p50_seconds, 0.0)
            self.assertGreaterEqual(statistics.p95_seconds, statistics.p50_seconds)

    def test_worker_returns_captured_unittest_output_on_failure(self) -> None:
        identifier = "tests.unit.test_native_build.NativeBuildTests.test_does_not_exist"
        with tempfile.TemporaryDirectory() as temporary_directory:
            execution = _run_shard(
                TestShard(0, (identifier,), 0.01),
                run_root=Path(temporary_directory),
            )

            self.assertNotEqual(execution.returncode, 0)
            self.assertIn("FAILED", execution.output)
            self.assertTrue(execution.result_path.is_file())

    def test_shared_fixture_patterns_keep_expensive_cases_in_one_process(self) -> None:
        shards = balance_test_ids(
            ("suite.Shared.test_one", "suite.Shared.test_two", "suite.Other.test_one"),
            jobs=2,
            durations={
                "suite.Shared.test_one": 1.0,
                "suite.Shared.test_two": 1.0,
                "suite.Other.test_one": 1.0,
            },
            shared_fixture_patterns=("suite.Shared.*",),
        )

        shared_shards = [
            shard
            for shard in shards
            if "suite.Shared.test_one" in shard.test_ids
            or "suite.Shared.test_two" in shard.test_ids
        ]
        self.assertEqual(len(shared_shards), 1)
        self.assertIn("suite.Shared.test_one", shared_shards[0].test_ids)
        self.assertIn("suite.Shared.test_two", shared_shards[0].test_ids)

    def test_runtime_budgets_fail_hard_and_flag_historical_regressions(self) -> None:
        config = RuntimeBudgetConfig(
            default_test_seconds=2.0,
            history_sample_limit=8,
            minimum_samples=5,
            p95_ratio=1.5,
            minimum_delta_seconds=0.25,
        )
        history = {
            "test.regressed": DurationStatistics(5, 0.1, 0.1, 0.1, 0.1),
        }

        findings = evaluate_runtime_budgets(
            {"test.too_slow": 2.5, "test.regressed": 0.5},
            history=history,
            config=config,
        )

        self.assertEqual(
            {(finding.identifier, finding.kind) for finding in findings},
            {("test.too_slow", "budget"), ("test.regressed", "regression")},
        )


if __name__ == "__main__":
    unittest.main()
