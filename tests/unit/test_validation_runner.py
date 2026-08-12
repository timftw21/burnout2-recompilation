from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tools import validation_runner
from tools.validation_runner import (
    ValidationNode,
    ValidationResult,
    _dependency_result_key,
    run_validation_graph,
)


class ValidationRunnerTests(unittest.TestCase):
    def test_cache_reuses_exact_success_and_rebuilds_missing_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cache = root / "cache.json"
            (root / "input.txt").write_text("one", encoding="utf-8")
            node = ValidationNode(
                "write",
                (
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('output.txt').write_text('ok')",
                ),
                "write an output",
                inputs=("input.txt",),
                outputs=("output.txt",),
            )

            first = run_validation_graph({"write": node}, ["write"], cache_path=cache, root=root)
            second = run_validation_graph({"write": node}, ["write"], cache_path=cache, root=root)
            (root / "output.txt").unlink()
            third = run_validation_graph({"write": node}, ["write"], cache_path=cache, root=root)

            self.assertEqual(first.results["write"].status, "passed")
            self.assertEqual(second.results["write"].status, "cached")
            self.assertEqual(third.results["write"].status, "passed")
            self.assertIn("outputs are missing", third.results["write"].cache_reason)

    def test_input_change_explains_cache_miss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cache = root / "cache.json"
            input_path = root / "input.txt"
            input_path.write_text("one", encoding="utf-8")
            node = ValidationNode(
                "check",
                (sys.executable, "-c", "pass"),
                "check an input",
                inputs=("input.txt",),
            )

            run_validation_graph({"check": node}, ["check"], cache_path=cache, root=root)
            input_path.write_text("two", encoding="utf-8")
            changed = run_validation_graph({"check": node}, ["check"], cache_path=cache, root=root)

            self.assertEqual(changed.results["check"].status, "passed")
            self.assertIn("input:input.txt", changed.results["check"].cache_reason)

    def test_content_addressed_cache_retains_more_than_latest_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cache = root / "cache.json"
            input_path = root / "input.txt"
            node = ValidationNode(
                "check",
                (sys.executable, "-c", "pass"),
                "check an input",
                inputs=("input.txt",),
            )

            input_path.write_text("first", encoding="utf-8")
            run_validation_graph({"check": node}, ["check"], cache_path=cache, root=root)
            input_path.write_text("second", encoding="utf-8")
            run_validation_graph({"check": node}, ["check"], cache_path=cache, root=root)
            input_path.write_text("first", encoding="utf-8")
            restored = run_validation_graph({"check": node}, ["check"], cache_path=cache, root=root)

            self.assertEqual(restored.results["check"].status, "cached")

    def test_success_without_declared_output_fails_the_node(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            node = ValidationNode(
                "missing",
                (sys.executable, "-c", "pass"),
                "omit output",
                outputs=("expected.txt",),
            )

            summary = run_validation_graph(
                {"missing": node},
                ["missing"],
                cache_path=root / "cache.json",
                root=root,
            )

            self.assertFalse(summary.passed)
            self.assertIn("expected.txt", summary.results["missing"].output)

    def test_dependency_key_includes_output_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output = root / "generated.bin"
            node = ValidationNode(
                "producer",
                (sys.executable, "-c", "pass"),
                "producer",
                outputs=("generated.bin",),
            )
            result = ValidationResult("producer", "passed", 0, 0.1, "input-key", "run")
            output.write_bytes(b"first")
            first = _dependency_result_key(node, result, root=root)
            output.write_bytes(b"second")
            second = _dependency_result_key(node, result, root=root)

            self.assertNotEqual(first, second)

    def test_independent_nodes_start_concurrently(self) -> None:
        barrier = threading.Barrier(2)

        def fake_run(
            node: ValidationNode,
            **kwargs: object,
        ) -> ValidationResult:
            barrier.wait(timeout=2.0)
            return ValidationResult(
                node.name,
                "passed",
                0,
                0.01,
                str(kwargs["cache_key"]),
                str(kwargs["cache_reason"]),
            )

        nodes = {
            name: ValidationNode(name, (sys.executable, "-c", "pass"), name)
            for name in ("first", "second")
        }
        with (
            tempfile.TemporaryDirectory() as temporary_directory,
            mock.patch.object(validation_runner, "_run_subprocess", side_effect=fake_run),
        ):
            summary = run_validation_graph(
                nodes,
                list(nodes),
                cache_path=Path(temporary_directory) / "cache.json",
                root=Path(temporary_directory),
                jobs=2,
                no_cache=True,
            )

        self.assertTrue(summary.passed)

    def test_successes_survive_a_failed_run_and_resume_from_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cache = root / "cache.json"
            success = ValidationNode("success", (sys.executable, "-c", "pass"), "success")
            failure = ValidationNode(
                "failure",
                (sys.executable, "-c", "raise SystemExit(7)"),
                "failure",
            )
            first = run_validation_graph(
                {"success": success, "failure": failure},
                ("success", "failure"),
                cache_path=cache,
                root=root,
                jobs=2,
            )
            repaired = ValidationNode("failure", (sys.executable, "-c", "pass"), "repaired")
            second = run_validation_graph(
                {"success": success, "failure": repaired},
                ("success", "failure"),
                cache_path=cache,
                root=root,
                jobs=2,
            )

            self.assertFalse(first.passed)
            self.assertEqual(second.results["success"].status, "cached")
            self.assertEqual(second.results["failure"].status, "passed")

    def test_no_cache_does_not_read_or_write_cache_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cache = root / "cache.json"
            cache.write_text(json.dumps({"schema": 1, "entries": {}}), encoding="utf-8")
            before = cache.read_bytes()
            node = ValidationNode("check", (sys.executable, "-c", "pass"), "check")

            summary = run_validation_graph(
                {"check": node},
                ["check"],
                cache_path=cache,
                root=root,
                no_cache=True,
            )

            self.assertEqual(summary.results["check"].status, "passed")
            self.assertEqual(cache.read_bytes(), before)

    def test_runtime_budget_failure_writes_exact_bounded_triage_capsule(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "input.txt").write_text("fixture", encoding="utf-8")
            failure_root = root / "failures"
            node = ValidationNode(
                "slow",
                (sys.executable, "-c", "import time; time.sleep(0.03)"),
                "intentionally slow",
                inputs=("input.txt",),
                budget_seconds=0.001,
            )

            summary = run_validation_graph(
                {"slow": node},
                ("slow",),
                cache_path=root / "cache.json",
                root=root,
                no_cache=True,
                failure_root=failure_root,
            )

            self.assertFalse(summary.passed)
            self.assertEqual(summary.results["slow"].returncode, 124)
            self.assertIsNotNone(summary.failure_capsule)
            assert summary.failure_capsule is not None
            failure = json.loads(
                (summary.failure_capsule / "failure.json").read_text(encoding="utf-8")
            )
            self.assertEqual(failure["node"], "slow")
            self.assertEqual(failure["budget_seconds"], 0.001)
            self.assertIn("input:input.txt", failure["input_hashes"])
            self.assertIn("time.sleep", failure["rerun_command"])
            self.assertTrue((summary.failure_capsule / "rerun.ps1").is_file())

    def test_nested_validation_uses_inherited_artifact_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            inherited = Path(
                os.environ.get(
                    "B2R_VALIDATION_ARTIFACT_DIR",
                    str(root / "outer-artifacts"),
                )
            )
            node = ValidationNode(
                "failure",
                (sys.executable, "-c", "raise SystemExit(7)"),
                "failure",
            )

            summary = run_validation_graph(
                {"failure": node},
                ("failure",),
                cache_path=root / "cache.json",
                root=root,
                no_cache=True,
                environment={"B2R_VALIDATION_ARTIFACT_DIR": str(inherited)},
            )

            assert summary.failure_capsule is not None
            self.assertEqual(
                summary.failure_capsule.parent,
                inherited / "nested-failures",
            )

    def test_timing_history_retains_p50_and_p95(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            timing_path = root / "timings.json"
            node = ValidationNode("check", (sys.executable, "-c", "pass"), "check")

            run_validation_graph(
                {"check": node},
                ("check",),
                cache_path=root / "cache.json",
                root=root,
                no_cache=True,
                timing_path=timing_path,
            )

            payload = json.loads(timing_path.read_text(encoding="utf-8"))
            self.assertGreaterEqual(payload["nodes"]["check"]["p50_seconds"], 0.0)
            self.assertGreaterEqual(payload["nodes"]["check"]["p95_seconds"], 0.0)

    def test_structured_test_failure_promotes_single_test_rerun(self) -> None:
        script = (
            "import json, os; from pathlib import Path; "
            "p=Path(os.environ['B2R_VALIDATION_ARTIFACT_DIR'])/'test-failure.json'; "
            "p.write_text(json.dumps({'rerun_command':'python -m unittest exact.test'})); "
            "print('B2R_TEST_FAILURE='+str(p)); raise SystemExit(1)"
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            node = ValidationNode("tests", (sys.executable, "-c", script), "tests")
            summary = run_validation_graph(
                {"tests": node},
                ("tests",),
                cache_path=root / "cache.json",
                root=root,
                no_cache=True,
            )

            assert summary.failure_capsule is not None
            payload = json.loads(
                (summary.failure_capsule / "failure.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["rerun_command"], "python -m unittest exact.test")
            self.assertEqual(
                payload["structured_test_failure_payload"]["rerun_command"],
                "python -m unittest exact.test",
            )

    def test_duplicate_execution_contracts_are_rejected(self) -> None:
        command = (sys.executable, "-c", "pass")
        nodes = {
            "first": ValidationNode("first", command, "first", coverage_key="same-fixture"),
            "second": ValidationNode("second", command, "second", coverage_key="same-fixture"),
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with self.assertRaisesRegex(ValueError, "redundant validation nodes"):
                run_validation_graph(
                    nodes,
                    tuple(nodes),
                    cache_path=root / "cache.json",
                    root=root,
                    no_cache=True,
                )

    def test_closeout_cancellation_stops_unscheduled_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cancellation = root / "cancel.requested"
            cancellation.write_text("superseded\n", encoding="utf-8")
            node = ValidationNode(
                "must_not_run",
                (
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('unexpected').touch()",
                ),
                "cancelled node",
            )

            summary = run_validation_graph(
                {"must_not_run": node},
                ("must_not_run",),
                cache_path=root / "cache.json",
                root=root,
                no_cache=True,
                cancellation_path=cancellation,
            )

            self.assertEqual(summary.results["must_not_run"].status, "cancelled")
            self.assertFalse((root / "unexpected").exists())


if __name__ == "__main__":
    unittest.main()
