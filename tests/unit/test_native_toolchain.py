from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.native_toolchain import (
    DEFAULT_LOCK,
    _clang_version_and_target,
    load_toolchain_lock,
    validate_native_toolchain,
)


class NativeToolchainTests(unittest.TestCase):
    def test_clang_version_and_target_parser(self) -> None:
        version, target = _clang_version_and_target(
            "clang version 22.1.8\nTarget: x86_64-pc-windows-msvc\n"
        )

        self.assertEqual(version, "22.1.8")
        self.assertEqual(target, "x86_64-pc-windows-msvc")

    def test_pinned_python_build_tools_validate(self) -> None:
        report = validate_native_toolchain(
            include_build_tools=True,
            include_presenter_tools=False,
        )

        self.assertTrue(report["passed"], report["errors"])

    def test_build_tool_version_mismatch_is_rejected(self) -> None:
        lock = load_toolchain_lock(DEFAULT_LOCK)
        lock["python_build_tools"]["cmake"] = "0.0.0"
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "native-toolchain.lock.json"
            lock_path.write_text(json.dumps(lock), encoding="utf-8")
            report = validate_native_toolchain(
                lock_path=lock_path,
                include_build_tools=True,
                include_presenter_tools=False,
            )

        self.assertFalse(report["passed"])
        self.assertIn("cmake version", " ".join(report["errors"]))


if __name__ == "__main__":
    unittest.main()
