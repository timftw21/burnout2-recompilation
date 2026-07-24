from __future__ import annotations

import unittest
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tools.profiling.system_profile import (
    LIVE_TEST,
    SystemProfileError,
    _postprocess_etl,
    build_live_command,
    build_renderdoc_command,
    build_wpr_start_command,
)


class SystemProfileTests(unittest.TestCase):
    def test_wpr_plan_captures_cpu_and_gpu_in_file_mode(self) -> None:
        command = build_wpr_start_command(
            Path("wpr.exe"),
            Path("capture"),
            include_gpu=True,
        )

        self.assertEqual(
            command,
            [
                "wpr.exe",
                "-start",
                "CPU",
                "-start",
                "GPU",
                "-filemode",
                "-recordtempto",
                "capture",
            ],
        )

    def test_live_plan_is_diagnostics_off_and_warm_by_default(self) -> None:
        command = build_live_command(skip_host_build=True, live_arguments=[])

        self.assertEqual(
            command,
            [
                command[0],
                str(LIVE_TEST),
                "--no-diagnostics",
                "--skip-host-build",
            ],
        )

    def test_live_plan_rejects_in_process_hot_path_instrumentation(self) -> None:
        with self.assertRaisesRegex(SystemProfileError, "contaminates"):
            build_live_command(
                skip_host_build=True,
                live_arguments=["--profile-hot-paths"],
            )

    def test_live_plan_consumes_argument_separator(self) -> None:
        command = build_live_command(
            skip_host_build=True,
            live_arguments=["--", "--presentation-pipeline-depth", "2"],
        )

        self.assertNotIn("--", command)
        self.assertEqual(command[-2:], ["--presentation-pipeline-depth", "2"])

    def test_renderdoc_plan_launches_the_same_live_command(self) -> None:
        live_command = ["python.exe", "live_test.py", "--no-diagnostics"]
        command = build_renderdoc_command(
            Path("renderdoccmd.exe"),
            Path("capture") / "b2-frame",
            live_command,
        )

        self.assertEqual(command[0:2], ["renderdoccmd.exe", "capture"])
        self.assertIn("--wait-for-exit", command)
        self.assertEqual(command[-len(live_command) :], live_command)

    def test_xperf_utilization_uses_supported_default_interval(self) -> None:
        completed = CompletedProcess([], 0, stdout="", stderr="")
        with TemporaryDirectory() as temporary_directory:
            with patch(
                "tools.profiling.system_profile._completed_output",
                return_value=completed,
            ):
                reports = _postprocess_etl(
                    Path("xperf.exe"),
                    Path("capture.etl"),
                    Path(temporary_directory),
                )

        utilization = next(
            report for report in reports if report["name"] == "cpu-utilization.txt"
        )
        command = utilization["command"]
        self.assertIsInstance(command, list)
        assert isinstance(command, list)
        self.assertEqual(command[-2:], ["profile", "-util"])


if __name__ == "__main__":
    unittest.main()
