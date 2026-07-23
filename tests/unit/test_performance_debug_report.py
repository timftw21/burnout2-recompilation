from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.playability.performance_debug_report import (
    _summarize_native_compilation,
    build_performance_debug_report,
)


class PerformanceDebugReportTests(unittest.TestCase):
    def test_native_compilation_counts_reused_executor_once(self) -> None:
        cache = {
            "executor_instance_id": "base-executor",
            "ahead_compiled_count": 84,
            "base_compiled_count": 84,
            "misses": 84,
            "compile_wall_us": 304_986_827,
        }
        execution = {
            "guest_thread_executions": [
                {
                    "thread_index": 0,
                    "native_runs": [
                        {"target": 0x1000, "native_module_cache": cache},
                        {"target": 0x2000, "native_module_cache": dict(cache)},
                    ],
                }
            ]
        }

        summary = _summarize_native_compilation(execution)

        self.assertEqual(summary["native_run_count"], 2)
        self.assertEqual(summary["compile_event_count"], 1)
        self.assertEqual(summary["compile_wall_us"], 304_986_827)
        self.assertEqual(summary["incremental_compile_wall_us"], 0)

    def test_background_promotion_compile_is_not_a_synchronous_stall(self) -> None:
        execution = {
            "guest_thread_executions": [
                {
                    "thread_index": 0,
                    "native_runs": [
                        {
                            "target": 0x2000,
                            "native_module_cache": {
                                "executor_instance_id": "base",
                            },
                        },
                        {
                            "target": 0,
                            "executor_role": "promoted_frontier",
                            "native_module_cache": {
                                "executor_instance_id": "promotion",
                                "ahead_compiled_count": 9,
                                "incremental_compiled_count": 9,
                                "compile_wall_us": 3_500_000,
                            },
                        },
                    ],
                }
            ]
        }

        summary = _summarize_native_compilation(execution)

        self.assertEqual(summary["incremental_compile_wall_us"], 3_500_000)
        self.assertEqual(summary["background_promotion_compile_wall_us"], 3_500_000)
        self.assertEqual(summary["synchronous_incremental_compile_wall_us"], 0)
        self.assertEqual(summary["incremental_frame_budget_stall_count"], 0)

    def test_report_flags_incremental_native_compilation_stalls(self) -> None:
        payload = {
            "format": "b2-recomp-playability-probe",
            "entry_recovery": {
                "execution": {
                    "status": "returned",
                    "guest_thread_executions": [
                        {
                            "thread_index": 0,
                            "native_frontier_interpreter": {
                                "enabled": True,
                                "invocation_count": 2,
                                "steps": 2161,
                                "total_us": 30_000,
                                "maximum_us": 25_000,
                                "compile_deferred_count": 2,
                                "compile_fallback_count": 0,
                                "budget_yield_count": 1,
                                "host_service_count": 1,
                                "host_service_coalesced_count": 1,
                                "instruction_budget": 100_000,
                            },
                            "native_frontier_promotion": {
                                "enabled": True,
                                "submission_count": 1,
                                "completion_count": 1,
                                "failure_count": 0,
                                "active_module_count": 1,
                                "active_address_count": 128,
                                "promoted_dispatch_count": 3,
                                "events": [
                                    {
                                        "status": "completed",
                                        "elapsed_us": 2_200_000,
                                        "compile_wall_us": 2_100_000,
                                    }
                                ],
                            },
                            "native_runs": [
                                {
                                    "reason": "unhandled_target",
                                    "target": 0x000D5987,
                                    "steps": 100,
                                    "performance": {"elapsed_us": 1000},
                                    "native_module_cache": {
                                        "hits": 84,
                                        "misses": 0,
                                        "ahead_compiled_count": 0,
                                        "compile_wall_us": 0,
                                    },
                                },
                                {
                                    "reason": "yield_handler_stop",
                                    "target": 0x002223FE,
                                    "steps": 200,
                                    "performance": {"elapsed_us": 2000},
                                    "native_module_cache": {
                                        "hits": 84,
                                        "misses": 1,
                                        "ahead_compiled_count": 1,
                                        "base_compiled_count": 0,
                                        "incremental_compiled_count": 1,
                                        "incremental_partition_count": 1,
                                        "incremental_instruction_count": 128,
                                        "incremental_max_partition_instruction_count": 128,
                                        "parallel_compile_workers": 1,
                                        "source_emit_us": 5000,
                                        "compiler_process_us": 2_000_000,
                                        "compile_wall_us": 2_100_000,
                                    },
                                },
                            ],
                        }
                    ],
                }
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "probe.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            report = build_performance_debug_report(path)

        compilation = report["boundaries"]["native_compilation"]
        self.assertEqual(compilation["incremental_compile_wall_us"], 2_100_000)
        self.assertEqual(compilation["incremental_frame_budget_stall_count"], 1)
        self.assertEqual(
            compilation["events"][0]["trigger_target_hex"],
            "0x000D5987",
        )
        self.assertEqual(report["throughput"]["native_compile_wall_us"], 2_100_000)
        frontier = report["boundaries"]["native_frontier_interpreter"]
        self.assertEqual(frontier["compile_deferred_count"], 2)
        self.assertEqual(frontier["budget_yield_count"], 1)
        self.assertEqual(frontier["host_service_coalesced_count"], 1)
        self.assertEqual(frontier["maximum_us"], 25_000)
        promotion = report["boundaries"]["native_frontier_promotion"]
        self.assertEqual(promotion["completion_count"], 1)
        self.assertEqual(promotion["promoted_dispatch_count"], 3)
        finding_ids = {finding["id"] for finding in report["findings"]}
        self.assertIn(
            "synchronous_native_frontier_compile_stalls_guest",
            finding_ids,
        )
        self.assertIn(
            "live_native_frontier_interpreter_exceeds_frame_budget",
            finding_ids,
        )

    def test_report_correlates_60_fps_target_and_profiled_boundaries(self) -> None:
        native_performance = {
            "elapsed_us": 2_000_000,
            "hot_path_profiling_enabled": True,
            "native_dispatch_count": 10,
            "native_module_call_count": 50_000,
            "handler_call_count": 100,
            "read_u32_callback_count": 2_000,
            "read_u8_callback_count": 0,
            "exact_read_u32_callback_count": 1_900,
            "exact_read_u8_callback_count": 0,
            "page_miss_read_u32_callback_count": 100,
            "page_miss_read_u8_callback_count": 0,
            "native_observed_write_count": 20_000,
            "native_observed_write_batch_count": 2_000,
            "selective_dirty_sync_call_count": 2_000,
            "selective_dirty_sync_no_match_count": 1_900,
            "observer_drain_deferred_count": 1_500,
            "empty_dependency_sync_bypass_count": 4_000,
            "memory_callback_policy_cache_hit_count": 100,
            "memory_callback_policy_cache_miss_count": 10,
            "observed_write_packet_yield_count": 12,
            "zero_read_callback_bypass_count": 8_000,
            "native_module_exit_profile": {
                "enabled": True,
                "reason_counts": {
                    "branch": 5_000,
                    "call": 10_000,
                    "return": 35_000,
                },
                "classified_module_calls": 50_000,
                "unclassified_module_calls": 0,
            },
            "native_module_edge_profile": {
                "enabled": True,
                "exact": True,
                "table_capacity": 131_072,
                "unique_edge_count": 1,
                "classified_module_calls": 50_000,
                "unclassified_module_calls": 0,
                "overflow_module_calls": 0,
                "edges": [
                    {
                        "entry_target": 0x1000,
                        "exit_target": 0x2000,
                        "module_calls": 50_000,
                        "module_exit_reasons": {"call": 50_000},
                    }
                ],
            },
            "timings": {
                "native_dispatch": {"count": 10, "total_us": 1_500_000, "max_us": 200_000},
                "native_dispatch_self": {"count": 10, "total_us": 50_000, "max_us": 8_000},
                "dirty_page_sync": {"count": 100, "total_us": 300_000, "max_us": 5_000},
            },
            "handler_hot_paths": [
                {
                    "target_hex": "0x00002000",
                    "name": "handler",
                    "count": 100,
                    "total_us": 100_000,
                    "max_us": 2_000,
                }
            ],
            "native_dispatch_hot_targets": [
                {
                    "target": 0x1000,
                    "module_calls": 50_000,
                    "guest_steps": 1_000_000,
                    "module_exit_reasons": {
                        "branch": 5_000,
                        "call": 10_000,
                        "return": 35_000,
                    },
                }
            ],
            "memory_callback_sampling": {
                "interval": 1024,
                "hot_addresses": [
                    {
                        "kind": "exact_u32",
                        "address": 0x2256B8,
                        "sample_count": 4,
                        "sampled_total_us": 80,
                        "sampled_max_us": 30,
                    }
                ],
            },
        }
        payload = {
            "format": "b2-recomp-playability-probe",
            "entry_recovery": {
                "execution": {
                    "status": "returned",
                    "guest_thread_executions": [
                        {
                            "thread_index": 0,
                            "status": "live_stop",
                            "native_runs": [
                                {
                                    "reason": "yield_handler_stop",
                                    "steps": 1_000_000,
                                    "performance": native_performance,
                                }
                            ],
                            "live_host_bridge": {
                                "recent_guest_flip_rate_hz": 30.0,
                                "recent_guest_flip_sample_count": 120,
                                "performance": {
                                    "metrics": {
                                        "presentation_ack_wait": {
                                            "count": 60,
                                            "total_us": 500_000,
                                            "max_us": 20_000,
                                        }
                                    },
                                    "hot_paths": [],
                                },
                            },
                        }
                    ],
                }
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "probe.json"
            render_path = Path(temp_dir) / "render.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            render_path.write_text(
                json.dumps(
                    {
                        "performance": {
                            "presenter": {
                                "reload_count": 60,
                                "reload_busy_ratio": 0.25,
                                "reload_stages": {
                                    "total_us": {
                                        "average_us": 20_000,
                                        "p95_us": 30_000,
                                    }
                                },
                                "hot_paths_by_total_time": [
                                    {"stage": "resource_update", "total_us": 500_000}
                                ],
                                "offscreen_target_lifecycle": {
                                    "instrumented_reload_count": 20,
                                    "target_reload_count": 20,
                                    "reused_reload_count": 2,
                                    "recreated_reload_count": 18,
                                    "reuse_ratio": 0.1,
                                },
                                "frame_pacing": {
                                    "frame_count": 120,
                                    "missed_target_ratio": 0.20,
                                    "target_frame_us": 16_667,
                                    "elapsed_us": {"p50_us": 16_000, "p95_us": 25_000},
                                },
                                "gpu_texture_conversion": {
                                    "batch_count": 1,
                                    "gpu_converted_textures": 90,
                                    "cpu_converted_textures": 2,
                                    "gpu_batch_us": {
                                        "average_us": 18_000,
                                        "p95_us": 18_000,
                                    },
                                    "setup_us": {"average_us": 12_000},
                                    "gpu_submission_us": {"average_us": 2_500},
                                    "validation": {
                                        "passed": True,
                                        "cpu_us": {"average_us": 3_500},
                                    },
                                },
                                "pipeline_compilation": {
                                    "graphics_pipeline_count": 12,
                                    "compute_pipeline_count": 1,
                                    "batched_graphics_event_count": 2,
                                    "pipeline_create_us": {
                                        "maximum_us": 20_000,
                                        "p95_us": 18_000,
                                    },
                                },
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            report = build_performance_debug_report(
                path,
                render_debug_report_path=render_path,
            )

        self.assertEqual(report["status"], "optimization_required")
        self.assertEqual(report["target"]["target_attainment_ratio"], 0.5)
        self.assertEqual(report["throughput"]["host_wait_us"], 500_000)
        self.assertEqual(
            report["boundaries"]["memory_callback_policy_cache_hit_ratio"],
            0.909091,
        )
        self.assertEqual(
            report["boundaries"]["empty_dependency_sync_bypass_ratio"],
            0.666667,
        )
        self.assertEqual(
            report["boundaries"]["counter_totals"][
                "zero_read_callback_bypass_count"
            ],
            8_000,
        )
        self.assertEqual(
            report["boundaries"]["counter_totals"][
                "observed_write_packet_yield_count"
            ],
            12,
        )
        self.assertEqual(
            report["sampled_hot_paths"]["native_dispatch_targets"][0]["target_hex"],
            "0x00001000",
        )
        self.assertEqual(
            report["boundaries"]["native_module_exit_profile"]["reason_counts"],
            {"branch": 5_000, "call": 10_000, "return": 35_000},
        )
        self.assertEqual(
            report["boundaries"]["native_module_exit_profile"][
                "unclassified_module_calls"
            ],
            0,
        )
        edge_profile = report["boundaries"]["native_module_edge_profile"]
        self.assertTrue(edge_profile["exact"])
        self.assertEqual(edge_profile["classified_module_calls"], 50_000)
        self.assertEqual(
            edge_profile["edges"][0]["entry_target_hex"],
            "0x00001000",
        )
        self.assertEqual(
            edge_profile["edges"][0]["exit_target_hex"],
            "0x00002000",
        )
        self.assertEqual(
            report["boundaries"]["native_dispatch_timing"],
            {
                "inclusive_us": 1_500_000,
                "self_us": 50_000,
                "compiled_module_and_call_us": 1_450_000,
                "self_ratio": 0.033333,
                "exclusive_measurement_available": True,
            },
        )
        self.assertEqual(
            report["sampled_hot_paths"]["native_dispatch_targets"][0][
                "module_exit_reasons"
            ]["return"],
            35_000,
        )
        self.assertEqual(
            report["sampled_hot_paths"]["memory_callbacks"]["hot_addresses"][0][
                "address_hex"
            ],
            "0x002256B8",
        )
        finding_ids = {finding["id"] for finding in report["findings"]}
        self.assertIn("guest_below_60_fps_target", finding_ids)
        self.assertIn("render_write_batches_are_fragmented", finding_ids)
        self.assertIn("presenter_frame_pacing_misses_target", finding_ids)
        self.assertIn("presenter_reload_is_hot", finding_ids)
        self.assertIn("offscreen_targets_are_recreated", finding_ids)
        self.assertIn(
            "gpu_texture_conversion_batch_exceeds_frame_budget", finding_ids
        )
        self.assertIn(
            "synchronous_pipeline_compile_exceeds_frame_budget", finding_ids
        )


if __name__ == "__main__":
    unittest.main()
