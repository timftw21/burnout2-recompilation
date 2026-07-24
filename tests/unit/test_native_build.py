from __future__ import annotations

import unittest
from pathlib import Path

from tools.native_build import NativeTools, build_commands


class NativeBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tools = NativeTools(
            cmake=Path("pinned") / "cmake.exe",
            ctest=Path("pinned") / "ctest.exe",
            binary_directories=(Path("pinned"),),
        )

    def test_build_commands_configure_build_and_test_the_requested_preset(self) -> None:
        commands = build_commands(
            self.tools,
            preset="release",
            build_presenter=False,
            clean_first=False,
            skip_tests=False,
        )

        self.assertEqual(
            commands,
            [
                [str(self.tools.cmake), "--preset", "release"],
                [str(self.tools.cmake), "--build", "--preset", "release"],
                [str(self.tools.ctest), "--preset", "release"],
            ],
        )

    def test_build_commands_support_clean_build_without_tests(self) -> None:
        commands = build_commands(
            self.tools,
            preset="sanitizer",
            build_presenter=True,
            clean_first=True,
            skip_tests=True,
        )

        self.assertEqual(
            commands,
            [
                [
                    str(self.tools.cmake),
                    "--preset",
                    "sanitizer",
                    "-DB2R_BUILD_PRESENTER=ON",
                ],
                [
                    str(self.tools.cmake),
                    "--build",
                    "--preset",
                    "sanitizer",
                    "--clean-first",
                ],
            ],
        )


if __name__ == "__main__":
    unittest.main()
