from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.playability.render_debug_report import build_render_debug_report


class RenderDebugReportTests(unittest.TestCase):
    def test_frame_pacing_uses_microsecond_60_hz_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe = root / "probe.json"
            events = root / "events.jsonl"
            probe.write_text("{}", encoding="utf-8")
            events.write_text(
                "\n".join(
                    json.dumps(
                        {
                            "event": "frame_presented",
                            "elapsed_us": elapsed_us,
                            "target_frame_us": 16_667,
                        }
                    )
                    for elapsed_us in (16_500, 17_250)
                )
                + "\n",
                encoding="utf-8",
            )

            report = build_render_debug_report(
                probe_summary_path=probe,
                presenter_events_path=events,
            )

        pacing = report["performance"]["presenter"]["frame_pacing"]
        self.assertEqual(pacing["target_frame_us"], 16_667)
        self.assertEqual(pacing["missed_target_count"], 1)
        self.assertEqual(pacing["elapsed_us"]["maximum_us"], 17_250)

    def test_correlates_guest_vertex_producer_with_host_collapsed_draw(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe = root / "native-live.json"
            events = root / "events.jsonl"
            probe.write_text(
                json.dumps(
                    {
                        "entry_recovery": {
                            "execution": {
                                "title_vertex_append_fast_path": {
                                    "invocation_count": 12,
                                    "geometry_bounds": {
                                        "min_x": 20.0,
                                        "max_x": 320.0,
                                        "min_y": 0.0,
                                        "max_y": 120.0,
                                    },
                                    "anomaly_counts": {"exact_center_origin": 4},
                                    "producer_callers": [
                                        {
                                            "caller_return_address_hex": "0x000C22E1",
                                            "invocation_count": 8,
                                            "anomaly_count": 4,
                                            "anomaly_counts": {"exact_center_origin": 4},
                                            "min_x": 320.0,
                                            "max_x": 320.0,
                                            "min_y": 0.0,
                                            "max_y": 0.0,
                                        }
                                    ],
                                    "geometry_by_guest_flip": [
                                        {
                                            "guest_flip_count": 7,
                                            "vertex_count": 12,
                                            "min_x": 0.0,
                                            "max_x": 640.0,
                                            "min_y": 0.0,
                                            "max_y": 480.0,
                                        }
                                    ],
                                    "anomalous_samples": [
                                        {
                                            "invocation_count": 9,
                                            "caller_return_address_hex": "0x000C22E1",
                                            "guest_flip_count": 7,
                                            "render_write_count": 110,
                                            "stack_code_candidates": [
                                                {
                                                    "stack_offset_hex": "0x00000050",
                                                    "address_hex": "0x000B8B44",
                                                }
                                            ],
                                            "frame_chain": [
                                                {
                                                    "depth": 0,
                                                    "return_address_hex": "0x000B8A10",
                                                }
                                            ],
                                        }
                                    ],
                                },
                                "title_d3d_primitive_draw_fast_path": {
                                    "invocation_count": 2,
                                    "execution_mode": "return_only_fast_path",
                                    "caller_return_counts": [],
                                    "sampled_invocations": [],
                                },
                                "title_immediate_draw_audit": {
                                    "draw_address_hex": "0x000EE1B0",
                                    "execution_mode": "observer_only_recovered_guest_code",
                                    "invocation_count": 1,
                                    "geometry_by_guest_flip": [
                                        {
                                            "guest_flip_count": 7,
                                            "draw_call_count": 1,
                                            "vertex_count": 4,
                                            "min_x": 0.0,
                                            "max_x": 640.0,
                                            "min_y": 0.0,
                                            "max_y": 480.0,
                                        }
                                    ],
                                },
                                "title_text_draw_fast_path": {
                                    "invocation_count": 1,
                                    "sampled_strings": [
                                        {
                                            "invocation_count": 1,
                                            "bytes_hex": "504C4159",
                                            "guest_flip_count": 7,
                                            "render_write_count": 110,
                                        }
                                    ],
                                },
                                "guest_thread_executions": [
                                    {
                                        "thread_index": 0,
                                        "status": "step_budget",
                                        "native_run": {
                                            "reason": "step_budget",
                                            "steps": 100,
                                            "performance": {
                                                "elapsed_us": 10,
                                                "steps_per_second": 10000000.0,
                                            },
                                        },
                                        "live_host_bridge": {
                                            "performance": {
                                                "metrics": {
                                                    "on_slice_total": {
                                                        "count": 1,
                                                        "total_us": 5,
                                                        "max_us": 5,
                                                        "average_us": 5.0,
                                                    }
                                                }
                                            }
                                        },
                                    }
                                ],
                            }
                        },
                        "runtime_abi_bridge": {
                            "invocation_count": 3,
                            "failed_caller_counts": [],
                        },
                    }
                ),
                encoding="utf-8",
            )
            events.write_text(
                "\n".join(
                    json.dumps(event)
                    for event in [
                        {
                            "event": "nv2a_presented_geometry_anomalies",
                            "reload": 4,
                            "source_commands": 120,
                            "guest_flips": 2,
                            "manifest_guest_flip_count": 7,
                            "presented_draw_count": 5,
                            "presented_vertex_count": 18,
                            "fullscreen_draw_count": 0,
                            "invalid_geometry_range_count": 0,
                            "non_finite_position_draw_count": 0,
                            "collapsed_x_draw_count": 1,
                            "collapsed_y_draw_count": 1,
                            "zero_area_draw_count": 1,
                            "exact_center_origin_draw_count": 1,
                            "fully_offscreen_draw_count": 0,
                            "outside_viewport_draw_count": 0,
                            "overall_min_x": 20.0,
                            "overall_max_x": 320.0,
                            "overall_min_y": 0.0,
                            "overall_max_y": 120.0,
                            "anomalous": True,
                        },
                        {
                            "event": "nv2a_native_resources_created",
                            "draws": 6,
                            "presented_draws": 5,
                            "textures": 2,
                        },
                        {
                            "event": "d3d8_stream_interpreted",
                            "clear_color_valid": True,
                            "clear_color_argb": 0,
                            "native_draws": 5,
                        },
                        {
                            "event": "render_validation",
                            "textured_presented_draw_count": 4,
                            "unmatched_presented_texture_draw_count": 0,
                            "unsupported_presented_primitive_count": 0,
                            "unsupported_draw_arrays_count": 0,
                            "passed": True,
                        },
                        {
                            "event": "frame_readback_captured",
                            "manifest_guest_flip_count": 7,
                            "output": "frame.bmp",
                            "width": 640,
                            "height": 480,
                            "pixel_count": 307200,
                            "unique_colors": 10,
                            "dominant_rgba": 0,
                            "dominant_count": 300000,
                        },
                        {
                            "event": "live_render_stream_reloaded",
                            "reload": 4,
                            "manifest_guest_flip_count": 7,
                            "guest_flips": 7,
                            "writes": 120,
                            "presentable_command_record_count": 120,
                            "interpreted_source_commands": 120,
                            "exact_completed_flip": True,
                            "wait_us": 1,
                            "command_load_us": 2,
                            "interpret_us": 30,
                            "resource_update_us": 4,
                            "command_record_us": 5,
                            "total_us": 42,
                        },
                        {"event": "frame_presented", "elapsed_ms": 17},
                        {"event": "frame_presented", "elapsed_ms": 10},
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            report = build_render_debug_report(
                probe_summary_path=probe,
                presenter_events_path=events,
            )

        self.assertEqual(report["status"], "correlated_guest_and_host_geometry")
        self.assertEqual(
            report["ranked_malformed_vertex_producers"][0][
                "caller_return_address_hex"
            ],
            "0x000C22E1",
        )
        self.assertEqual(
            report["guest_host_correlations"][0]["host_reload"],
            4,
        )
        self.assertEqual(report["host_geometry"]["peak_counts"]["zero_area_draw_count"], 1)
        self.assertEqual(
            report["ranked_upstream_stack_code_candidates"][0]["address_hex"],
            "0x000B8B44",
        )
        self.assertEqual(report["performance"]["guest"]["native_run_count"], 1)
        self.assertIsNotNone(report["performance"]["guest"]["live_host_bridge"])
        self.assertEqual(
            report["performance"]["presenter"]["hot_paths_by_total_time"][0][
                "stage"
            ],
            "interpret",
        )
        self.assertEqual(
            report["performance"]["presenter"]["frame_pacing"][
                "missed_target_count"
            ],
            1,
        )
        issue_ids = {
            issue["id"] for issue in report["composition_coverage"]["issues"]
        }
        self.assertIn(
            "guest_submitted_fullscreen_geometry_missing_on_host", issue_ids
        )
        self.assertIn("dominant_black_or_near_solid_readback", issue_ids)
        self.assertIn(
            "black_clear_without_fullscreen_background_draw", issue_ids
        )
        self.assertIn(
            "guest_clear_replaced_by_return_only_fast_path",
            issue_ids,
        )
        self.assertNotIn("guest_draw_submission_audit_missing", issue_ids)
        self.assertNotIn("manifest_flip_identity_missing", issue_ids)
        self.assertNotIn("host_completed_flip_replay_inexact", issue_ids)
        self.assertNotIn("current_presented_flip_readback_missing", issue_ids)
        self.assertIsNotNone(report["guest_host_flip_timeline"][0]["host_geometry"])

    def test_flags_inexact_replay_without_reusing_an_old_readback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe = root / "native-live.json"
            events = root / "events.jsonl"
            probe.write_text(
                json.dumps(
                    {
                        "entry_recovery": {
                            "execution": {
                                "title_vertex_append_fast_path": {
                                    "geometry_by_guest_flip": [
                                        {
                                            "guest_flip_count": 6,
                                            "vertex_count": 4,
                                            "min_x": 0.0,
                                            "max_x": 640.0,
                                            "min_y": 0.0,
                                            "max_y": 480.0,
                                        }
                                    ]
                                },
                                "title_d3d_primitive_draw_fast_path": {
                                    "execution_mode": "recovered_guest_code",
                                    "invocation_count": 1,
                                },
                                "title_immediate_draw_audit": {
                                    "draw_address_hex": "0x000EE1B0",
                                    "geometry_by_guest_flip": [
                                        {
                                            "guest_flip_count": 6,
                                            "vertex_count": 4,
                                            "min_x": 0.0,
                                            "max_x": 640.0,
                                            "min_y": 0.0,
                                            "max_y": 480.0,
                                        }
                                    ],
                                },
                                "guest_thread_executions": [
                                    {
                                        "thread_index": 0,
                                        "native_run": {
                                            "reason": "yield_handler_stop",
                                            "steps": 50,
                                            "performance": {"elapsed_us": 100},
                                        },
                                    }
                                ],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            events.write_text(
                "\n".join(
                    json.dumps(event)
                    for event in [
                        {
                            "event": "frame_readback_captured",
                            "manifest_guest_flip_count": 1,
                            "width": 640,
                            "height": 480,
                            "pixel_count": 307200,
                            "dominant_rgba": 0,
                            "dominant_count": 307000,
                            "output": "old.bmp",
                        },
                        {
                            "event": "nv2a_presented_geometry_anomalies",
                            "reload": 2,
                            "manifest_guest_flip_count": 7,
                            "presented_draw_count": 1,
                            "fullscreen_draw_count": 0,
                            "overall_min_x": 20.0,
                            "overall_max_x": 300.0,
                            "overall_min_y": 20.0,
                            "overall_max_y": 200.0,
                        },
                        {
                            "event": "live_render_stream_reloaded",
                            "reload": 2,
                            "manifest_guest_flip_count": 7,
                            "guest_flips": 2,
                            "writes": 1000,
                            "presentable_command_record_count": 1000,
                            "interpreted_source_commands": 100,
                            "total_us": 10,
                        },
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            report = build_render_debug_report(
                probe_summary_path=probe,
                presenter_events_path=events,
            )

        issue_ids = {
            issue["id"] for issue in report["composition_coverage"]["issues"]
        }
        self.assertIn("host_completed_flip_replay_inexact", issue_ids)
        self.assertIn("current_presented_flip_readback_missing", issue_ids)
        self.assertNotIn("dominant_black_or_near_solid_readback", issue_ids)
        self.assertEqual(
            report["composition_coverage"]["latest_guest_geometry"][
                "guest_flip_count"
            ],
            6,
        )
        self.assertEqual(report["performance"]["guest"]["native_run_count"], 1)


if __name__ == "__main__":
    unittest.main()
