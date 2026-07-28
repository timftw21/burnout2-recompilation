#!/usr/bin/env python3
"""Build a focused 60 FPS guest hot-path report from a live probe summary."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict
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
    retained_native_run_count = 0
    for thread in execution.get("guest_thread_executions", []):
        if not isinstance(thread, dict):
            continue
        runs = thread.get("native_runs")
        if not isinstance(runs, list):
            run = thread.get("native_run")
            runs = [run] if isinstance(run, dict) else []
        retained_thread_run_count = sum(
            1 for run in runs if isinstance(run, dict)
        )
        history = thread.get("native_run_history")
        native_run_count += max(
            retained_thread_run_count,
            _as_int(history.get("total_count"))
            if isinstance(history, dict)
            else retained_thread_run_count,
        )
        retained_native_run_count += retained_thread_run_count
        previous_target: int | None = None
        seen_executor_keys: set[str] = set()
        for run_index, run in enumerate(runs):
            if not isinstance(run, dict):
                continue
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
        "retained_native_run_count": retained_native_run_count,
        "dropped_native_run_count": max(
            0, native_run_count - retained_native_run_count
        ),
        "history_complete": native_run_count == retained_native_run_count,
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


def _summarize_profile_integrity(
    execution: dict[str, Any],
    guest: dict[str, Any],
    native_frontier_interpreter: dict[str, Any],
    native_frontier_promotion: dict[str, Any],
) -> dict[str, Any]:
    profile = guest.get("hot_path_profile") or {}
    enabled = profile.get("enabled") is True
    threads = [
        thread
        for thread in execution.get("guest_thread_executions", [])
        if isinstance(thread, dict)
    ]
    runtime_abi_invocations = sum(
        _as_int(thread.get("runtime_abi_invocation_count"))
        for thread in threads
    )
    bootstrap_abi_invocations = _as_int(
        (execution.get("native_bootstrap") or {}).get(
            "python_runtime_abi_invocation_count"
        )
    )
    counters = guest.get("counter_totals") or {}
    callback_fields = (
        "handler_call_count",
        "native_cold_host_call_count",
        "read_u32_callback_count",
        "read_u8_callback_count",
        "write_u32_callback_count",
        "write_u8_callback_count",
        "observer_callback_count",
        "call_handler_yield_count",
        "slice_yield_count",
        "predicate_yield_count",
    )
    callback_boundary_counts = {
        field: _as_int(counters.get(field)) for field in callback_fields
    }
    session_python_callback_evidence_count = (
        runtime_abi_invocations
        + bootstrap_abi_invocations
        + sum(callback_boundary_counts.values())
    )
    capture_windows = [
        window
        for window in profile.get("capture_windows", [])
        if isinstance(window, dict)
    ]
    controlled_capture = bool(capture_windows) and all(
        window.get("controlled") is True for window in capture_windows
    )
    completed_capture = controlled_capture and all(
        window.get("state") == "complete"
        and _as_int(window.get("started_count")) > 0
        and _as_int(window.get("completed_count")) > 0
        for window in capture_windows
    )
    capture_callback_scope_available = completed_capture and all(
        isinstance(window.get("python_runtime_callbacks"), dict)
        for window in capture_windows
    )
    capture_boundary_counts = {field: 0 for field in callback_fields}
    if capture_callback_scope_available:
        for window in capture_windows:
            capture_counts = (
                window.get("python_runtime_callbacks") or {}
            ).get("boundary_counts") or {}
            for field in callback_fields:
                capture_boundary_counts[field] += _as_int(
                    capture_counts.get(field)
                )
    if controlled_capture:
        callback_scope = "capture_window"
        active_boundary_counts = capture_boundary_counts
        python_callback_evidence_count = (
            runtime_abi_invocations
            + sum(capture_boundary_counts.values())
        )
    else:
        callback_scope = "full_session"
        active_boundary_counts = callback_boundary_counts
        python_callback_evidence_count = session_python_callback_evidence_count
    normal_runtime_rows = [
        {
            "thread_index": _as_int(thread.get("thread_index")),
            "enabled": bool(
                ((thread.get("native_host_services") or {}).get(
                    "normal_runtime"
                ) or {}).get("enabled")
            ),
        }
        for thread in threads
    ]
    normal_runtime_observed = any(row["enabled"] for row in normal_runtime_rows)
    developer_live_compilation_enabled = any(
        bool(thread.get("developer_live_compilation_enabled"))
        for thread in threads
    )
    frontier_invocations = _as_int(
        native_frontier_interpreter.get("invocation_count")
    )
    frontier_steps = _as_int(native_frontier_interpreter.get("steps"))
    promotion_enabled = bool(native_frontier_promotion.get("enabled"))
    promotion_dispatches = _as_int(
        native_frontier_promotion.get("promoted_dispatch_count")
    )
    static_clean = not (
        developer_live_compilation_enabled
        or frontier_invocations
        or frontier_steps
        or promotion_enabled
        or promotion_dispatches
    )
    observer_dispatch_clean = (
        profile.get("native_observer_dispatch_all") is True
    )
    captured_module_calls = _as_int(profile.get("captured_module_calls"))
    capture_observed = captured_module_calls > 0
    if not enabled:
        status = "not_profiled"
        accepted: bool | None = None
    elif not normal_runtime_observed:
        status = "normal_runtime_not_observed"
        accepted = False
    elif controlled_capture and not completed_capture:
        status = "capture_window_incomplete"
        accepted = False
    elif controlled_capture and not capture_callback_scope_available:
        status = "capture_callback_scope_unavailable"
        accepted = False
    elif python_callback_evidence_count:
        status = "python_callbacks_observed"
        accepted = False
    elif not static_clean:
        status = "static_boundary_violation"
        accepted = False
    elif not observer_dispatch_clean:
        status = "native_observer_dispatch_disabled"
        accepted = False
    elif not capture_observed:
        status = "empty_capture_window"
        accepted = False
    else:
        status = "native_profile_clean"
        accepted = True
    service_call_counts: dict[str, int] = {}
    scheduler_service_count = 0
    for thread in threads:
        services = thread.get("native_host_services") or {}
        for name, count in (services.get("service_call_counts") or {}).items():
            service_call_counts[str(name)] = (
                service_call_counts.get(str(name), 0) + _as_int(count)
            )
        scheduler_service_count += _as_int(
            (services.get("normal_runtime") or {}).get(
                "scheduler_service_count"
            )
        )
    return {
        "enabled": enabled,
        "accepted": accepted,
        "status": status,
        "normal_runtime_observed": normal_runtime_observed,
        "normal_runtime_threads": normal_runtime_rows,
        "native_observer_dispatch_all": observer_dispatch_clean,
        "capture_observed": capture_observed,
        "captured_module_calls": captured_module_calls,
        "captured_guest_steps": _as_int(
            profile.get("captured_guest_steps")
        ),
        "captured_elapsed_us": _as_int(
            profile.get("captured_elapsed_us")
        ),
        "estimated_profile_bookkeeping_us": _as_int(
            profile.get("estimated_profile_bookkeeping_us")
        ),
        "capture_windows": capture_windows,
        "python_runtime_callbacks": {
            "scope": callback_scope,
            "runtime_abi_invocation_count": runtime_abi_invocations,
            "bootstrap_runtime_abi_invocation_count": (
                bootstrap_abi_invocations
                if callback_scope == "full_session"
                else 0
            ),
            "boundary_counts": active_boundary_counts,
            "evidence_count": python_callback_evidence_count,
            "session": {
                "runtime_abi_invocation_count": runtime_abi_invocations,
                "bootstrap_runtime_abi_invocation_count": (
                    bootstrap_abi_invocations
                ),
                "boundary_counts": callback_boundary_counts,
                "evidence_count": session_python_callback_evidence_count,
            },
        },
        "static_runtime": {
            "clean": static_clean,
            "developer_live_compilation_enabled": (
                developer_live_compilation_enabled
            ),
            "frontier_interpreter_invocation_count": frontier_invocations,
            "frontier_interpreter_steps": frontier_steps,
            "native_promotion_enabled": promotion_enabled,
            "native_promotion_dispatch_count": promotion_dispatches,
        },
        "native_service_activity": {
            "service_call_counts": dict(sorted(service_call_counts.items())),
            "scheduler_service_count": scheduler_service_count,
        },
    }


def _guest_flip_measurement(
    bridge: dict[str, Any],
    presenter: dict[str, Any],
) -> dict[str, Any]:
    window = presenter.get("profile_capture_window") or {}
    frame_pacing = presenter.get("frame_pacing") or {}
    started_flip = window.get("started_guest_flip_count")
    completed_flip = window.get("completed_guest_flip_count")
    elapsed_seconds = _as_float(frame_pacing.get("elapsed_total_seconds"))
    if (
        window.get("state") == "complete"
        and started_flip is not None
        and completed_flip is not None
        and elapsed_seconds is not None
        and elapsed_seconds > 0.0
    ):
        flip_count = max(
            0,
            _as_int(completed_flip) - _as_int(started_flip),
        )
        return {
            "source": "profile_capture_window",
            "rate_hz": round(flip_count / elapsed_seconds, 6),
            "sample_count": flip_count,
            "elapsed_seconds": elapsed_seconds,
            "started_guest_flip_count": _as_int(started_flip),
            "completed_guest_flip_count": _as_int(completed_flip),
        }
    recent_rate = _as_float(bridge.get("recent_guest_flip_rate_hz"))
    recent_sample_count = _as_int(
        bridge.get("recent_guest_flip_sample_count")
    )
    if recent_rate is not None and (
        recent_sample_count > 0 or recent_rate > 0.0
    ):
        return {
            "source": "bridge_recent_window",
            "rate_hz": recent_rate,
            "sample_count": recent_sample_count,
            "elapsed_seconds": None,
            "started_guest_flip_count": None,
            "completed_guest_flip_count": None,
        }
    return {
        "source": "unavailable",
        "rate_hz": None,
        "sample_count": 0,
        "elapsed_seconds": None,
        "started_guest_flip_count": None,
        "completed_guest_flip_count": None,
    }


def _strongly_connected_components(
    nodes: set[int],
    adjacency: dict[int, set[int]],
) -> list[list[int]]:
    """Return deterministic SCCs without recursion-depth limits."""
    visited: set[int] = set()
    finish_order: list[int] = []
    for root in sorted(nodes):
        if root in visited:
            continue
        stack: list[tuple[int, bool]] = [(root, False)]
        while stack:
            node, expanded = stack.pop()
            if expanded:
                finish_order.append(node)
                continue
            if node in visited:
                continue
            visited.add(node)
            stack.append((node, True))
            for neighbor in sorted(adjacency.get(node, ()), reverse=True):
                if neighbor not in visited:
                    stack.append((neighbor, False))

    reverse_adjacency: dict[int, set[int]] = defaultdict(set)
    for source, destinations in adjacency.items():
        for destination in destinations:
            reverse_adjacency[destination].add(source)
    components: list[list[int]] = []
    assigned: set[int] = set()
    for root in reversed(finish_order):
        if root in assigned:
            continue
        component: list[int] = []
        stack = [(root, False)]
        while stack:
            node, _expanded = stack.pop()
            if node in assigned:
                continue
            assigned.add(node)
            component.append(node)
            for neighbor in sorted(
                reverse_adjacency.get(node, ()), reverse=True
            ):
                if neighbor not in assigned:
                    stack.append((neighbor, False))
        components.append(sorted(component))
    return components


def _summarize_hot_path_analysis(
    guest: dict[str, Any],
    flip_measurement: dict[str, Any],
) -> dict[str, Any]:
    raw_targets = guest.get("native_dispatch_targets") or guest.get(
        "native_dispatch_hot_targets"
    ) or []
    target_by_address = {
        _as_int(target.get("target")): dict(target)
        for target in raw_targets
        if isinstance(target, dict) and target.get("target") is not None
    }
    edge_profile = guest.get("native_module_edge_profile") or {}
    raw_edges = [
        edge
        for edge in edge_profile.get("edges", [])
        if isinstance(edge, dict)
    ]
    total_calls = sum(
        _as_int(target.get("module_calls"))
        for target in target_by_address.values()
    )
    total_steps = sum(
        _as_int(target.get("guest_steps"))
        for target in target_by_address.values()
    )
    total_estimated_native_time_us = sum(
        _as_float(target.get("estimated_native_time_us")) or 0.0
        for target in target_by_address.values()
    )
    native_services = [
        dict(service)
        for service in guest.get("native_host_service_hot_targets", [])
        if isinstance(service, dict)
    ]
    total_estimated_service_time_us = sum(
        _as_float(service.get("estimated_native_time_us")) or 0.0
        for service in native_services
    )
    total_estimated_combined_time_us = (
        total_estimated_native_time_us + total_estimated_service_time_us
    )
    capture = guest.get("hot_path_profile") or {}
    captured_elapsed_us = _as_int(capture.get("captured_elapsed_us"))
    captured_flip_count = (
        _as_int(flip_measurement.get("sample_count"))
        if flip_measurement.get("source") == "profile_capture_window"
        else 0
    )
    outgoing_counts: Counter[int] = Counter()
    incoming_counts: Counter[int] = Counter()
    outgoing_neighbors: dict[int, set[int]] = defaultdict(set)
    incoming_neighbors: dict[int, set[int]] = defaultdict(set)
    adjacency: dict[int, set[int]] = defaultdict(set)
    self_loops: set[int] = set()
    for edge in raw_edges:
        source = _as_int(edge.get("entry_target"), -1)
        destination = _as_int(edge.get("exit_target"), -1)
        count = _as_int(edge.get("module_calls"))
        if source < 0 or count <= 0:
            continue
        outgoing_counts[source] += count
        if destination >= 0:
            incoming_counts[destination] += count
            outgoing_neighbors[source].add(destination)
            incoming_neighbors[destination].add(source)
        if source in target_by_address and destination in target_by_address:
            adjacency[source].add(destination)
            if source == destination:
                self_loops.add(source)

    targets: list[dict[str, Any]] = []
    for address, target in target_by_address.items():
        module_calls = _as_int(target.get("module_calls"))
        guest_steps = _as_int(target.get("guest_steps"))
        estimated_us = _as_float(target.get("estimated_native_time_us")) or 0.0
        lanes = target.get("execution_lanes") or {}
        dominant_lane = None
        if lanes:
            dominant_lane = max(
                lanes,
                key=lambda name: (
                    _as_int((lanes.get(name) or {}).get("guest_steps")),
                    _as_int((lanes.get(name) or {}).get("module_calls")),
                    name,
                ),
            )
        target.update(
            {
                "call_share": (
                    round(module_calls / total_calls, 9)
                    if total_calls
                    else None
                ),
                "guest_step_share": (
                    round(guest_steps / total_steps, 9)
                    if total_steps
                    else None
                ),
                "estimated_native_time_share": (
                    round(estimated_us / total_estimated_native_time_us, 9)
                    if total_estimated_native_time_us
                    else None
                ),
                "module_calls_per_second": (
                    round(module_calls * 1_000_000 / captured_elapsed_us, 3)
                    if captured_elapsed_us
                    else None
                ),
                "guest_steps_per_second": (
                    round(guest_steps * 1_000_000 / captured_elapsed_us, 3)
                    if captured_elapsed_us
                    else None
                ),
                "estimated_native_us_per_second": (
                    round(estimated_us * 1_000_000 / captured_elapsed_us, 3)
                    if captured_elapsed_us
                    else None
                ),
                "module_calls_per_flip": (
                    round(module_calls / captured_flip_count, 6)
                    if captured_flip_count
                    else None
                ),
                "guest_steps_per_flip": (
                    round(guest_steps / captured_flip_count, 6)
                    if captured_flip_count
                    else None
                ),
                "estimated_native_us_per_flip": (
                    round(estimated_us / captured_flip_count, 6)
                    if captured_flip_count
                    else None
                ),
                "frame_budget_share": (
                    round(
                        estimated_us
                        / captured_flip_count
                        / TARGET_FRAME_US,
                        9,
                    )
                    if captured_flip_count
                    else None
                ),
                "estimated_native_ns_per_guest_step": (
                    round(estimated_us * 1_000 / guest_steps, 6)
                    if guest_steps
                    else None
                ),
                "incoming_transition_count": incoming_counts[address],
                "outgoing_transition_count": outgoing_counts[address],
                "incoming_fan_in": len(incoming_neighbors[address]),
                "outgoing_fan_out": len(outgoing_neighbors[address]),
                "dominant_execution_lane": dominant_lane,
            }
        )
        targets.append(target)

    total_service_calls = sum(
        _as_int(service.get("calls")) for service in native_services
    )
    for service in native_services:
        calls = _as_int(service.get("calls"))
        estimated_us = _as_float(service.get("estimated_native_time_us")) or 0.0
        service.update(
            {
                "call_share": (
                    round(calls / total_service_calls, 9)
                    if total_service_calls
                    else None
                ),
                "estimated_native_time_share": (
                    round(estimated_us / total_estimated_service_time_us, 9)
                    if total_estimated_service_time_us
                    else None
                ),
                "calls_per_second": (
                    round(calls * 1_000_000 / captured_elapsed_us, 3)
                    if captured_elapsed_us
                    else None
                ),
                "calls_per_flip": (
                    round(calls / captured_flip_count, 6)
                    if captured_flip_count
                    else None
                ),
                "estimated_native_us_per_second": (
                    round(estimated_us * 1_000_000 / captured_elapsed_us, 3)
                    if captured_elapsed_us
                    else None
                ),
                "estimated_native_us_per_flip": (
                    round(estimated_us / captured_flip_count, 6)
                    if captured_flip_count
                    else None
                ),
                "frame_budget_share": (
                    round(
                        estimated_us
                        / captured_flip_count
                        / TARGET_FRAME_US,
                        9,
                    )
                    if captured_flip_count
                    else None
                ),
            }
        )
    native_services.sort(
        key=lambda service: (
            -(_as_float(service.get("estimated_native_time_us")) or 0.0),
            -_as_int(service.get("calls")),
            _as_int(service.get("target")),
        )
    )

    enriched_edges: list[dict[str, Any]] = []
    for edge in raw_edges:
        source = _as_int(edge.get("entry_target"), -1)
        destination = _as_int(edge.get("exit_target"), -1)
        count = _as_int(edge.get("module_calls"))
        source_total = outgoing_counts[source]
        enriched_edges.append(
            {
                **edge,
                "source_transition_ratio": (
                    round(count / source_total, 9) if source_total else None
                ),
                "captured_transition_share": (
                    round(count / total_calls, 9) if total_calls else None
                ),
                "transitions_per_flip": (
                    round(count / captured_flip_count, 6)
                    if captured_flip_count
                    else None
                ),
                "terminal": destination not in target_by_address,
            }
        )
    enriched_edges.sort(
        key=lambda edge: (
            -_as_int(edge.get("module_calls")),
            _as_int(edge.get("entry_target")),
            _as_int(edge.get("exit_target")),
        )
    )

    branch_points: list[dict[str, Any]] = []
    edges_by_source: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for edge in enriched_edges:
        edges_by_source[_as_int(edge.get("entry_target"))].append(edge)
    for source, edges in edges_by_source.items():
        if len(edges) < 2:
            continue
        probabilities = [
            _as_int(edge.get("module_calls")) / outgoing_counts[source]
            for edge in edges
            if outgoing_counts[source]
        ]
        entropy_bits = -sum(
            probability * math.log2(probability)
            for probability in probabilities
            if probability > 0.0
        )
        branch_points.append(
            {
                "target": source,
                "target_hex": f"0x{source:08X}",
                "fan_out": len(edges),
                "transition_count": outgoing_counts[source],
                "entropy_bits": round(entropy_bits, 6),
                "normalized_entropy": (
                    round(entropy_bits / math.log2(len(edges)), 6)
                    if len(edges) > 1
                    else 0.0
                ),
                "dominant_transition_ratio": max(probabilities, default=0.0),
                "transitions": edges[:16],
            }
        )
    branch_points.sort(
        key=lambda item: (
            -item["transition_count"],
            -item["normalized_entropy"],
            item["target"],
        )
    )

    components = _strongly_connected_components(
        set(target_by_address), adjacency
    )
    cycles: list[dict[str, Any]] = []
    component_by_target: dict[int, int] = {}
    cyclic_components = [
        component
        for component in components
        if len(component) > 1 or component[0] in self_loops
    ]
    for cycle_id, component in enumerate(cyclic_components):
        for address in component:
            component_by_target[address] = cycle_id
    internal_transition_counts: Counter[int] = Counter()
    for edge in raw_edges:
        source_cycle = component_by_target.get(
            _as_int(edge.get("entry_target"), -1)
        )
        if source_cycle is None:
            continue
        if source_cycle == component_by_target.get(
            _as_int(edge.get("exit_target"), -1)
        ):
            internal_transition_counts[source_cycle] += _as_int(
                edge.get("module_calls")
            )
    for cycle_id, component in enumerate(cyclic_components):
        component_targets = [target_by_address[address] for address in component]
        component_calls = sum(
            _as_int(target.get("module_calls")) for target in component_targets
        )
        component_steps = sum(
            _as_int(target.get("guest_steps")) for target in component_targets
        )
        component_estimated_us = sum(
            _as_float(target.get("estimated_native_time_us")) or 0.0
            for target in component_targets
        )
        cycles.append(
            {
                "cycle_id": cycle_id,
                "target_count": len(component),
                "targets": [f"0x{address:08X}" for address in component],
                "module_calls": component_calls,
                "guest_steps": component_steps,
                "estimated_native_time_us": round(component_estimated_us, 3),
                "estimated_native_time_share": (
                    round(
                        component_estimated_us
                        / total_estimated_native_time_us,
                        9,
                    )
                    if total_estimated_native_time_us
                    else None
                ),
                "internal_transition_count": internal_transition_counts[
                    cycle_id
                ],
                "top_targets": sorted(
                    (
                        {
                            "target": address,
                            "target_hex": f"0x{address:08X}",
                            "module_calls": _as_int(
                                target_by_address[address].get("module_calls")
                            ),
                            "guest_steps": _as_int(
                                target_by_address[address].get("guest_steps")
                            ),
                            "estimated_native_time_us": _as_float(
                                target_by_address[address].get(
                                    "estimated_native_time_us"
                                )
                            ) or 0.0,
                        }
                        for address in component
                    ),
                    key=lambda item: (
                        -item["estimated_native_time_us"],
                        -item["guest_steps"],
                        item["target"],
                    ),
                )[:16],
            }
        )
    cycles.sort(
        key=lambda cycle: (
            -cycle["estimated_native_time_us"],
            -cycle["guest_steps"],
            cycle["targets"],
        )
    )
    for rank, cycle in enumerate(cycles, start=1):
        cycle["rank"] = rank
    for target in targets:
        address = _as_int(target.get("target"))
        if address in component_by_target:
            target["cycle_id"] = component_by_target[address]

    targets_by_native_time = sorted(
        targets,
        key=lambda target: (
            -(_as_float(target.get("estimated_native_time_us")) or 0.0),
            -_as_int(target.get("guest_steps")),
            _as_int(target.get("target")),
        ),
    )
    targets_by_guest_steps = sorted(
        targets,
        key=lambda target: (
            -_as_int(target.get("guest_steps")),
            -_as_int(target.get("module_calls")),
            _as_int(target.get("target")),
        ),
    )
    targets_by_calls = sorted(
        targets,
        key=lambda target: (
            -_as_int(target.get("module_calls")),
            -_as_int(target.get("guest_steps")),
            _as_int(target.get("target")),
        ),
    )
    lane_totals = {
        lane_name: {
            "module_calls": sum(
                _as_int(
                    ((target.get("execution_lanes") or {}).get(lane_name) or {}).get(
                        "module_calls"
                    )
                )
                for target in targets
            ),
            "guest_steps": sum(
                _as_int(
                    ((target.get("execution_lanes") or {}).get(lane_name) or {}).get(
                        "guest_steps"
                    )
                )
                for target in targets
            ),
        }
        for lane_name in ("primary", "worker", "vblank")
    }
    for lane in lane_totals.values():
        lane["call_ratio"] = (
            round(lane["module_calls"] / total_calls, 6)
            if total_calls
            else None
        )
        lane["step_ratio"] = (
            round(lane["guest_steps"] / total_steps, 6)
            if total_steps
            else None
        )
        lane["guest_steps_per_flip"] = (
            round(lane["guest_steps"] / captured_flip_count, 6)
            if captured_flip_count
            else None
        )

    confidence_counts = Counter(
        str(target.get("timing_confidence", "unavailable"))
        for target in targets
    )
    low_confidence_hot_targets = [
        target
        for target in targets_by_native_time
        if target.get("timing_confidence") == "low"
        and (_as_float(target.get("estimated_native_time_share")) or 0.0)
        >= 0.01
    ]
    unstable_targets = [
        target
        for target in targets_by_native_time
        if (_as_float(target.get("relative_margin_of_error_95")) or 0.0)
        > 0.50
    ]
    diagnostics: list[dict[str, Any]] = []
    if low_confidence_hot_targets:
        diagnostics.append(
            {
                "id": "hot_target_timing_needs_more_samples",
                "severity": "warning",
                "evidence": low_confidence_hot_targets[:16],
            }
        )
    if unstable_targets:
        diagnostics.append(
            {
                "id": "hot_target_timing_is_variable",
                "severity": "warning",
                "evidence": unstable_targets[:16],
            }
        )
    estimated_capture_ratio = (
        total_estimated_combined_time_us / captured_elapsed_us
        if captured_elapsed_us
        else None
    )
    if estimated_capture_ratio is not None and estimated_capture_ratio > 1.15:
        diagnostics.append(
            {
                "id": "sampled_target_time_exceeds_capture_wall_time",
                "severity": "high",
                "evidence": {
                    "estimated_capture_ratio": round(
                        estimated_capture_ratio, 6
                    ),
                    "likely_causes": [
                        "deterministic timing samples alias variable work",
                        "capture spans concurrent guest execution lanes",
                        "insufficient timing samples",
                    ],
                },
            }
        )
    if cycles and (
        _as_float(cycles[0].get("estimated_native_time_share")) or 0.0
    ) >= 0.30:
        diagnostics.append(
            {
                "id": "native_time_concentrates_in_guest_cycle",
                "severity": "info",
                "evidence": cycles[0],
            }
        )

    def concentration(
        ranked: list[dict[str, Any]], field: str, total: float
    ) -> dict[str, float | None]:
        return {
            f"top_{limit}_share": (
                round(
                    sum((_as_float(item.get(field)) or 0.0) for item in ranked[:limit])
                    / total,
                    9,
                )
                if total
                else None
            )
            for limit in (1, 5, 10, 25)
        }

    return {
        "status": (
            "complete"
            if target_by_address and edge_profile.get("exact") is True
            else "incomplete_edges"
            if target_by_address
            else "unavailable"
        ),
        "measurement": {
            "captured_elapsed_us": captured_elapsed_us,
            "captured_flip_count": captured_flip_count,
            "module_calls": total_calls,
            "guest_steps": total_steps,
            "estimated_native_time_us": round(
                total_estimated_combined_time_us, 3
            ),
            "estimated_guest_module_time_us": round(
                total_estimated_native_time_us, 3
            ),
            "estimated_native_host_service_time_us": round(
                total_estimated_service_time_us, 3
            ),
            "estimated_capture_ratio": (
                round(estimated_capture_ratio, 6)
                if estimated_capture_ratio is not None
                else None
            ),
            "estimated_native_us_per_flip": (
                round(total_estimated_combined_time_us / captured_flip_count, 6)
                if captured_flip_count
                else None
            ),
            "estimated_native_frame_budget_share": (
                round(
                    total_estimated_combined_time_us
                    / captured_flip_count
                    / TARGET_FRAME_US,
                    6,
                )
                if captured_flip_count
                else None
            ),
        },
        "accuracy": {
            "exact_target_counts": bool(target_by_address),
            "exact_transition_counts": edge_profile.get("exact") is True,
            "transition_overflow_module_calls": _as_int(
                edge_profile.get("overflow_module_calls")
            ),
            "transition_unclassified_module_calls": _as_int(
                edge_profile.get("unclassified_module_calls")
            ),
            "timing_mode": "deterministic_sampled",
            "timing_confidence_counts": dict(sorted(confidence_counts.items())),
        },
        "concentration": {
            "native_time": concentration(
                targets_by_native_time,
                "estimated_native_time_us",
                total_estimated_native_time_us,
            ),
            "guest_steps": concentration(
                targets_by_guest_steps,
                "guest_steps",
                float(total_steps),
            ),
            "module_calls": concentration(
                targets_by_calls,
                "module_calls",
                float(total_calls),
            ),
        },
        "execution_lanes": lane_totals,
        "rankings": {
            "by_estimated_native_time": targets_by_native_time[:128],
            "by_guest_steps": targets_by_guest_steps[:128],
            "by_module_calls": targets_by_calls[:128],
            "native_host_services": native_services[:128],
        },
        "targets": targets_by_native_time,
        "native_host_services": native_services,
        "transitions": {
            "unique_count": len(enriched_edges),
            "edges": enriched_edges,
            "branch_points": branch_points,
        },
        "cycles": {
            "count": len(cycles),
            "hot_cycles": cycles,
        },
        "diagnostics": diagnostics,
    }


def _findings(
    guest: dict[str, Any],
    bridge: dict[str, Any],
    presenter: dict[str, Any],
    native_compilation: dict[str, Any],
    native_frontier_interpreter: dict[str, Any],
    native_frontier_promotion: dict[str, Any],
    profile_integrity: dict[str, Any],
    hot_path_analysis: dict[str, Any],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    findings.extend(
        finding
        for finding in hot_path_analysis.get("diagnostics", [])
        if isinstance(finding, dict)
    )
    if profile_integrity.get("enabled") and not profile_integrity.get("accepted"):
        findings.append(
            {
                "id": "hot_path_profile_is_not_native_clean",
                "severity": "critical",
                "evidence": profile_integrity,
            }
        )
    overhead_ratios = [
        _as_float(
            (window.get("profiling_overhead_estimate") or {}).get(
                "estimated_capture_ratio"
            )
        )
        for window in profile_integrity.get("capture_windows", [])
        if isinstance(window, dict)
    ]
    overhead_ratios = [
        ratio for ratio in overhead_ratios if ratio is not None
    ]
    maximum_overhead_ratio = max(overhead_ratios, default=None)
    if maximum_overhead_ratio is not None and maximum_overhead_ratio >= 0.05:
        findings.append(
            {
                "id": "hot_path_profile_overhead_is_material",
                "severity": (
                    "high" if maximum_overhead_ratio >= 0.15 else "warning"
                ),
                "evidence": {
                    "maximum_estimated_capture_ratio": (
                        maximum_overhead_ratio
                    ),
                    "capture_windows": profile_integrity.get(
                        "capture_windows", []
                    ),
                },
            }
        )
    dropped_native_runs = _as_int(
        native_compilation.get("dropped_native_run_count")
    )
    if dropped_native_runs:
        findings.append(
            {
                "id": "native_run_history_is_bounded",
                "severity": "info",
                "evidence": {
                    "native_run_count": _as_int(
                        native_compilation.get("native_run_count")
                    ),
                    "retained_native_run_count": _as_int(
                        native_compilation.get("retained_native_run_count")
                    ),
                    "dropped_native_run_count": dropped_native_runs,
                    "compilation_totals_complete": False,
                },
            }
        )
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
    flip_measurement = _guest_flip_measurement(bridge, presenter)
    flip_rate = _as_float(flip_measurement.get("rate_hz"))
    if flip_rate is not None and flip_rate < TARGET_ACCEPTANCE_FPS:
        findings.append(
            {
                "id": "guest_below_60_fps_target",
                "severity": "critical" if flip_rate < TARGET_FPS * 0.75 else "high",
                "evidence": {
                    "guest_flip_rate_hz": round(flip_rate, 3),
                    "measurement_source": flip_measurement.get("source"),
                    "sample_count": flip_measurement.get("sample_count"),
                    "elapsed_seconds": flip_measurement.get(
                        "elapsed_seconds"
                    ),
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
                    "hot_targets_by_native_time": guest.get(
                        "native_dispatch_timing_targets", []
                    )[:8],
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
                        "and exact native dispatch edges."
                    )
                },
            }
        )

    order = {"critical": 0, "high": 1, "warning": 2, "info": 3}
    findings.sort(key=lambda item: (order.get(str(item["severity"]), 99), item["id"]))
    for priority, finding in enumerate(findings, start=1):
        finding["priority"] = priority
    return findings


def _compare_hot_path_reports(
    current_report: dict[str, Any],
    baseline_report: dict[str, Any],
) -> dict[str, Any]:
    current = current_report.get("hot_path_analysis") or {}
    baseline = baseline_report.get("hot_path_analysis") or {}
    if not current.get("targets") or not baseline.get("targets"):
        return {
            "status": "unavailable",
            "reason": "both reports must contain expanded hot-path target data",
        }
    current_targets = {
        _as_int(target.get("target")): target
        for target in current.get("targets", [])
        if isinstance(target, dict)
    }
    baseline_targets = {
        _as_int(target.get("target")): target
        for target in baseline.get("targets", [])
        if isinstance(target, dict)
    }

    def delta(
        current_value: float | None,
        baseline_value: float | None,
    ) -> dict[str, float | None]:
        absolute = (
            current_value - baseline_value
            if current_value is not None and baseline_value is not None
            else None
        )
        return {
            "current": current_value,
            "baseline": baseline_value,
            "absolute": round(absolute, 6) if absolute is not None else None,
            "ratio": (
                round(current_value / baseline_value, 6)
                if current_value is not None
                and baseline_value is not None
                and baseline_value != 0.0
                else None
            ),
        }

    target_deltas: list[dict[str, Any]] = []
    for address in sorted(current_targets.keys() | baseline_targets.keys()):
        current_target = current_targets.get(address) or {}
        baseline_target = baseline_targets.get(address) or {}
        target_deltas.append(
            {
                "target": address,
                "target_hex": f"0x{address:08X}",
                "presence": (
                    "both"
                    if current_target and baseline_target
                    else "current_only"
                    if current_target
                    else "baseline_only"
                ),
                "estimated_native_us_per_second": delta(
                    _as_float(
                        current_target.get("estimated_native_us_per_second")
                    ),
                    _as_float(
                        baseline_target.get("estimated_native_us_per_second")
                    ),
                ),
                "estimated_native_us_per_flip": delta(
                    _as_float(
                        current_target.get("estimated_native_us_per_flip")
                    ),
                    _as_float(
                        baseline_target.get("estimated_native_us_per_flip")
                    ),
                ),
                "guest_steps_per_second": delta(
                    _as_float(current_target.get("guest_steps_per_second")),
                    _as_float(baseline_target.get("guest_steps_per_second")),
                ),
                "module_calls_per_second": delta(
                    _as_float(current_target.get("module_calls_per_second")),
                    _as_float(baseline_target.get("module_calls_per_second")),
                ),
                "current_timing_confidence": current_target.get(
                    "timing_confidence"
                ),
                "baseline_timing_confidence": baseline_target.get(
                    "timing_confidence"
                ),
            }
        )
    comparable_deltas = [
        item
        for item in target_deltas
        if item["estimated_native_us_per_second"]["absolute"] is not None
    ]
    regressions = sorted(
        comparable_deltas,
        key=lambda item: (
            -item["estimated_native_us_per_second"]["absolute"],
            item["target"],
        ),
    )
    improvements = sorted(
        comparable_deltas,
        key=lambda item: (
            item["estimated_native_us_per_second"]["absolute"],
            item["target"],
        ),
    )
    current_services = {
        (
            _as_int(service.get("target")),
            _as_int(service.get("kind")),
            _as_int(service.get("value")),
        ): service
        for service in current.get("native_host_services", [])
        if isinstance(service, dict)
    }
    baseline_services = {
        (
            _as_int(service.get("target")),
            _as_int(service.get("kind")),
            _as_int(service.get("value")),
        ): service
        for service in baseline.get("native_host_services", [])
        if isinstance(service, dict)
    }
    native_service_deltas: list[dict[str, Any]] = []
    for key in sorted(current_services.keys() | baseline_services.keys()):
        address, kind, value = key
        current_service = current_services.get(key) or {}
        baseline_service = baseline_services.get(key) or {}
        native_service_deltas.append(
            {
                "target": address,
                "target_hex": f"0x{address:08X}",
                "kind": kind,
                "kind_name": current_service.get("kind_name")
                or baseline_service.get("kind_name"),
                "value": value,
                "presence": (
                    "both"
                    if current_service and baseline_service
                    else "current_only"
                    if current_service
                    else "baseline_only"
                ),
                "estimated_native_us_per_second": delta(
                    _as_float(
                        current_service.get("estimated_native_us_per_second")
                    ),
                    _as_float(
                        baseline_service.get("estimated_native_us_per_second")
                    ),
                ),
                "estimated_native_us_per_flip": delta(
                    _as_float(
                        current_service.get("estimated_native_us_per_flip")
                    ),
                    _as_float(
                        baseline_service.get("estimated_native_us_per_flip")
                    ),
                ),
                "calls_per_second": delta(
                    _as_float(current_service.get("calls_per_second")),
                    _as_float(baseline_service.get("calls_per_second")),
                ),
                "current_timing_confidence": current_service.get(
                    "timing_confidence"
                ),
                "baseline_timing_confidence": baseline_service.get(
                    "timing_confidence"
                ),
            }
        )
    comparable_service_deltas = [
        item
        for item in native_service_deltas
        if item["estimated_native_us_per_second"]["absolute"] is not None
    ]
    service_regressions = sorted(
        comparable_service_deltas,
        key=lambda item: (
            -item["estimated_native_us_per_second"]["absolute"],
            item["target"],
            item["kind"],
            item["value"],
        ),
    )
    service_improvements = sorted(
        comparable_service_deltas,
        key=lambda item: (
            item["estimated_native_us_per_second"]["absolute"],
            item["target"],
            item["kind"],
            item["value"],
        ),
    )
    current_measurement = current.get("measurement") or {}
    baseline_measurement = baseline.get("measurement") or {}
    return {
        "status": "comparable",
        "normalization": (
            "Per-second target cost is the primary comparison; per-flip cost is "
            "reported only when both controlled captures contain guest flips."
        ),
        "overall": {
            "guest_flip_rate_hz": delta(
                _as_float(
                    (current_report.get("target") or {}).get(
                        "guest_flip_rate_hz"
                    )
                ),
                _as_float(
                    (baseline_report.get("target") or {}).get(
                        "guest_flip_rate_hz"
                    )
                ),
            ),
            "estimated_capture_ratio": delta(
                _as_float(current_measurement.get("estimated_capture_ratio")),
                _as_float(baseline_measurement.get("estimated_capture_ratio")),
            ),
            "estimated_native_us_per_flip": delta(
                _as_float(
                    current_measurement.get("estimated_native_us_per_flip")
                ),
                _as_float(
                    baseline_measurement.get("estimated_native_us_per_flip")
                ),
            ),
        },
        "matched_target_count": len(comparable_deltas),
        "current_only_target_count": sum(
            item["presence"] == "current_only" for item in target_deltas
        ),
        "baseline_only_target_count": sum(
            item["presence"] == "baseline_only" for item in target_deltas
        ),
        "largest_regressions": [
            item
            for item in regressions
            if item["estimated_native_us_per_second"]["absolute"] > 0.0
        ][:128],
        "largest_improvements": [
            item
            for item in improvements
            if item["estimated_native_us_per_second"]["absolute"] < 0.0
        ][:128],
        "targets": target_deltas,
        "native_host_services": {
            "matched_count": len(comparable_service_deltas),
            "largest_regressions": [
                item
                for item in service_regressions
                if item["estimated_native_us_per_second"]["absolute"] > 0.0
            ][:128],
            "largest_improvements": [
                item
                for item in service_improvements
                if item["estimated_native_us_per_second"]["absolute"] < 0.0
            ][:128],
            "targets": native_service_deltas,
        },
    }


def build_performance_debug_report(
    probe_summary_path: Path,
    *,
    render_debug_report_path: Path | None = None,
    baseline_report_path: Path | None = None,
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
    profile_integrity = _summarize_profile_integrity(
        execution,
        guest,
        native_frontier_interpreter,
        native_frontier_promotion,
    )
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
    flip_measurement = _guest_flip_measurement(bridge, presenter)
    hot_path_analysis = _summarize_hot_path_analysis(
        guest,
        flip_measurement,
    )
    measured_flip_rate = _as_float(flip_measurement.get("rate_hz"))
    target_attainment = (
        round(measured_flip_rate / TARGET_FPS, 6)
        if measured_flip_rate is not None
        else None
    )
    findings = _findings(
        guest,
        bridge,
        presenter,
        native_compilation,
        native_frontier_interpreter,
        native_frontier_promotion,
        profile_integrity,
        hot_path_analysis,
    )
    critical_or_high = any(
        finding["severity"] in {"critical", "high"} for finding in findings
    )

    report = {
        "format": "b2-recomp-performance-debug-report",
        "public_safe": False,
        "status": (
            "invalid_profile_boundary"
            if profile_integrity.get("enabled")
            and not profile_integrity.get("accepted")
            else "insufficient_native_evidence"
            if not guest.get("native_run_count")
            else "target_met"
            if measured_flip_rate is not None
            and measured_flip_rate >= TARGET_ACCEPTANCE_FPS
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
                "Per-target call, step, entry-to-exit edge, and module-exit-reason "
                "accounting is exact; native target time and profiling bookkeeping "
                "cost use deterministic sampling. Profiling is opt-in diagnostic "
                "metadata and does not alter guest control flow."
            ),
            "gpu_command_read_scope": (
                "The GPU command-kick address retains its exact native write hook; "
                "ordinary reads use the coherent push-buffer cache because the title "
                "read model has no command-kick side effect."
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
            "baseline_report_path": (
                str(baseline_report_path)
                if baseline_report_path is not None
                else None
            ),
        },
        "profiling": profile_integrity,
        "hot_path_analysis": hot_path_analysis,
        "target": {
            "fps": TARGET_FPS,
            "acceptance_floor_fps": TARGET_ACCEPTANCE_FPS,
            "frame_budget_us": TARGET_FRAME_US,
            "guest_flip_rate_hz": measured_flip_rate,
            "guest_flip_rate_source": flip_measurement.get("source"),
            "guest_flip_sample_count": flip_measurement.get("sample_count"),
            "guest_flip_elapsed_seconds": flip_measurement.get(
                "elapsed_seconds"
            ),
            "target_attainment_ratio": target_attainment,
            "recent_guest_flip_rate_hz": _as_float(
                bridge.get("recent_guest_flip_rate_hz")
            ),
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
            "profile_integrity": profile_integrity,
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
            "native_dispatch_timing_targets": guest.get(
                "native_dispatch_timing_targets"
            ),
            "native_host_services": guest.get(
                "native_host_service_hot_targets"
            ),
            "memory_callbacks": guest.get("memory_callback_sampling"),
            "legacy_read_callbacks": guest.get("read_callback_sampling"),
        },
        "live_host_bridge": bridge,
        "presenter": presenter,
        "findings": findings,
    }
    if baseline_report_path is not None:
        baseline_report = json.loads(
            baseline_report_path.read_text(encoding="utf-8")
        )
        report["comparison"] = _compare_hot_path_reports(
            report,
            baseline_report,
        )
    else:
        report["comparison"] = {
            "status": "not_requested",
        }
    return report


def write_performance_debug_report(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def write_hot_path_tables(
    report: dict[str, Any], directory: Path
) -> list[Path]:
    """Write sortable lossless tables for the captured target graph."""
    analysis = report.get("hot_path_analysis") or {}
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def write_rows(
        name: str,
        fieldnames: list[str],
        rows: list[dict[str, Any]],
    ) -> None:
        path = directory / name
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=fieldnames,
                extrasaction="ignore",
                lineterminator="\n",
            )
            writer.writeheader()
            writer.writerows(rows)
        written.append(path)

    target_rows: list[dict[str, Any]] = []
    for target in analysis.get("targets", []):
        lanes = target.get("execution_lanes") or {}
        target_rows.append(
            {
                **target,
                "module_exit_reasons": json.dumps(
                    target.get("module_exit_reasons") or {},
                    sort_keys=True,
                ),
                "primary_calls": _as_int(
                    (lanes.get("primary") or {}).get("module_calls")
                ),
                "primary_steps": _as_int(
                    (lanes.get("primary") or {}).get("guest_steps")
                ),
                "worker_calls": _as_int(
                    (lanes.get("worker") or {}).get("module_calls")
                ),
                "worker_steps": _as_int(
                    (lanes.get("worker") or {}).get("guest_steps")
                ),
                "vblank_calls": _as_int(
                    (lanes.get("vblank") or {}).get("module_calls")
                ),
                "vblank_steps": _as_int(
                    (lanes.get("vblank") or {}).get("guest_steps")
                ),
            }
        )
    write_rows(
        "targets.csv",
        [
            "target_hex",
            "module_calls",
            "guest_steps",
            "estimated_native_time_us",
            "estimated_native_time_share",
            "estimated_native_us_per_flip",
            "frame_budget_share",
            "sampled_average_ns",
            "sampled_min_ns",
            "sampled_max_ns",
            "sample_standard_deviation_ns",
            "relative_margin_of_error_95",
            "timing_sample_count",
            "timing_confidence",
            "sampled_ns_per_guest_step",
            "estimated_native_ns_per_guest_step",
            "module_calls_per_flip",
            "guest_steps_per_flip",
            "incoming_fan_in",
            "outgoing_fan_out",
            "cycle_id",
            "dominant_execution_lane",
            "primary_calls",
            "primary_steps",
            "worker_calls",
            "worker_steps",
            "vblank_calls",
            "vblank_steps",
            "module_exit_reasons",
        ],
        target_rows,
    )
    edge_rows = [
        {
            **edge,
            "module_exit_reasons": json.dumps(
                edge.get("module_exit_reasons") or {},
                sort_keys=True,
            ),
        }
        for edge in (analysis.get("transitions") or {}).get("edges", [])
    ]
    write_rows(
        "transitions.csv",
        [
            "entry_target_hex",
            "exit_target_hex",
            "module_calls",
            "source_transition_ratio",
            "captured_transition_share",
            "transitions_per_flip",
            "terminal",
            "module_exit_reasons",
        ],
        edge_rows,
    )
    cycle_rows = [
        {
            **cycle,
            "targets": " ".join(cycle.get("targets") or []),
            "top_targets": json.dumps(
                cycle.get("top_targets") or [],
                sort_keys=True,
            ),
        }
        for cycle in (analysis.get("cycles") or {}).get("hot_cycles", [])
    ]
    write_rows(
        "cycles.csv",
        [
            "rank",
            "cycle_id",
            "target_count",
            "module_calls",
            "guest_steps",
            "estimated_native_time_us",
            "estimated_native_time_share",
            "internal_transition_count",
            "targets",
            "top_targets",
        ],
        cycle_rows,
    )
    service_rows = []
    for service in analysis.get("native_host_services", []):
        lanes = service.get("execution_lanes") or {}
        service_rows.append(
            {
                **service,
                "primary_calls": _as_int(
                    (lanes.get("primary") or {}).get("calls")
                ),
                "worker_calls": _as_int(
                    (lanes.get("worker") or {}).get("calls")
                ),
                "vblank_calls": _as_int(
                    (lanes.get("vblank") or {}).get("calls")
                ),
            }
        )
    write_rows(
        "native-services.csv",
        [
            "target_hex",
            "kind_name",
            "kind",
            "value",
            "calls",
            "call_share",
            "estimated_native_time_us",
            "estimated_native_time_share",
            "estimated_native_us_per_second",
            "estimated_native_us_per_flip",
            "frame_budget_share",
            "timing_sample_count",
            "sampled_average_ns",
            "sampled_min_ns",
            "sampled_max_ns",
            "sample_standard_deviation_ns",
            "relative_margin_of_error_95",
            "timing_confidence",
            "primary_calls",
            "worker_calls",
            "vblank_calls",
        ],
        service_rows,
    )
    comparison_rows = []
    for target in (report.get("comparison") or {}).get("targets", []):
        native_rate = target.get("estimated_native_us_per_second") or {}
        native_per_flip = target.get("estimated_native_us_per_flip") or {}
        step_rate = target.get("guest_steps_per_second") or {}
        call_rate = target.get("module_calls_per_second") or {}
        comparison_rows.append(
            {
                "target_hex": target.get("target_hex"),
                "presence": target.get("presence"),
                "current_native_us_per_second": native_rate.get("current"),
                "baseline_native_us_per_second": native_rate.get("baseline"),
                "native_us_per_second_delta": native_rate.get("absolute"),
                "native_us_per_second_ratio": native_rate.get("ratio"),
                "current_native_us_per_flip": native_per_flip.get("current"),
                "baseline_native_us_per_flip": native_per_flip.get("baseline"),
                "native_us_per_flip_delta": native_per_flip.get("absolute"),
                "current_guest_steps_per_second": step_rate.get("current"),
                "baseline_guest_steps_per_second": step_rate.get("baseline"),
                "guest_steps_per_second_delta": step_rate.get("absolute"),
                "current_module_calls_per_second": call_rate.get("current"),
                "baseline_module_calls_per_second": call_rate.get("baseline"),
                "module_calls_per_second_delta": call_rate.get("absolute"),
                "current_timing_confidence": target.get(
                    "current_timing_confidence"
                ),
                "baseline_timing_confidence": target.get(
                    "baseline_timing_confidence"
                ),
            }
        )
    if comparison_rows:
        write_rows(
            "comparison.csv",
            list(comparison_rows[0]),
            comparison_rows,
        )
    service_comparison_rows = []
    for service in (
        ((report.get("comparison") or {}).get("native_host_services") or {}).get(
            "targets", []
        )
    ):
        native_rate = service.get("estimated_native_us_per_second") or {}
        native_per_flip = service.get("estimated_native_us_per_flip") or {}
        call_rate = service.get("calls_per_second") or {}
        service_comparison_rows.append(
            {
                "target_hex": service.get("target_hex"),
                "kind_name": service.get("kind_name"),
                "kind": service.get("kind"),
                "value": service.get("value"),
                "presence": service.get("presence"),
                "current_native_us_per_second": native_rate.get("current"),
                "baseline_native_us_per_second": native_rate.get("baseline"),
                "native_us_per_second_delta": native_rate.get("absolute"),
                "native_us_per_second_ratio": native_rate.get("ratio"),
                "current_native_us_per_flip": native_per_flip.get("current"),
                "baseline_native_us_per_flip": native_per_flip.get("baseline"),
                "native_us_per_flip_delta": native_per_flip.get("absolute"),
                "current_calls_per_second": call_rate.get("current"),
                "baseline_calls_per_second": call_rate.get("baseline"),
                "calls_per_second_delta": call_rate.get("absolute"),
                "current_timing_confidence": service.get(
                    "current_timing_confidence"
                ),
                "baseline_timing_confidence": service.get(
                    "baseline_timing_confidence"
                ),
            }
        )
    if service_comparison_rows:
        write_rows(
            "native-service-comparison.csv",
            list(service_comparison_rows[0]),
            service_comparison_rows,
        )
    return written


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
    parser.add_argument(
        "--compare-to",
        type=Path,
        help="Optional prior performance report for normalized target regression ranking.",
    )
    parser.add_argument(
        "--tables-directory",
        type=Path,
        help="Optional directory for sortable targets, transitions, and cycles CSVs.",
    )
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    report = build_performance_debug_report(
        args.probe_summary,
        render_debug_report_path=args.render_debug_report,
        baseline_report_path=args.compare_to,
    )
    write_performance_debug_report(report, args.json_output)
    if args.tables_directory is not None:
        write_hot_path_tables(report, args.tables_directory)
    print(json.dumps(report, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
