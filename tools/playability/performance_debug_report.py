#!/usr/bin/env python3
"""Build a focused 60 FPS guest hot-path report from a live probe summary."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.playability.render_debug_report import _summarize_guest_performance

DEFAULT_PROBE_SUMMARY = (
    REPO_ROOT / "reports" / "local" / "playability" / "native-live.json"
)
DEFAULT_REPORT = (
    REPO_ROOT / "reports" / "local" / "playability" / "performance-debug-report.json"
)
TARGET_FPS = 60.0
TARGET_ACCEPTANCE_FPS = 59.9
TARGET_FRAME_US = round(1_000_000 / TARGET_FPS)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _metric_total(metrics: dict[str, Any], *names: str) -> int:
    return sum(_as_int((metrics.get(name) or {}).get("total_us")) for name in names)


def _summarize_native_compilation(execution: dict[str, Any]) -> dict[str, Any]:
    compilations: list[dict[str, Any]] = []
    native_run_count = 0
    for thread in execution.get("guest_thread_executions", []):
        if not isinstance(thread, dict):
            continue
        runs = thread.get("native_runs")
        if not isinstance(runs, list):
            run = thread.get("native_run")
            runs = [run] if isinstance(run, dict) else []
        previous_target: int | None = None
        seen_executor_keys: set[str] = set()
        for run_index, run in enumerate(runs):
            if not isinstance(run, dict):
                continue
            native_run_count += 1
            cache = run.get("native_module_cache") or {}
            executor_instance_id = cache.get("executor_instance_id")
            executor_key = (
                str(executor_instance_id)
                if executor_instance_id
                else json.dumps(cache, sort_keys=True, separators=(",", ":"))
            )
            compile_wall_us = _as_int(cache.get("compile_wall_us"))
            compiled_count = _as_int(cache.get("ahead_compiled_count"))
            misses = _as_int(cache.get("misses"))
            first_executor_run = executor_key not in seen_executor_keys
            seen_executor_keys.add(executor_key)
            if first_executor_run and (compile_wall_us or compiled_count or misses):
                trigger_target = previous_target if run_index > 0 else None
                compilations.append(
                    {
                        "thread_index": _as_int(thread.get("thread_index")),
                        "run_index": run_index,
                        "phase": (
                            "initial_native_build"
                            if len(seen_executor_keys) == 1
                            else "frontier_promotion"
                        ),
                        "executor_instance_id": executor_instance_id,
                        "executor_role": run.get("executor_role"),
                        "trigger_target": trigger_target,
                        "trigger_target_hex": (
                            f"0x{trigger_target & 0xFFFFFFFF:08X}"
                            if trigger_target is not None
                            else None
                        ),
                        "compile_wall_us": compile_wall_us,
                        "compiler_process_us": _as_int(
                            cache.get("compiler_process_us")
                        ),
                        "source_emit_us": _as_int(cache.get("source_emit_us")),
                        "compiled_module_count": compiled_count,
                        "base_compiled_count": _as_int(
                            cache.get("base_compiled_count")
                        ),
                        "incremental_compiled_count": _as_int(
                            cache.get("incremental_compiled_count")
                        ),
                        "incremental_partition_count": _as_int(
                            cache.get("incremental_partition_count")
                        ),
                        "incremental_instruction_count": _as_int(
                            cache.get("incremental_instruction_count")
                        ),
                        "incremental_max_partition_instruction_count": _as_int(
                            cache.get("incremental_max_partition_instruction_count")
                        ),
                        "parallel_compile_workers": _as_int(
                            cache.get("parallel_compile_workers")
                        ),
                        "cache_hits": _as_int(cache.get("hits")),
                        "cache_misses": misses,
                    }
                )
            target = run.get("target")
            previous_target = _as_int(target) if target is not None else None

    incremental = [
        item for item in compilations if item["phase"] != "initial_native_build"
    ]
    background_promotions = [
        item
        for item in incremental
        if item.get("executor_role") == "promoted_frontier"
    ]
    synchronous_incremental = [
        item for item in incremental if item not in background_promotions
    ]
    slow_incremental = [
        item
        for item in synchronous_incremental
        if item["compile_wall_us"] > TARGET_FRAME_US
    ]
    return {
        "native_run_count": native_run_count,
        "compile_event_count": len(compilations),
        "compiled_module_count": sum(
            item["compiled_module_count"] for item in compilations
        ),
        "compile_wall_us": sum(item["compile_wall_us"] for item in compilations),
        "initial_compile_wall_us": sum(
            item["compile_wall_us"]
            for item in compilations
            if item["phase"] == "initial_native_build"
        ),
        "incremental_compile_wall_us": sum(
            item["compile_wall_us"] for item in incremental
        ),
        "synchronous_incremental_compile_wall_us": sum(
            item["compile_wall_us"] for item in synchronous_incremental
        ),
        "background_promotion_compile_wall_us": sum(
            item["compile_wall_us"] for item in background_promotions
        ),
        "maximum_compile_wall_us": max(
            (item["compile_wall_us"] for item in compilations),
            default=0,
        ),
        "maximum_incremental_compile_wall_us": max(
            (item["compile_wall_us"] for item in incremental),
            default=0,
        ),
        "maximum_synchronous_incremental_compile_wall_us": max(
            (item["compile_wall_us"] for item in synchronous_incremental),
            default=0,
        ),
        "maximum_background_promotion_compile_wall_us": max(
            (item["compile_wall_us"] for item in background_promotions),
            default=0,
        ),
        "incremental_frame_budget_stall_count": len(slow_incremental),
        "events": sorted(
            compilations,
            key=lambda item: item["compile_wall_us"],
            reverse=True,
        ),
    }


def _summarize_native_frontier_interpreter(
    execution: dict[str, Any],
) -> dict[str, Any]:
    rows = []
    for thread in execution.get("guest_thread_executions", []):
        if not isinstance(thread, dict):
            continue
        summary = thread.get("native_frontier_interpreter")
        if not isinstance(summary, dict):
            continue
        rows.append(
            {
                "thread_index": _as_int(thread.get("thread_index")),
                "enabled": bool(summary.get("enabled")),
                "invocation_count": _as_int(summary.get("invocation_count")),
                "steps": _as_int(summary.get("steps")),
                "total_us": _as_int(summary.get("total_us")),
                "maximum_us": _as_int(summary.get("maximum_us")),
                "compile_deferred_count": _as_int(
                    summary.get("compile_deferred_count")
                ),
                "compile_fallback_count": _as_int(
                    summary.get("compile_fallback_count")
                ),
                "budget_yield_count": _as_int(
                    summary.get("budget_yield_count")
                ),
                "wall_budget_yield_count": _as_int(
                    summary.get("wall_budget_yield_count")
                ),
                "wall_budget_us": _as_int(summary.get("wall_budget_us")),
                "host_service_count": _as_int(
                    summary.get("host_service_count")
                ),
                "host_service_coalesced_count": _as_int(
                    summary.get("host_service_coalesced_count")
                ),
                "hot_targets": summary.get("hot_targets") or [],
                "instruction_budget": _as_int(
                    summary.get("instruction_budget")
                ),
            }
        )
    invocation_count = sum(item["invocation_count"] for item in rows)
    total_us = sum(item["total_us"] for item in rows)
    return {
        "enabled": any(item["enabled"] for item in rows),
        "thread_count": len(rows),
        "invocation_count": invocation_count,
        "steps": sum(item["steps"] for item in rows),
        "total_us": total_us,
        "maximum_us": max((item["maximum_us"] for item in rows), default=0),
        "average_us": round(total_us / max(1, invocation_count), 3),
        "compile_deferred_count": sum(
            item["compile_deferred_count"] for item in rows
        ),
        "compile_fallback_count": sum(
            item["compile_fallback_count"] for item in rows
        ),
        "budget_yield_count": sum(
            item["budget_yield_count"] for item in rows
        ),
        "wall_budget_yield_count": sum(
            item["wall_budget_yield_count"] for item in rows
        ),
        "wall_budget_us": max(
            (item["wall_budget_us"] for item in rows),
            default=0,
        ),
        "host_service_count": sum(
            item["host_service_count"] for item in rows
        ),
        "host_service_coalesced_count": sum(
            item["host_service_coalesced_count"] for item in rows
        ),
        "hot_targets": sorted(
            (
                {**target, "thread_index": item["thread_index"]}
                for item in rows
                for target in item["hot_targets"]
                if isinstance(target, dict)
            ),
            key=lambda target: (
                -_as_int(target.get("total_us")),
                -_as_int(target.get("invocation_count")),
            ),
        )[:128],
        "threads": rows,
    }


def _summarize_native_frontier_promotion(
    execution: dict[str, Any],
) -> dict[str, Any]:
    rows = []
    for thread in execution.get("guest_thread_executions", []):
        if not isinstance(thread, dict):
            continue
        summary = thread.get("native_frontier_promotion")
        if not isinstance(summary, dict):
            continue
        rows.append(
            {
                "thread_index": _as_int(thread.get("thread_index")),
                "enabled": bool(summary.get("enabled")),
                "submission_count": _as_int(summary.get("submission_count")),
                "completion_count": _as_int(summary.get("completion_count")),
                "failure_count": _as_int(summary.get("failure_count")),
                "pending": bool(summary.get("pending")),
                "requested_module_count": _as_int(
                    summary.get("requested_module_count")
                ),
                "active_module_count": _as_int(
                    summary.get("active_module_count")
                ),
                "active_address_count": _as_int(
                    summary.get("active_address_count")
                ),
                "promoted_dispatch_count": _as_int(
                    summary.get("promoted_dispatch_count")
                ),
                "events": summary.get("events") or [],
            }
        )
    events = [
        {**event, "thread_index": row["thread_index"]}
        for row in rows
        for event in row["events"]
        if isinstance(event, dict)
    ]
    return {
        "enabled": any(row["enabled"] for row in rows),
        "thread_count": len(rows),
        "submission_count": sum(row["submission_count"] for row in rows),
        "completion_count": sum(row["completion_count"] for row in rows),
        "failure_count": sum(row["failure_count"] for row in rows),
        "pending": any(row["pending"] for row in rows),
        "requested_module_count": sum(
            row["requested_module_count"] for row in rows
        ),
        "active_module_count": sum(row["active_module_count"] for row in rows),
        "active_address_count": sum(
            row["active_address_count"] for row in rows
        ),
        "promoted_dispatch_count": sum(
            row["promoted_dispatch_count"] for row in rows
        ),
        "compile_wall_us": sum(
            _as_int(event.get("compile_wall_us")) for event in events
        ),
        "maximum_build_elapsed_us": max(
            (_as_int(event.get("elapsed_us")) for event in events),
            default=0,
        ),
        "events": events[-64:],
        "threads": rows,
    }


def _findings(
    guest: dict[str, Any],
    bridge: dict[str, Any],
    presenter: dict[str, Any],
    native_compilation: dict[str, Any],
    native_frontier_interpreter: dict[str, Any],
    native_frontier_promotion: dict[str, Any],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    maximum_incremental_compile_us = _as_int(
        native_compilation.get("maximum_synchronous_incremental_compile_wall_us")
    )
    if maximum_incremental_compile_us > TARGET_FRAME_US:
        findings.append(
            {
                "id": "synchronous_native_frontier_compile_stalls_guest",
                "severity": (
                    "critical"
                    if maximum_incremental_compile_us >= 1_000_000
                    else "high"
                ),
                "evidence": {
                    "incremental_compile_wall_us": _as_int(
                        native_compilation.get(
                            "synchronous_incremental_compile_wall_us"
                        )
                    ),
                    "maximum_incremental_compile_wall_us": (
                        maximum_incremental_compile_us
                    ),
                    "incremental_frame_budget_stall_count": _as_int(
                        native_compilation.get(
                            "incremental_frame_budget_stall_count"
                        )
                    ),
                    "target_frame_us": TARGET_FRAME_US,
                    "largest_events": native_compilation.get("events", [])[:8],
                },
            }
        )
    maximum_frontier_interpreter_us = _as_int(
        native_frontier_interpreter.get("maximum_us")
    )
    if maximum_frontier_interpreter_us > TARGET_FRAME_US:
        findings.append(
            {
                "id": "live_native_frontier_interpreter_exceeds_frame_budget",
                "severity": (
                    "high"
                    if maximum_frontier_interpreter_us >= 100_000
                    else "warning"
                ),
                "evidence": {
                    "maximum_us": maximum_frontier_interpreter_us,
                    "average_us": native_frontier_interpreter.get("average_us"),
                    "invocation_count": _as_int(
                        native_frontier_interpreter.get("invocation_count")
                    ),
                    "steps": _as_int(native_frontier_interpreter.get("steps")),
                    "compile_deferred_count": _as_int(
                        native_frontier_interpreter.get(
                            "compile_deferred_count"
                        )
                    ),
                    "compile_fallback_count": _as_int(
                        native_frontier_interpreter.get(
                            "compile_fallback_count"
                        )
                    ),
                    "budget_yield_count": _as_int(
                        native_frontier_interpreter.get("budget_yield_count")
                    ),
                    "wall_budget_yield_count": _as_int(
                        native_frontier_interpreter.get(
                            "wall_budget_yield_count"
                        )
                    ),
                    "wall_budget_us": _as_int(
                        native_frontier_interpreter.get("wall_budget_us")
                    ),
                    "target_frame_us": TARGET_FRAME_US,
                },
            }
        )
    frontier_invocation_count = _as_int(
        native_frontier_interpreter.get("invocation_count")
    )
    if frontier_invocation_count >= 1_000:
        findings.append(
            {
                "id": "live_native_frontier_handoff_storm",
                "severity": (
                    "high" if frontier_invocation_count >= 10_000 else "warning"
                ),
                "evidence": {
                    "invocation_count": frontier_invocation_count,
                    "steps": _as_int(
                        native_frontier_interpreter.get("steps")
                    ),
                    "total_us": _as_int(
                        native_frontier_interpreter.get("total_us")
                    ),
                    "host_service_count": _as_int(
                        native_frontier_interpreter.get("host_service_count")
                    ),
                    "host_service_coalesced_count": _as_int(
                        native_frontier_interpreter.get(
                            "host_service_coalesced_count"
                        )
                    ),
                    "hot_targets": native_frontier_interpreter.get(
                        "hot_targets", []
                    )[:16],
                },
            }
        )
    promotion_failure_count = _as_int(
        native_frontier_promotion.get("failure_count")
    )
    if promotion_failure_count:
        findings.append(
            {
                "id": "native_frontier_background_promotion_failed",
                "severity": "high",
                "evidence": {
                    "failure_count": promotion_failure_count,
                    "events": native_frontier_promotion.get("events", [])[-16:],
                },
            }
        )
    flip_rate = _as_float(bridge.get("recent_guest_flip_rate_hz"))
    if flip_rate is not None and flip_rate < TARGET_ACCEPTANCE_FPS:
        findings.append(
            {
                "id": "guest_below_60_fps_target",
                "severity": "critical" if flip_rate < TARGET_FPS * 0.75 else "high",
                "evidence": {
                    "recent_guest_flip_rate_hz": round(flip_rate, 3),
                    "target_fps": TARGET_FPS,
                    "target_attainment_ratio": round(flip_rate / TARGET_FPS, 6),
                },
            }
        )

    counter_totals = guest.get("counter_totals") or {}
    sync_calls = _as_int(counter_totals.get("selective_dirty_sync_call_count"))
    no_matches = _as_int(counter_totals.get("selective_dirty_sync_no_match_count"))
    if sync_calls and no_matches / sync_calls >= 0.80:
        findings.append(
            {
                "id": "selective_sync_mostly_has_no_matching_page",
                "severity": "high",
                "evidence": {
                    "selective_dirty_sync_call_count": sync_calls,
                    "selective_dirty_sync_no_match_count": no_matches,
                    "no_match_ratio": round(no_matches / sync_calls, 6),
                    "observer_drain_deferred_count": _as_int(
                        counter_totals.get("observer_drain_deferred_count")
                    ),
                },
            }
        )

    writes_per_batch = _as_float(guest.get("average_observed_writes_per_batch"))
    batch_count = _as_int(counter_totals.get("native_observed_write_batch_count"))
    if writes_per_batch is not None and batch_count >= 1000 and writes_per_batch < 64:
        findings.append(
            {
                "id": "render_write_batches_are_fragmented",
                "severity": "high",
                "evidence": {
                    "average_observed_writes_per_batch": writes_per_batch,
                    "native_observed_write_batch_count": batch_count,
                    "native_observed_write_count": _as_int(
                        counter_totals.get("native_observed_write_count")
                    ),
                },
            }
        )

    callback_rate = _as_float(guest.get("memory_read_callbacks_per_million_steps"))
    if callback_rate is not None and callback_rate >= 1000:
        findings.append(
            {
                "id": "native_memory_callback_boundary_is_hot",
                "severity": "high",
                "evidence": {
                    "memory_read_callbacks_per_million_steps": callback_rate,
                    "exact_read_callback_ratio": guest.get("exact_read_callback_ratio"),
                    "sampled_hot_addresses": (
                        guest.get("memory_callback_sampling", {}).get("hot_addresses", [])[:8]
                    ),
                },
            }
        )

    module_rate = _as_float(guest.get("native_module_calls_per_million_steps"))
    if module_rate is not None and module_rate >= 20_000:
        findings.append(
            {
                "id": "native_module_handoff_rate_is_high",
                "severity": "warning",
                "evidence": {
                    "native_module_calls_per_million_steps": module_rate,
                    "profiled_native_run_count": guest.get("profiled_native_run_count"),
                    "hot_targets": guest.get("native_dispatch_hot_targets", [])[:8],
                },
            }
        )

    module_edge_profile = guest.get("native_module_edge_profile") or {}
    if module_edge_profile.get("enabled") and not module_edge_profile.get("exact"):
        findings.append(
            {
                "id": "native_module_edge_profile_is_incomplete",
                "severity": "high",
                "evidence": {
                    "unique_edge_count": _as_int(
                        module_edge_profile.get("unique_edge_count")
                    ),
                    "classified_module_calls": _as_int(
                        module_edge_profile.get("classified_module_calls")
                    ),
                    "unclassified_module_calls": _as_int(
                        module_edge_profile.get("unclassified_module_calls")
                    ),
                    "overflow_module_calls": _as_int(
                        module_edge_profile.get("overflow_module_calls")
                    ),
                    "table_capacities": module_edge_profile.get(
                        "table_capacities", []
                    ),
                },
            }
        )

    frame_pacing = presenter.get("frame_pacing") or {}
    missed_target_ratio = _as_float(frame_pacing.get("missed_target_ratio"))
    if missed_target_ratio is not None and missed_target_ratio >= 0.10:
        findings.append(
            {
                "id": "presenter_frame_pacing_misses_target",
                "severity": "high",
                "evidence": {
                    "missed_target_ratio": missed_target_ratio,
                    "frame_count": _as_int(frame_pacing.get("frame_count")),
                    "p50_us": (frame_pacing.get("elapsed_us") or {}).get("p50_us"),
                    "p95_us": (frame_pacing.get("elapsed_us") or {}).get("p95_us"),
                    "target_frame_us": frame_pacing.get("target_frame_us"),
                },
            }
        )

    reload_stages = presenter.get("reload_stages") or {}
    reload_total = reload_stages.get("total_us") or {}
    reload_average_us = _as_float(reload_total.get("average_us"))
    reload_busy_ratio = _as_float(presenter.get("reload_busy_ratio"))
    if (
        reload_average_us is not None
        and reload_average_us > TARGET_FRAME_US
    ) or (reload_busy_ratio is not None and reload_busy_ratio >= 0.20):
        findings.append(
            {
                "id": "presenter_reload_is_hot",
                "severity": "high",
                "evidence": {
                    "reload_count": _as_int(presenter.get("reload_count")),
                    "reload_busy_ratio": reload_busy_ratio,
                    "average_reload_us": reload_average_us,
                    "p95_reload_us": reload_total.get("p95_us"),
                    "target_frame_us": TARGET_FRAME_US,
                    "hot_stages": presenter.get("hot_paths_by_total_time", [])[:6],
                },
            }
        )

    offscreen_lifecycle = presenter.get("offscreen_target_lifecycle") or {}
    offscreen_target_reloads = _as_int(
        offscreen_lifecycle.get("target_reload_count")
    )
    offscreen_reuse_ratio = _as_float(offscreen_lifecycle.get("reuse_ratio"))
    if (
        offscreen_target_reloads >= 10
        and offscreen_reuse_ratio is not None
        and offscreen_reuse_ratio < 0.80
    ):
        findings.append(
            {
                "id": "offscreen_targets_are_recreated",
                "severity": "high",
                "evidence": offscreen_lifecycle,
            }
        )

    texture_conversion = presenter.get("gpu_texture_conversion") or {}
    texture_validation = texture_conversion.get("validation") or {}
    if texture_validation.get("passed") is False:
        findings.append(
            {
                "id": "gpu_texture_conversion_validation_failed",
                "severity": "critical",
                "evidence": texture_validation,
            }
        )
    gpu_converted_textures = _as_int(
        texture_conversion.get("gpu_converted_textures")
    )
    cpu_converted_textures = _as_int(
        texture_conversion.get("cpu_converted_textures")
    )
    if (
        gpu_converted_textures + cpu_converted_textures >= 10
        and cpu_converted_textures > gpu_converted_textures
    ):
        findings.append(
            {
                "id": "texture_conversion_mostly_on_cpu",
                "severity": "high",
                "evidence": {
                    "gpu_converted_textures": gpu_converted_textures,
                    "cpu_converted_textures": cpu_converted_textures,
                    "backend_counts": texture_conversion.get("backend_counts"),
                },
            }
        )
    texture_batch_us = texture_conversion.get("gpu_batch_us") or {}
    texture_batch_average_us = _as_float(texture_batch_us.get("average_us"))
    if (
        _as_int(texture_conversion.get("batch_count")) > 0
        and texture_batch_average_us is not None
        and texture_batch_average_us > TARGET_FRAME_US
    ):
        findings.append(
            {
                "id": "gpu_texture_conversion_batch_exceeds_frame_budget",
                "severity": "warning",
                "evidence": {
                    "average_batch_us": texture_batch_average_us,
                    "p95_batch_us": texture_batch_us.get("p95_us"),
                    "target_frame_us": TARGET_FRAME_US,
                    "setup_us": texture_conversion.get("setup_us"),
                    "gpu_submission_us": texture_conversion.get(
                        "gpu_submission_us"
                    ),
                    "validation_cpu_us": texture_validation.get("cpu_us"),
                },
            }
        )

    pipeline_compilation = presenter.get("pipeline_compilation") or {}
    pipeline_create_us = pipeline_compilation.get("pipeline_create_us") or {}
    maximum_pipeline_create_us = _as_float(
        pipeline_create_us.get("maximum_us")
    )
    if (
        maximum_pipeline_create_us is not None
        and maximum_pipeline_create_us > TARGET_FRAME_US
    ):
        findings.append(
            {
                "id": "synchronous_pipeline_compile_exceeds_frame_budget",
                "severity": "warning",
                "evidence": {
                    "maximum_us": maximum_pipeline_create_us,
                    "p95_us": pipeline_create_us.get("p95_us"),
                    "target_frame_us": TARGET_FRAME_US,
                    "graphics_pipeline_count": pipeline_compilation.get(
                        "graphics_pipeline_count"
                    ),
                    "compute_pipeline_count": pipeline_compilation.get(
                        "compute_pipeline_count"
                    ),
                    "batched_graphics_event_count": pipeline_compilation.get(
                        "batched_graphics_event_count"
                    ),
                },
            }
        )

    if not _as_int(guest.get("profiled_native_run_count")):
        findings.append(
            {
                "id": "detailed_hot_path_profile_not_enabled",
                "severity": "info",
                "evidence": {
                    "guidance": (
                        "Run live_test.py --profile-hot-paths for module targets "
                        "and sampled callback latency."
                    )
                },
            }
        )

    order = {"critical": 0, "high": 1, "warning": 2, "info": 3}
    findings.sort(key=lambda item: (order.get(str(item["severity"]), 99), item["id"]))
    for priority, finding in enumerate(findings, start=1):
        finding["priority"] = priority
    return findings


def build_performance_debug_report(
    probe_summary_path: Path,
    *,
    render_debug_report_path: Path | None = None,
) -> dict[str, Any]:
    probe = json.loads(probe_summary_path.read_text(encoding="utf-8"))
    execution = probe.get("entry_recovery", {}).get("execution", {})
    if not isinstance(execution, dict):
        execution = {}
    guest = _summarize_guest_performance(execution)
    native_compilation = _summarize_native_compilation(execution)
    native_frontier_interpreter = _summarize_native_frontier_interpreter(
        execution
    )
    native_frontier_promotion = _summarize_native_frontier_promotion(execution)
    bridge = guest.get("live_host_bridge_summary") or {}
    render_watchpoint = execution.get("render_watchpoint_stream") or {}
    render_write_batch = render_watchpoint.get("native_write_batch") or {}
    presenter: dict[str, Any] = {}
    if render_debug_report_path is not None and render_debug_report_path.is_file():
        render_report = json.loads(
            render_debug_report_path.read_text(encoding="utf-8")
        )
        candidate = (render_report.get("performance") or {}).get("presenter")
        if isinstance(candidate, dict):
            presenter = candidate
    bridge_metrics = (bridge.get("performance") or {}).get("metrics") or {}
    total_elapsed_us = _as_int(guest.get("elapsed_total_us"))
    wait_us = _metric_total(
        bridge_metrics,
        "presentation_ack_wait",
        "audit_ack_wait",
        "video_frame_pacing",
    )
    active_elapsed_us = max(0, total_elapsed_us - min(total_elapsed_us, wait_us))
    total_steps = _as_int(guest.get("total_steps"))
    active_steps_per_second = (
        round(total_steps * 1_000_000 / active_elapsed_us, 3)
        if active_elapsed_us
        else None
    )
    recent_flip_rate = _as_float(bridge.get("recent_guest_flip_rate_hz"))
    target_attainment = (
        round(recent_flip_rate / TARGET_FPS, 6)
        if recent_flip_rate is not None
        else None
    )
    findings = _findings(
        guest,
        bridge,
        presenter,
        native_compilation,
        native_frontier_interpreter,
        native_frontier_promotion,
    )
    critical_or_high = any(
        finding["severity"] in {"critical", "high"} for finding in findings
    )

    return {
        "format": "b2-recomp-performance-debug-report",
        "public_safe": False,
        "status": (
            "insufficient_native_evidence"
            if not guest.get("native_run_count")
            else "target_met"
            if recent_flip_rate is not None
            and recent_flip_rate >= TARGET_ACCEPTANCE_FPS
            else "optimization_required"
            if critical_or_high
            else "profile_complete"
        ),
        "accuracy_policy": {
            "guest_visible_shortcuts_allowed": False,
            "timing_interpretation": (
                "inclusive timings may overlap; never sum them as exclusive CPU time"
            ),
            "observer_deferral_scope": (
                "Only exact reads declared independent of pending render observer events; "
                "all bytes, MMIO hooks, dirty pages, and ordered events remain intact."
            ),
            "callback_policy_cache_scope": (
                "Only memory models that explicitly declare static callback policies; "
                "guest-visible read and write effects remain preserved on every access."
            ),
            "aligned_read_dispatch_scope": (
                "Only the matching aligned title read hook is selected; general reads "
                "and cross-page words retain the original path."
            ),
            "zero_guarded_read_scope": (
                "Initialized D3D context and submission words read from the native cache; "
                "a zero value still commits its dirty page and executes the original "
                "fallback callback before the cache is refreshed."
            ),
            "packed_render_write_scope": (
                "The native packer validates the active guest range, preserves the exact "
                "ordered 16-byte command and provenance records, and compacts only "
                "contiguous texture bytes into run descriptors consumed in order."
            ),
            "flip_packet_yield_scope": (
                "The configured observed-write stream yields only after recording both "
                "the NV097 flip header and its contiguous data word; the ordinary native "
                "read path remains unchanged."
            ),
            "target_profiling_scope": (
                "Per-target, exact entry-to-exit edge, module-exit-reason, and native "
                "dispatcher self-time accounting is opt-in diagnostic metadata and does "
                "not alter guest control flow; aggregate module counts remain enabled "
                "in normal runs."
            ),
            "live_frontier_recovery_scope": (
                "New executable CFG batches use the existing instruction interpreter "
                "with the same runtime handlers and ordered memory effects until control "
                "returns to an available native address. Known frontier modules remain "
                "inside one interpreter dispatch, while a one-worker below-normal-priority "
                "build creates a cumulative native executor off the presentation thread. "
                "Completed executors are adopted only at an execution boundary."
            ),
            "indexed_vertex_lookup_scope": (
                "The presenter page index changes only candidate lookup; exact byte-range "
                "validation and original resource precedence remain unchanged."
            ),
            "offscreen_target_reuse_scope": (
                "Targets are reused only after the in-flight fence and only when resource "
                "generation, target specs, dedicated-depth handles, and the exact retained "
                "feedback color view match; unrelated presented feedback may rotate."
            ),
            "feedback_image_cache_scope": (
                "Inactive GPU-produced feedback images remain in a bounded exact-spec "
                "cache only after the in-flight fence; active descriptors are rewritten "
                "before eviction, and missing or changed specs still allocate a new image."
            ),
            "gpu_texture_conversion_scope": (
                "DXT1/DXT5 use integer-exact compute decode and the existing recovered-"
                "versus-generated mip policy; representative device output is compared "
                "byte-for-byte with the CPU oracle, rejected batches fall back to CPU, "
                "and all other formats remain on the CPU path."
            ),
        },
        "source": {
            "probe_summary_path": str(probe_summary_path),
            "probe_format": probe.get("format"),
            "execution_status": execution.get("status"),
            "render_debug_report_path": (
                str(render_debug_report_path)
                if render_debug_report_path is not None
                else None
            ),
            "render_debug_report_loaded": bool(presenter),
        },
        "target": {
            "fps": TARGET_FPS,
            "acceptance_floor_fps": TARGET_ACCEPTANCE_FPS,
            "frame_budget_us": TARGET_FRAME_US,
            "recent_guest_flip_rate_hz": recent_flip_rate,
            "target_attainment_ratio": target_attainment,
            "recent_guest_flip_sample_count": bridge.get(
                "recent_guest_flip_sample_count"
            ),
        },
        "throughput": {
            "guest_steps": total_steps,
            "elapsed_total_us": total_elapsed_us,
            "effective_steps_per_second": guest.get("effective_steps_per_second"),
            "host_wait_us": wait_us,
            "host_wait_ratio": (
                round(wait_us / total_elapsed_us, 6) if total_elapsed_us else None
            ),
            "active_elapsed_us": active_elapsed_us,
            "active_steps_per_second": active_steps_per_second,
            "native_compile_wall_us": native_compilation.get("compile_wall_us"),
            "incremental_native_compile_wall_us": native_compilation.get(
                "incremental_compile_wall_us"
            ),
            "synchronous_incremental_native_compile_wall_us": (
                native_compilation.get(
                    "synchronous_incremental_compile_wall_us"
                )
            ),
            "background_native_promotion_compile_wall_us": (
                native_compilation.get("background_promotion_compile_wall_us")
            ),
            "accounted_session_wall_us": total_elapsed_us
            + _as_int(native_compilation.get("compile_wall_us"))
            - _as_int(
                native_compilation.get("background_promotion_compile_wall_us")
            ),
            "effective_steps_per_60hz_frame": (
                round(float(guest["effective_steps_per_second"]) / TARGET_FPS, 3)
                if guest.get("effective_steps_per_second") is not None
                else None
            ),
        },
        "boundaries": {
            "counter_totals": guest.get("counter_totals"),
            "timing_hot_paths": guest.get("timing_hot_paths"),
            "handler_hot_paths": guest.get("handler_hot_paths"),
            "native_module_calls_per_million_steps": guest.get(
                "native_module_calls_per_million_steps"
            ),
            "native_module_exit_profile": guest.get(
                "native_module_exit_profile"
            ),
            "native_module_edge_profile": guest.get(
                "native_module_edge_profile"
            ),
            "native_dispatch_timing": guest.get("native_dispatch_timing"),
            "native_compilation": native_compilation,
            "native_frontier_interpreter": native_frontier_interpreter,
            "native_frontier_promotion": native_frontier_promotion,
            "memory_read_callbacks_per_million_steps": guest.get(
                "memory_read_callbacks_per_million_steps"
            ),
            "page_cache_fills_per_million_steps": guest.get(
                "page_cache_fills_per_million_steps"
            ),
            "dirty_page_sync_time_ratio": guest.get("dirty_page_sync_time_ratio"),
            "dirty_sync_no_work_ratio": guest.get("dirty_sync_no_work_ratio"),
            "average_observed_writes_per_batch": guest.get(
                "average_observed_writes_per_batch"
            ),
            "memory_callback_policy_cache_hit_ratio": guest.get(
                "memory_callback_policy_cache_hit_ratio"
            ),
            "empty_dependency_sync_bypass_ratio": guest.get(
                "empty_dependency_sync_bypass_ratio"
            ),
            "render_write_batch": render_write_batch,
        },
        "sampled_hot_paths": {
            "profiled_native_run_count": guest.get("profiled_native_run_count"),
            "native_dispatch_targets": guest.get("native_dispatch_hot_targets"),
            "memory_callbacks": guest.get("memory_callback_sampling"),
            "legacy_read_callbacks": guest.get("read_callback_sampling"),
        },
        "live_host_bridge": bridge,
        "presenter": presenter,
        "findings": findings,
    }


def write_performance_debug_report(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a focused native guest hot-path and 60 FPS target report."
    )
    parser.add_argument("--probe-summary", type=Path, default=DEFAULT_PROBE_SUMMARY)
    parser.add_argument(
        "--render-debug-report",
        type=Path,
        default=REPO_ROOT
        / "reports"
        / "local"
        / "playability"
        / "render-debug-report.json",
    )
    parser.add_argument("--json-output", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    report = build_performance_debug_report(
        args.probe_summary,
        render_debug_report_path=args.render_debug_report,
    )
    write_performance_debug_report(report, args.json_output)
    print(json.dumps(report, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
