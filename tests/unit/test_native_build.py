from __future__ import annotations

import unittest
from pathlib import Path

from tools.native_build import NativeTools, build_commands


class NativeBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tools = NativeTools(
            cmake=Path("pinned") / "cmake.exe",
            ctest=Path("pinned") / "ctest.exe",
            ninja=Path("pinned") / "ninja.exe",
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
                [str(self.tools.ninja), "-C", str(Path("build/cmake/release"))],
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
                    "-U",
                    "B2R_SDL3_*",
                    "-U",
                    "Vulkan_*",
                    "-DB2R_BUILD_PRESENTER=ON",
                ],
                [
                    str(self.tools.ninja),
                    "-C",
                    str(Path("build/cmake/sanitizer")),
                    "clean",
                ],
                [
                    str(self.tools.ninja),
                    "-C",
                    str(Path("build/cmake/sanitizer")),
                ],
            ],
        )

    def test_build_commands_can_target_and_filter_native_tests(self) -> None:
        commands = build_commands(
            self.tools,
            preset="debug",
            build_presenter=False,
            clean_first=False,
            skip_tests=False,
            targets=("b2r_host_core_tests",),
            test_regexes=("native\\.host_core\\.texture", "native\\.host_core\\.pipeline"),
            test_labels=("texture", "pipeline"),
            parallel=6,
        )

        self.assertEqual(
            commands,
            [
                [str(self.tools.cmake), "--preset", "debug"],
                [
                    str(self.tools.ninja),
                    "-C",
                    str(Path("build/cmake/debug")),
                    "b2r_host_core_tests",
                    "-j",
                    "6",
                ],
                [
                    str(self.tools.ctest),
                    "--preset",
                    "debug",
                    "--parallel",
                    "6",
                    "--tests-regex",
                    r"(native\.host_core\.texture)|(native\.host_core\.pipeline)",
                    "--label-regex",
                    "(texture)|(pipeline)",
                ],
            ],
        )

    def test_build_commands_support_independent_configure_and_build_phases(self) -> None:
        configure = build_commands(
            self.tools,
            preset="debug",
            build_presenter=False,
            clean_first=False,
            skip_tests=False,
            configure_only=True,
        )
        build = build_commands(
            self.tools,
            preset="debug",
            build_presenter=False,
            clean_first=False,
            skip_tests=False,
            targets=("b2r_native_tests",),
            build_only=True,
        )

        self.assertEqual(configure, [[str(self.tools.cmake), "--preset", "debug"]])
        self.assertEqual(
            build,
            [
                [
                    str(self.tools.ninja),
                    "-C",
                    str(Path("build/cmake/debug")),
                    "b2r_native_tests",
                ]
            ],
        )

    def test_configure_can_pin_a_compiler_launcher(self) -> None:
        commands = build_commands(
            self.tools,
            preset="release",
            build_presenter=False,
            clean_first=False,
            skip_tests=True,
            compiler_launcher=Path("tools") / "sccache.exe",
        )

        self.assertIn(
            f"-DCMAKE_CXX_COMPILER_LAUNCHER={Path('tools') / 'sccache.exe'}",
            commands[0],
        )


if __name__ == "__main__":
    unittest.main()
