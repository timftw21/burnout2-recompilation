from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.host.first_frame_smoke import (
    Toolchain,
    build_command,
    read_debug_events,
    summarize_smoke,
)


class FirstFrameSmokeTests(unittest.TestCase):
    def test_build_command_targets_windows_vulkan_dependencies(self) -> None:
        toolchain = Toolchain(
            clangxx=Path("C:/Program Files/LLVM/bin/clang++.exe"),
            vulkan_sdk=Path("C:/VulkanSDK/1.4.341.1"),
        )

        command = build_command(
            source=Path("runtime/host/vulkan_first_frame.cpp"),
            output=Path("build/local/first-frame/b2_first_frame.exe"),
            toolchain=toolchain,
        )

        self.assertIn("-std=c++17", command)
        self.assertIn("-DUNICODE", command)
        self.assertIn("-D_UNICODE", command)
        self.assertIn("-lvulkan-1", command)
        self.assertIn("-luser32", command)
        self.assertIn("-lgdi32", command)
        self.assertIn("-lshell32", command)
        normalized = [item.replace("\\", "/") for item in command]
        self.assertTrue(any(item.startswith("-IC:/VulkanSDK/1.4.341.1") for item in normalized))

    def test_reads_jsonl_debug_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "events.jsonl"
            path.write_text(
                '{"sequence":0,"event":"main_loop_enter"}\n'
                '{"sequence":1,"event":"frame_presented","frame":1}\n',
                encoding="utf-8",
            )

            events = read_debug_events(path)

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["event"], "main_loop_enter")
        self.assertEqual(events[1]["frame"], 1)

    def test_summarizes_first_frame_and_input_evidence(self) -> None:
        run_result = {
            "returncode": 0,
            "debug_json": "reports/local/first-frame/events.jsonl",
            "events": [
                {"sequence": 0, "event": "startup"},
                {"sequence": 1, "event": "physical_device_selected", "name": "GPU"},
                {"sequence": 2, "event": "main_loop_enter"},
                {"sequence": 3, "event": "input_event", "virtual_key": 32},
                {"sequence": 4, "event": "frame_presented", "frame": 1},
                {"sequence": 5, "event": "main_loop_exit"},
            ],
        }
        compile_result = {
            "returncode": 0,
            "output": "build/local/first-frame/b2_first_frame.exe",
        }

        summary = summarize_smoke(
            compile_result=compile_result,
            run_result=run_result,
        )

        self.assertEqual(summary["format"], "b2-recomp-first-frame-smoke")
        self.assertEqual(summary["target_platform"], "windows")
        self.assertEqual(summary["renderer_backend"], "vulkan")
        self.assertTrue(summary["build"]["compiled"])
        self.assertTrue(summary["run"]["main_loop_entered"])
        self.assertTrue(summary["run"]["main_loop_exited"])
        self.assertTrue(summary["run"]["visible_frame_presented"])
        self.assertEqual(summary["run"]["frames_presented"], 1)
        self.assertEqual(summary["run"]["input_events"], 1)
        self.assertEqual(summary["run"]["selected_device"]["name"], "GPU")


if __name__ == "__main__":
    unittest.main()
