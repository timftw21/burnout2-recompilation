from __future__ import annotations

import unittest

from tests.unit.test_xbe_info import _synthetic_xbe
from tools.analysis.analysis_db import build_analysis_database_from_bytes


class AnalysisDatabaseTests(unittest.TestCase):
    def test_builds_schema_confidence_and_naming_conventions(self) -> None:
        blob, _ = _synthetic_xbe()
        database = build_analysis_database_from_bytes(blob, source_name="default.xbe")

        self.assertEqual(database["format"], "b2-recomp-analysis-db")
        self.assertEqual(database["schema_version"], 1)
        self.assertFalse(database["public_safe"])
        self.assertEqual(database["source"]["input_file_name"], "default.xbe")
        self.assertEqual(database["source"]["title_name"], "Burnout 2")
        self.assertIn("functions", database["naming_conventions"])
        self.assertIn("data", database["naming_conventions"])
        self.assertIn("vtables", database["naming_conventions"])
        self.assertIn("subsystems", database["naming_conventions"])
        self.assertEqual(
            set(database["confidence_levels"]),
            {"confirmed", "probable", "inferred", "placeholder"},
        )

    def test_seeds_records_from_sanitized_xbe_and_loader_metadata(self) -> None:
        blob, layout = _synthetic_xbe()
        database = build_analysis_database_from_bytes(blob, source_name="default.xbe")
        records_by_name = {record["name"]: record for record in database["records"]}

        self.assertEqual(
            records_by_name["entry_start"]["address"],
            layout["text_va"] + 0x20,
        )
        self.assertEqual(records_by_name["entry_start"]["confidence"], "probable")
        self.assertEqual(
            records_by_name[f"tls_callback_{layout['text_va'] + 0x24:08X}".lower()][
                "subsystem"
            ],
            "startup",
        )
        self.assertEqual(records_by_name["section_text"]["subsystem"], "code")
        self.assertEqual(records_by_name["section_data"]["subsystem"], "data")
        self.assertEqual(records_by_name["mem_text_00011000"]["kind"], "memory_region")
        self.assertEqual(records_by_name["lib_xboxkrnl"]["kind"], "library")
        self.assertEqual(records_by_name["lib_xapilib"]["subsystem"], "runtime")
        self.assertEqual(records_by_name["feature_d3d8"]["subsystem"], "rendering")
        self.assertEqual(
            records_by_name["imp_kernel_0065_iocreatedevice"]["subsystem"],
            "filesystem",
        )
        self.assertEqual(
            records_by_name["imp_kernel_0008_dbgprint"]["subsystem"],
            "diagnostics",
        )
        self.assertEqual(records_by_name["imp_xapilib_dll_0002"]["ordinal"], 2)

    def test_tracks_focus_areas_and_compiler_runtime_patterns(self) -> None:
        blob, _ = _synthetic_xbe()
        database = build_analysis_database_from_bytes(blob, source_name="default.xbe")

        focus_by_name = {
            record["name"]: record
            for record in database["records"]
            if record["kind"] == "analysis_focus"
        }
        self.assertEqual(
            set(focus_by_name),
            {
                "focus_startup_code",
                "focus_allocator_paths",
                "focus_filesystem_paths",
                "focus_rendering_setup",
                "focus_audio_setup",
                "focus_input_polling",
                "focus_main_loop",
            },
        )
        self.assertEqual(focus_by_name["focus_startup_code"]["confidence"], "confirmed")
        self.assertEqual(focus_by_name["focus_main_loop"]["confidence"], "placeholder")
        self.assertGreater(len(focus_by_name["focus_filesystem_paths"]["evidence"]), 0)

        patterns = {pattern["name"] for pattern in database["compiler_runtime_patterns"]}
        self.assertIn("xbe_entry_point", patterns)
        self.assertIn("tls_directory", patterns)
        self.assertIn("xdk_library_versions", patterns)
        self.assertIn("kernel_import_thunks", patterns)
        self.assertIn("non_kernel_import_descriptors", patterns)
        self.assertIn("section_zero_fill", patterns)
        self.assertIn("main_loop_pending", patterns)

        self.assertEqual(database["summary"]["focus_area_count"], 7)
        self.assertGreaterEqual(database["summary"]["record_count"], 20)


if __name__ == "__main__":
    unittest.main()
