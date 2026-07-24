from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from tools import project_maintenance
from tools.project_maintenance import (
    FileRecord,
    delete_records,
    inspect_native_manifest,
    inventory_files,
    parse_byte_budget,
    retention_plan,
)


class ProjectMaintenanceTests(unittest.TestCase):
    def test_parse_byte_budget_accepts_binary_and_decimal_units(self) -> None:
        self.assertEqual(parse_byte_budget("1.5 GiB"), int(1.5 * 1024**3))
        self.assertEqual(parse_byte_budget("25MB"), 25_000_000)
        self.assertEqual(parse_byte_budget("4096"), 4096)

    def test_retention_plan_applies_age_count_and_bytes_without_protected_files(
        self,
    ) -> None:
        root = Path("C:/generated")
        records = [
            FileRecord(root / "protected.json", 50, 1),
            FileRecord(root / "expired.json", 50, 2),
            FileRecord(root / "middle.json", 40, 8),
            FileRecord(root / "newest.json", 30, 9),
        ]

        selected = retention_plan(
            records,
            now_ns=10,
            max_age_ns=5,
            max_files=2,
            max_bytes=100,
            protected_paths={root / "protected.json"},
        )

        self.assertEqual(
            {record.path.name for record in selected},
            {"expired.json", "middle.json"},
        )

    def test_inventory_and_delete_are_confined_to_managed_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "reports" / "local"
            nested = root / "capture" / "frame.bin"
            nested.parent.mkdir(parents=True)
            nested.write_bytes(b"frame")

            records = inventory_files(root)
            removed = delete_records(records, root=root)

            self.assertEqual(removed, {"removed_files": 1, "removed_bytes": 5})
            self.assertTrue(root.is_dir())
            self.assertFalse(nested.exists())
            self.assertFalse(nested.parent.exists())

    def test_delete_rejects_record_outside_managed_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "managed"
            root.mkdir()
            outside = base / "outside.bin"
            outside.write_bytes(b"x")
            record = FileRecord(outside.resolve(), 1, 0)

            with self.assertRaises(project_maintenance.MaintenanceError):
                delete_records([record], root=root)

            self.assertTrue(outside.exists())

    def test_native_manifest_inspection_is_read_only_and_reports_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "native-module-manifest.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.executescript(
                    """
                    CREATE TABLE cache_metadata (key TEXT PRIMARY KEY, value TEXT);
                    CREATE TABLE native_modules (
                        artifact_bytes INTEGER NOT NULL,
                        source_bytes INTEGER NOT NULL
                    );
                    INSERT INTO cache_metadata VALUES ('last_prune_ns', '123');
                    INSERT INTO native_modules VALUES (80, 20);
                    INSERT INTO native_modules VALUES (70, 30);
                    """
                )
                connection.commit()

            summary = inspect_native_manifest(
                path,
                max_artifacts=1,
                max_bytes=150,
            )

            self.assertEqual(summary["artifact_count"], 2)
            self.assertEqual(summary["artifact_bytes"], 150)
            self.assertEqual(summary["source_bytes"], 50)
            self.assertEqual(summary["last_prune_ns"], 123)
            self.assertTrue(summary["over_artifact_budget"])
            self.assertTrue(summary["over_byte_budget"])

    def test_clean_reports_is_dry_run_without_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "reports" / "local"
            report = root / "sample.json"
            report.parent.mkdir(parents=True)
            report.write_text("{}", encoding="utf-8")
            output = StringIO()

            with (
                patch.object(project_maintenance, "REPORT_ROOT", root),
                redirect_stdout(output),
            ):
                returncode = project_maintenance.main(["clean-reports"])

            summary = json.loads(output.getvalue())
            self.assertEqual(returncode, 0)
            self.assertFalse(summary["applied"])
            self.assertEqual(summary["roots"][0]["candidate_count"], 1)
            self.assertTrue(report.exists())


if __name__ == "__main__":
    unittest.main()
