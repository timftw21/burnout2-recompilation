import json
import tempfile
import unittest
from pathlib import Path

from tools.playability.command_work_cache_trace import (
    build_report,
    layout_id,
    partition_segment,
    profile_capture_reloads,
    simulate_configuration,
)


class CommandWorkCacheTraceTests(unittest.TestCase):
    def test_embedded_capture_state_scopes_reload_simulation(self) -> None:
        reloads = [
            {"reload": 1, "profile_capture_state": "armed"},
            {"reload": 2, "profile_capture_state": "active"},
            {"reload": 3, "profile_capture_state": "active"},
            {"reload": 4, "profile_capture_state": "complete"},
        ]

        selected, scope = profile_capture_reloads(reloads)

        self.assertEqual([item["reload"] for item in selected], [2, 3])
        self.assertEqual(scope["source"], "trace_profile_capture_state")

    def test_capture_simulation_warms_cache_from_prefix(self) -> None:
        reloads = [
            {"reload": 1, "epoch": "stable", "segments": [[{"address": 0, "size": 4}]]},
            {"reload": 2, "epoch": "stable", "segments": [[{"address": 0, "size": 4}]]},
        ]

        result = simulate_configuration(
            reloads,
            window_bytes=4096,
            plan_limit=1,
            measured_reload_numbers={2},
        )

        self.assertEqual(result["lookup_count"], 1)
        self.assertEqual(result["hit_count"], 1)
        self.assertEqual(result["build_count"], 0)

    def test_partition_uses_shift_invariant_bounded_layouts(self) -> None:
        layouts = partition_segment(
            [{"address": 0x2000, "size": 20 * 1024}],
            16 * 1024,
        )

        self.assertEqual(
            layouts,
            [((0, 16 * 1024),), ((0, 4 * 1024),)],
        )
        self.assertEqual(layout_id(layouts[0]), layout_id(((0, 16 * 1024),)))

    def test_capacity_matrix_exposes_reuse_beyond_one_plan(self) -> None:
        header = {
            "event": "command_work_cache_trace_start",
            "schema_version": 1,
            "runtime_window_bytes": 16 * 1024,
            "runtime_plan_limit": 256,
            "runtime_byte_limit": 64 * 1024 * 1024,
        }
        layouts = (
            [{"address": 0, "size": 4096}],
            [
                {"address": 0, "size": 2048},
                {"address": 2048, "size": 2048},
            ],
            [{"address": 0, "size": 4096}],
        )
        reloads = []
        for index, segment in enumerate(layouts, 1):
            runtime_layout = partition_segment(segment, 16 * 1024)[0]
            reloads.append(
                {
                    "event": "command_work_cache_reload",
                    "schema_version": 1,
                    "reload": index,
                    "epoch": "stable",
                    "cacheable": True,
                    "epoch_changed": index == 1,
                    "segments": [segment],
                    "windows": [
                        {
                            "layout_id": layout_id(runtime_layout),
                            "plan_bytes": 100 + index,
                            "hit": index == 3,
                            "retained": True,
                            "reuse_distance": 1 if index == 3 else None,
                            "evictions": [],
                        }
                    ],
                }
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            trace = Path(temp_dir) / "trace.jsonl"
            trace.write_text(
                "\n".join(json.dumps(item) for item in (header, *reloads)) + "\n",
                encoding="utf-8",
            )
            report = build_report(
                trace,
                window_kib=(4,),
                plan_limits=(1, 2),
            )

        by_limit = {
            item["plan_limit"]: item for item in report["simulations"]
        }
        self.assertEqual(by_limit[1]["hit_count"], 0)
        self.assertEqual(by_limit[1]["eviction_count"], 2)
        self.assertEqual(by_limit[2]["hit_count"], 1)
        self.assertEqual(by_limit[2]["eviction_count"], 0)
        self.assertEqual(report["captured"]["hit_count"], 1)
        self.assertEqual(report["captured"]["reuse_distance"]["p95"], 1)


if __name__ == "__main__":
    unittest.main()
