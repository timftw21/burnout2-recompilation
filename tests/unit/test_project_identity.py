from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.unit.test_xbe_info import _synthetic_xbe
from tools.project_identity import (
    ANALYSIS_SCHEMA_VERSION,
    GENERATED_CODE_ABI_VERSION,
    RUNTIME_COMPATIBILITY_VERSION,
    ProjectIdentityError,
    inspect_xbe_identity,
    verify_supported_xbe,
)


class ProjectIdentityTests(unittest.TestCase):
    def _write_fixture(self, root: Path) -> tuple[Path, Path]:
        xbe_path = root / "default.xbe"
        payload, _layout = _synthetic_xbe()
        xbe_path.write_bytes(payload)
        identity = inspect_xbe_identity(xbe_path)
        target = {
            "target_id": "synthetic-test-xbe",
            "description": "redistributable synthetic unit-test XBE",
            "file_size": identity["file_size"],
            "file_sha256": identity["file_sha256"],
            "normalized_image_sha256": identity["normalized_image_sha256"],
            "header": identity["header"],
            "certificate": identity["certificate"],
            "sections": identity["sections"],
        }
        manifest_path = root / "supported-targets.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "tool_contract": {
                        "analysis_schema_version": ANALYSIS_SCHEMA_VERSION,
                        "generated_code_abi_version": GENERATED_CODE_ABI_VERSION,
                        "runtime_compatibility_version": RUNTIME_COMPATIBILITY_VERSION,
                    },
                    "targets": [target],
                }
            ),
            encoding="utf-8",
        )
        return xbe_path, manifest_path

    def test_exact_supported_xbe_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            xbe_path, manifest_path = self._write_fixture(Path(temp_dir))

            result = verify_supported_xbe(xbe_path, manifest_path=manifest_path)

        self.assertTrue(result["supported"])
        self.assertEqual(result["status"], "supported")
        self.assertEqual(result["target_id"], "synthetic-test-xbe")

    def test_changed_xbe_is_rejected_without_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            xbe_path, manifest_path = self._write_fixture(Path(temp_dir))
            payload = bytearray(xbe_path.read_bytes())
            payload[0x1100] ^= 0x01
            xbe_path.write_bytes(payload)

            with self.assertRaisesRegex(ProjectIdentityError, "unsupported XBE"):
                verify_supported_xbe(xbe_path, manifest_path=manifest_path)

    def test_changed_xbe_override_is_explicitly_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            xbe_path, manifest_path = self._write_fixture(Path(temp_dir))
            payload = bytearray(xbe_path.read_bytes())
            payload[0x1100] ^= 0x01
            xbe_path.write_bytes(payload)

            result = verify_supported_xbe(
                xbe_path,
                manifest_path=manifest_path,
                allow_unsupported=True,
            )

        self.assertFalse(result["supported"])
        self.assertTrue(result["override_used"])
        self.assertEqual(result["status"], "unsupported_override")
        self.assertTrue(result["mismatches"])


if __name__ == "__main__":
    unittest.main()
