from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.playability.render_debug_report import build_render_debug_report
from tools.playability.render_debug_suite import (
    inspect_render_snapshot_integrity,
    latest_retained_render_manifest,
    run_render_debug_suite,
)


class RenderDebugReportTests(unittest.TestCase):
    def test_debug_suite_detects_unpresented_trailing_snapshot_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            commands = root / "commands.bin"
            commands.write_bytes(b"B2APPND1" + bytes(16 * 4))
            manifest = root / "render.json"
            manifest.write_text(
                json.dumps(
                    {
                        "command_snapshot_path": str(commands),
                        "command_snapshot_record_count": 2,
                    }
                ),
                encoding="utf-8",
            )

            integrity = inspect_render_snapshot_integrity(manifest)

        self.assertEqual(integrity["status"], "contains_unpresented_records")
        self.assertFalse(integrity["accepted"])
        self.assertEqual(integrity["snapshot_record_count"], 4)
        self.assertEqual(integrity["trailing_record_count"], 2)

    def test_debug_suite_accepts_an_exact_snapshot_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            commands = root / "commands.bin"
            commands.write_bytes(b"B2APPND1" + bytes(16 * 3))
            manifest = root / "render.json"
            manifest.write_text(
                json.dumps(
                    {
                        "command_snapshot_path": str(commands),
                        "command_snapshot_record_count": 3,
                        "command_snapshot_exact_prefix": True,
                    }
                ),
                encoding="utf-8",
            )

            integrity = inspect_render_snapshot_integrity(manifest)

        self.assertEqual(integrity["status"], "exact")
        self.assertTrue(integrity["accepted"])
        self.assertTrue(integrity["exact_prefix"])

    def test_debug_suite_rejects_a_continuation_without_interpreter_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            commands = root / "commands.bin"
            commands.write_bytes(b"B2APPND1" + bytes(16 * 2))
            manifest = root / "render.json"
            manifest.write_text(
                json.dumps(
                    {
                        "command_snapshot_path": str(commands),
                        "command_snapshot_record_count": 2,
                        "command_snapshot_base_record_count": 100,
                        "standalone_replay_state_complete": False,
                    }
                ),
                encoding="utf-8",
            )

            integrity = inspect_render_snapshot_integrity(manifest)

        self.assertEqual(integrity["status"], "incomplete_state_history")
        self.assertFalse(integrity["accepted"])
        self.assertTrue(integrity["exact_prefix"])
        self.assertEqual(integrity["interpreter_bootstrap_status"], "missing")

    def test_debug_suite_accepts_a_valid_continuation_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            commands = root / "commands.bin"
            commands.write_bytes(b"B2APPND1" + bytes(16))
            bootstrap = root / "interpreter-bootstrap.bin"
            bootstrap.write_bytes(
                b"B2NVST01"
                + (1).to_bytes(4, "little")
                + (0x03A0).to_bytes(4, "little")
                + (0x0901).to_bytes(4, "little")
            )
            manifest = root / "render.json"
            manifest.write_text(
                json.dumps(
                    {
                        "command_snapshot_path": str(commands),
                        "command_snapshot_record_count": 1,
                        "command_snapshot_base_record_count": 100,
                        "standalone_replay_state_complete": True,
                        "interpreter_bootstrap_path": str(bootstrap),
                    }
                ),
                encoding="utf-8",
            )

            integrity = inspect_render_snapshot_integrity(manifest)

        self.assertEqual(integrity["status"], "exact")
        self.assertTrue(integrity["accepted"])
        self.assertEqual(integrity["interpreter_bootstrap_status"], "valid")
        self.assertEqual(integrity["interpreter_bootstrap_method_count"], 1)

    def test_debug_suite_discovers_latest_retained_f12_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "first" / "render.json"
            latest = root / "latest" / "render.json"
            first.parent.mkdir()
            latest.parent.mkdir()
            first.write_text("{}", encoding="utf-8")
            latest.write_text("{}", encoding="utf-8")
            events = root / "events.jsonl"
            events.write_text(
                "\n".join(
                    (
                        "not-json",
                        json.dumps(
                            {
                                "event": "hotkey_render_capture_retained",
                                "manifest": str(first),
                            }
                        ),
                        json.dumps(
                            {
                                "event": "hotkey_render_capture_retained",
                                "manifest": str(root / "missing" / "render.json"),
                            }
                        ),
                        json.dumps(
                            {
                                "event": "hotkey_render_capture_retained",
                                "manifest": str(latest),
                            }
                        ),
                    )
                )
                + "\n",
                encoding="utf-8",
            )

            selected = latest_retained_render_manifest(events)

        self.assertEqual(selected, latest)

    def test_debug_suite_rejects_a_missing_frozen_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "render.json"
            with self.assertRaisesRegex(FileNotFoundError, "manifest is missing"):
                run_render_debug_suite(
                    render_manifest=missing,
                    skip_build=True,
                )

    def test_debug_suite_surfaces_render_state_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest = root / "render.json"
            manifest.write_text("{}", encoding="utf-8")
            report = {
                "performance": {
                    "guest": {"counter_totals": {}},
                    "presenter": {},
                },
                "vertex_transforms": {"status": "healthy"},
                "render_state_coverage": {
                    "status": "healthy",
                    "state_history_complete": True,
                    "latest_event": {
                        "repeat_address_draw_count": 361,
                        "alpha_test_enabled_draw_count": 150,
                        "alpha_test_applied_draw_count": 150,
                        "texture_alpha_kill_enabled_draw_count": 198,
                        "texture_alpha_kill_applied_draw_count": 198,
                        "fixed_function_draw_count": 3,
                        "fixed_function_transformed_draw_count": 3,
                        "fixed_function_filtered_draw_count": 0,
                    },
                },
                "live_capture_coverage": {"status": "not_captured"},
                "render_target_feedback_cache": {"status": "not_captured"},
                "guest_transform_constant_provenance": {},
                "guest_world_matrix_provenance": {},
                "diagnostic_findings": [],
            }
            with (
                patch(
                    "tools.playability.render_debug_suite.run_first_frame",
                    return_value={"returncode": 0},
                ),
                patch(
                    "tools.playability.render_debug_suite.summarize_smoke",
                    return_value={
                        "run": {"render_stream_analysis_completed": True}
                    },
                ),
                patch(
                    "tools.playability.render_debug_suite.build_render_debug_report",
                    return_value=report,
                ),
                patch(
                    "tools.playability.render_debug_suite.write_render_debug_report"
                ),
            ):
                result = run_render_debug_suite(
                    render_manifest=manifest,
                    analysis_events=root / "analysis.jsonl",
                    analysis_summary=root / "analysis.json",
                    report_output=root / "report.json",
                    skip_build=True,
                )

        coverage = result["render_state_coverage"]
        self.assertEqual(result["format"], "b2-recomp-render-debug-suite-v10")
        self.assertEqual(coverage["repeat_address_draw_count"], 361)
        self.assertEqual(coverage["alpha_test_applied_draw_count"], 150)
        self.assertEqual(coverage["fixed_function_transformed_draw_count"], 3)

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

    def test_correlates_singular_world_matrix_source_from_guest_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe = root / "probe.json"
            events = root / "events.jsonl"
            probe.write_text(
                json.dumps(
                    {
                        "entry_recovery": {
                            "execution": {
                                "guest_thread_executions": [
                                    {
                                        "thread_index": 0,
                                        "title_world_matrix_audit": {
                                            "enabled": True,
                                            "invocation_count": 12,
                                            "singular_matrix_count": 4,
                                            "nonfinite_matrix_count": 0,
                                            "caller_counts": [
                                                {
                                                    "caller_return_address_hex": "0x000860C6",
                                                    "invocation_count": 12,
                                                }
                                            ],
                                            "source_records": [
                                                {
                                                    "caller_return_address_hex": "0x000860C6",
                                                    "source_address_hex": "0x10100D00",
                                                    "matrix_owner_candidate_hex": "0x10100000",
                                                    "invocation_count": 12,
                                                    "singular_matrix_count": 4,
                                                }
                                            ],
                                            "singular_samples": [
                                                {
                                                    "guest_flip_count": 1400,
                                                    "source_address_hex": "0x10100D00",
                                                    "singular": True,
                                                }
                                            ],
                                            "indexed_draw_audit": {
                                                "world_draw_callback_count": 12,
                                                "world_draw_callback_zero_index_count": 4,
                                                "indexed_draw_count": 20,
                                                "zero_index_draw_count": 4,
                                                "indexed_draw_caller_counts": [
                                                    {
                                                        "caller_return_address_hex": "0x000C4AA3",
                                                        "invocation_count": 12,
                                                        "zero_index_count": 4,
                                                    }
                                                ],
                                                "world_draw_callback_samples": [
                                                    {
                                                        "mesh_entry_address_hex": "0x10102000",
                                                        "index_count": 0,
                                                    }
                                                ],
                                                "indexed_draw_samples": [
                                                    {
                                                        "upstream_return_address_hex": "0x000C4AA3",
                                                        "index_count": 0,
                                                    }
                                                ],
                                            },
                                            "write_audit": {
                                                "enabled": True,
                                                "address_hex": "0x10100D00",
                                                "write_count": 3,
                                                "singularizing_write_count": 1,
                                                "restoring_write_count": 0,
                                                "instruction_counts": [
                                                    {
                                                        "instruction_address_hex": "0x00081234",
                                                        "write_count": 3,
                                                    }
                                                ],
                                                "write_samples": [],
                                                "transition_samples": [
                                                    {
                                                        "transition": "became_singular",
                                                        "instruction_address_hex": "0x00081234",
                                                    }
                                                ],
                                            },
                                            "rotation_audit": {
                                                "enabled": True,
                                                "matrix_address_hex": "0x10100D00",
                                                "entry_address_hex": "0x000EA0E0",
                                                "builder_address_hex": "0x000E9F30",
                                                "return_addresses_hex": [
                                                    "0x00085FF3",
                                                    "0x00086016",
                                                ],
                                                "entry_count": 2,
                                                "completion_count": 2,
                                                "pending_count": 0,
                                                "unmatched_completion_count": 0,
                                                "singular_input_count": 0,
                                                "singular_output_count": 1,
                                                "nonfinite_output_count": 0,
                                                "return_counts": [
                                                    {
                                                        "return_address_hex": "0x00085FF3",
                                                        "entry_count": 1,
                                                    },
                                                    {
                                                        "return_address_hex": "0x00086016",
                                                        "entry_count": 1,
                                                    },
                                                ],
                                                "stage_records": [
                                                    {
                                                        "return_address_hex": "0x00086016",
                                                        "input_singular": False,
                                                        "output_singular": True,
                                                        "completion_count": 1,
                                                    }
                                                ],
                                                "samples": [
                                                    {
                                                        "return_address_hex": "0x00086016",
                                                        "input": {"singular": False},
                                                        "output": {"singular": True},
                                                    }
                                                ],
                                            },
                                        },
                                    }
                                ]
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            events.write_text("", encoding="utf-8")

            report = build_render_debug_report(
                probe_summary_path=probe,
                presenter_events_path=events,
            )

        provenance = report["guest_world_matrix_provenance"]
        self.assertTrue(provenance["enabled"])
        self.assertEqual(provenance["singular_matrix_count"], 4)
        self.assertEqual(
            provenance["source_records"][0]["matrix_owner_candidate_hex"],
            "0x10100000",
        )
        self.assertIn(
            "singular_title_world_matrix_input",
            {finding["id"] for finding in report["diagnostic_findings"]},
        )
        self.assertEqual(
            provenance["indexed_draw_audit"]["zero_index_draw_count"], 4
        )
        self.assertIn(
            "zero_index_world_draw_submissions",
            {finding["id"] for finding in report["diagnostic_findings"]},
        )
        self.assertEqual(provenance["write_audit"]["write_count"], 3)
        self.assertEqual(
            provenance["write_audit"]["instruction_counts"][0][
                "instruction_address_hex"
            ],
            "0x00081234",
        )
        self.assertIn(
            "title_world_matrix_became_singular_on_write",
            {finding["id"] for finding in report["diagnostic_findings"]},
        )
        rotation_audit = provenance["rotation_audit"]
        self.assertEqual(rotation_audit["entry_count"], 2)
        self.assertEqual(rotation_audit["singular_input_count"], 0)
        self.assertEqual(rotation_audit["singular_output_count"], 1)
        self.assertIn(
            "title_world_matrix_rotation_output_singular",
            {finding["id"] for finding in report["diagnostic_findings"]},
        )

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
                            "zero_count_method_words": 8,
                            "zero_count_indexed_array_packets": 7,
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
        self.assertIn(
            "zero_count_indexed_array_packets",
            {finding["id"] for finding in report["diagnostic_findings"]},
        )

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

    def test_reports_render_state_translation_mismatches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe = root / "probe.json"
            events = root / "events.jsonl"
            analysis = root / "analysis.jsonl"
            probe.write_text("{}", encoding="utf-8")
            events.write_text("", encoding="utf-8")
            analysis.write_text(
                "\n".join(
                    json.dumps(event)
                    for event in (
                        {
                            "event": "nv2a_render_state_diagnostics",
                            "out_of_unit_texture_coordinate_draw_count": 2,
                            "sampler_address_mismatch_draw_count": 2,
                            "alpha_blend_enabled_draw_count": 4,
                            "alpha_test_enabled_draw_count": 1,
                            "alpha_test_applied_draw_count": 0,
                            "alpha_test_state_mismatch_draw_count": 1,
                            "fixed_function_draw_count": 2,
                            "fixed_function_transformed_draw_count": 0,
                            "fixed_function_raw_fallback_draw_count": 1,
                            "fixed_function_filtered_draw_count": 1,
                            "fixed_function_invalid_vertex_count": 27,
                            "analysis_state_complete": True,
                        },
                        {
                            "event": "nv2a_presented_transform_diagnostics",
                            "presented_index": 10,
                            "sampler_address_match": False,
                            "host_transform_path": "programmable",
                        },
                        {
                            "event": "nv2a_presented_transform_diagnostics",
                            "presented_index": 11,
                            "alpha_test_enable": 1,
                            "host_alpha_test_applied": False,
                            "host_transform_path": "programmable",
                        },
                        {
                            "event": "nv2a_presented_transform_diagnostics",
                            "presented_index": 12,
                            "host_transform_path": "filtered",
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )

            report = build_render_debug_report(
                probe_summary_path=probe,
                presenter_events_path=events,
                stream_analysis_events_path=analysis,
            )

        coverage = report["render_state_coverage"]
        self.assertEqual(coverage["status"], "translation_mismatch")
        self.assertEqual(coverage["mismatch_draw_count"], 5)
        self.assertEqual(coverage["mismatching_draws"][0]["presented_index"], 10)
        finding_ids = {finding["id"] for finding in report["diagnostic_findings"]}
        self.assertIn("texture_sampler_address_mode_mismatch", finding_ids)
        self.assertIn("alpha_test_state_not_applied", finding_ids)
        self.assertIn("fixed_function_draws_not_host_transformed", finding_ids)

    def test_supported_fixed_function_draws_are_not_reported_as_filtered(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe = root / "probe.json"
            events = root / "events.jsonl"
            probe.write_text("{}", encoding="utf-8")
            events.write_text(
                "\n".join(
                    (
                        json.dumps(
                            {
                                "event": "nv2a_vertex_transform_diagnostics",
                                "fixed_function_indexed_draw_count": 2,
                            }
                        ),
                        json.dumps(
                            {
                                "event": "nv2a_render_state_diagnostics",
                                "fixed_function_draw_count": 2,
                                "fixed_function_transformed_draw_count": 2,
                                "fixed_function_raw_fallback_draw_count": 0,
                                "fixed_function_filtered_draw_count": 0,
                                "fixed_function_invalid_vertex_count": 0,
                                "sampler_address_mismatch_draw_count": 0,
                                "alpha_test_state_mismatch_draw_count": 0,
                                "analysis_state_complete": True,
                            }
                        ),
                    )
                )
                + "\n",
                encoding="utf-8",
            )

            report = build_render_debug_report(
                probe_summary_path=probe,
                presenter_events_path=events,
                stream_analysis_events_path=None,
            )

        finding_ids = {finding["id"] for finding in report["diagnostic_findings"]}
        self.assertNotIn("fixed_function_draws_not_host_transformed", finding_ids)
        self.assertNotIn("unsupported_fixed_function_indexed_draw_filtered", finding_ids)

    def test_reports_aliased_gpu_target_until_offscreen_replay_is_covered(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe = root / "probe.json"
            events = root / "events.jsonl"
            probe.write_text("{}", encoding="utf-8")
            events.write_text(
                "\n".join(
                    json.dumps(event)
                    for event in (
                        {
                            "event": "nv2a_render_state_diagnostics",
                            "aliased_gpu_produced_texture_draw_count": 1,
                            "offscreen_render_target_replay_draw_count": 0,
                            "analysis_state_complete": True,
                        },
                        {
                            "event": "nv2a_presented_transform_diagnostics",
                            "presented_index": 843,
                            "texture_resource_payload_all_zero": True,
                            "texture_address_alias_applied": True,
                            "host_offscreen_target_replay_supported": False,
                            "texture_producer_draw_count": 6,
                            "texture_raw_producer_draw_count": 0,
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )

            report = build_render_debug_report(
                probe_summary_path=probe,
                presenter_events_path=events,
            )

        coverage = report["render_state_coverage"]
        self.assertEqual(
            coverage["aliased_gpu_produced_draws"][0]["presented_index"],
            843,
        )
        finding_ids = {finding["id"] for finding in report["diagnostic_findings"]}
        self.assertIn("aliased_gpu_target_not_replayed", finding_ids)

    def test_covered_aliased_gpu_target_is_not_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe = root / "probe.json"
            events = root / "events.jsonl"
            probe.write_text("{}", encoding="utf-8")
            events.write_text(
                json.dumps(
                    {
                        "event": "nv2a_render_state_diagnostics",
                        "aliased_gpu_produced_texture_draw_count": 1,
                        "offscreen_render_target_replay_draw_count": 1,
                        "analysis_state_complete": True,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            report = build_render_debug_report(
                probe_summary_path=probe,
                presenter_events_path=events,
            )

        finding_ids = {finding["id"] for finding in report["diagnostic_findings"]}
        self.assertNotIn("aliased_gpu_target_not_replayed", finding_ids)

    def test_reports_retained_f12_capture_and_isolated_offscreen_depth(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe = root / "probe.json"
            events = root / "events.jsonl"
            probe.write_text("{}", encoding="utf-8")
            events.write_text(
                "\n".join(
                    json.dumps(event)
                    for event in (
                        {
                            "event": "nv2a_offscreen_render_targets_created",
                            "count": 1,
                            "dedicated_depth_count": 1,
                            "shares_presented_depth": False,
                        },
                        {
                            "event": "hotkey_render_capture_retained",
                            "manifest": "capture/render.json",
                            "command_snapshot_copied": True,
                            "resource_snapshot_copied": True,
                        },
                        {
                            "event": "frame_readback_captured",
                            "trigger": "f12",
                            "render_capture_manifest": "capture/render.json",
                            "pixel_fingerprint": 1234,
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )

            report = build_render_debug_report(
                probe_summary_path=probe,
                presenter_events_path=events,
            )

        coverage = report["live_capture_coverage"]
        self.assertEqual(coverage["status"], "capture_retained")
        self.assertEqual(coverage["retained_capture_count"], 1)
        self.assertTrue(coverage["offscreen_depth_isolated"])
        finding_ids = {finding["id"] for finding in report["diagnostic_findings"]}
        self.assertNotIn("offscreen_target_shares_presented_depth", finding_ids)
        self.assertNotIn("hotkey_render_capture_not_retained", finding_ids)

    def test_reports_render_target_feedback_cache_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe = root / "probe.json"
            events = root / "events.jsonl"
            probe.write_text("{}", encoding="utf-8")
            events.write_text(
                "\n".join(
                    json.dumps(event)
                    for event in (
                        {
                            "event": "nv2a_native_resources_created",
                            "render_target_feedback_textures": 3,
                            "render_target_feedback_pruned": 0,
                        },
                        {
                            "event": "nv2a_render_state_diagnostics",
                            "render_target_feedback_required": 2,
                            "render_target_feedback_required_addresses": [
                                0x01D94000,
                                0x22298080,
                            ],
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )

            report = build_render_debug_report(
                probe_summary_path=probe,
                presenter_events_path=events,
            )

        cache = report["render_target_feedback_cache"]
        self.assertEqual(cache["status"], "mismatch")
        self.assertEqual(cache["active_count"], 3)
        self.assertEqual(cache["required_count"], 2)
        self.assertEqual(cache["stale_count"], 1)
        finding_ids = {finding["id"] for finding in report["diagnostic_findings"]}
        self.assertIn("render_target_feedback_cache_mismatch", finding_ids)

    def test_reports_healthy_render_target_feedback_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe = root / "probe.json"
            events = root / "events.jsonl"
            probe.write_text("{}", encoding="utf-8")
            events.write_text(
                json.dumps(
                    {
                        "event": "nv2a_native_resources_created",
                        "render_target_feedback_textures": 2,
                        "render_target_feedback_required": 2,
                        "render_target_feedback_missing": 0,
                        "render_target_feedback_stale": 0,
                        "render_target_feedback_pruned": 1,
                        "render_target_feedback_required_addresses": [1, 2],
                        "render_target_feedback_active_addresses": [1, 2],
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            report = build_render_debug_report(
                probe_summary_path=probe,
                presenter_events_path=events,
            )

        cache = report["render_target_feedback_cache"]
        self.assertEqual(cache["status"], "healthy")
        self.assertEqual(cache["pruned_count"], 1)
        finding_ids = {finding["id"] for finding in report["diagnostic_findings"]}
        self.assertNotIn("render_target_feedback_cache_mismatch", finding_ids)

    def test_reports_shared_offscreen_depth_and_unretained_f12_capture(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe = root / "probe.json"
            events = root / "events.jsonl"
            probe.write_text("{}", encoding="utf-8")
            events.write_text(
                "\n".join(
                    json.dumps(event)
                    for event in (
                        {
                            "event": "nv2a_offscreen_render_targets_created",
                            "count": 1,
                            "dedicated_depth_count": 0,
                            "shares_presented_depth": True,
                        },
                        {
                            "event": "frame_readback_captured",
                            "trigger": "f12",
                            "render_capture_manifest": None,
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )

            report = build_render_debug_report(
                probe_summary_path=probe,
                presenter_events_path=events,
            )

        finding_ids = {finding["id"] for finding in report["diagnostic_findings"]}
        self.assertIn("offscreen_target_shares_presented_depth", finding_ids)
        self.assertIn("hotkey_render_capture_not_retained", finding_ids)

    def test_merges_frozen_transform_analysis_and_ranks_performance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe = root / "native-live.json"
            events = root / "events.jsonl"
            analysis = root / "render-stream-analysis.jsonl"
            probe.write_text(
                json.dumps(
                    {
                        "entry_recovery": {
                            "execution": {
                                "native_run": {
                                    "steps": 1_000_000,
                                    "performance": {
                                        "elapsed_us": 1_000_000,
                                        "dirty_sync_call_count": 100,
                                        "dirty_sync_no_work_count": 80,
                                        "dirty_page_writeback_count": 20,
                                        "page_cache_fill_count": 2_000,
                                        "read_u32_callback_count": 15_000,
                                        "read_u8_callback_count": 5_000,
                                        "exact_read_u32_callback_count": 12_000,
                                        "exact_read_u8_callback_count": 3_000,
                                        "timings": {
                                            "native_dispatch": {
                                                "count": 4,
                                                "total_us": 600_000,
                                                "max_us": 200_000,
                                            },
                                            "dirty_page_sync": {
                                                "count": 20,
                                                "total_us": 150_000,
                                                "max_us": 20_000,
                                            }
                                        },
                                    },
                                },
                                "render_watchpoint_stream": {
                                    "transform_constant_upload_provenance": {
                                        "status": "captured",
                                        "matrix_upload_count": 12,
                                        "near_zero_basis_matrix_upload_count": 8,
                                        "near_zero_producer_instruction_counts": [
                                            {
                                                "instruction_address_hex": "0x00123456",
                                                "matrix_upload_count": 8,
                                            }
                                        ],
                                    }
                                },
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            events.write_text("", encoding="utf-8")
            analysis.write_text(
                json.dumps(
                    {
                        "event": "nv2a_vertex_transform_diagnostics",
                        "indexed_program_draw_count": 20,
                        "indexed_program_vertex_count": 11_323,
                        "subpixel_indexed_program_draw_count": 16,
                        "tiny_indexed_program_draw_count": 20,
                        "fixed_function_indexed_draw_count": 1,
                        "invalid_program_vertex_count": 0,
                        "indexed_program_output_span_x": 12.9,
                        "indexed_program_output_span_y": 4.9,
                        "indexed_program_width_coverage": 0.02,
                        "indexed_program_height_coverage": 0.01,
                        "indexed_program_tiny_coverage_suspected": True,
                        "viewport_constants_valid": True,
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "event": "nv2a_render_state_diagnostics",
                        "fixed_function_draw_count": 1,
                        "fixed_function_transformed_draw_count": 0,
                        "fixed_function_raw_fallback_draw_count": 1,
                        "fixed_function_filtered_draw_count": 1,
                        "fixed_function_invalid_vertex_count": 27,
                        "sampler_address_mismatch_draw_count": 0,
                        "alpha_test_state_mismatch_draw_count": 0,
                        "analysis_state_complete": True,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            report = build_render_debug_report(
                probe_summary_path=probe,
                presenter_events_path=events,
                stream_analysis_events_path=analysis,
            )

        self.assertEqual(report["format"], "b2-recomp-render-debug-report-v12")
        self.assertEqual(report["status"], "render_stream_analysis_only")
        self.assertEqual(
            report["vertex_transforms"]["status"],
            "indexed_program_tiny_coverage",
        )
        guest = report["performance"]["guest"]
        self.assertEqual(guest["dirty_sync_no_work_ratio"], 0.8)
        self.assertEqual(guest["dirty_page_sync_time_ratio"], 0.15)
        self.assertEqual(guest["page_cache_fills_per_million_steps"], 2_000.0)
        self.assertEqual(guest["exact_read_callback_ratio"], 0.75)
        self.assertEqual(guest["timing_hot_paths"][0]["name"], "native_dispatch")
        finding_ids = {finding["id"] for finding in report["diagnostic_findings"]}
        self.assertIn("indexed_program_world_geometry_tiny_coverage", finding_ids)
        self.assertIn("fixed_function_draws_not_host_transformed", finding_ids)
        self.assertIn("dirty_page_sync_hot_path", finding_ids)
        self.assertIn("high_native_page_cache_refill_rate", finding_ids)
        self.assertIn("high_native_memory_read_callback_rate", finding_ids)
        self.assertEqual(
            report["guest_transform_constant_provenance"]["status"],
            "captured",
        )


if __name__ == "__main__":
    unittest.main()
