#!/usr/bin/env python3
"""Correlate guest vertex provenance with host NV2A geometry diagnostics."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
DEFAULT_XBE = (
    REPO_ROOT
    / "data"
    / "local"
    / "extracted"
    / "burnout_2_poi_usa"
    / "default.xbe"
)
DEFAULT_PROBE_SUMMARY = (
    REPO_ROOT / "reports" / "local" / "playability" / "native-live.json"
)
DEFAULT_PRESENTER_EVENTS = (
    REPO_ROOT / "reports" / "local" / "playability" / "render-debug-events.jsonl"
)
DEFAULT_STREAM_ANALYSIS_EVENTS = (
    REPO_ROOT
    / "reports"
    / "local"
    / "playability"
    / "render-stream-analysis.jsonl"
)
DEFAULT_REPORT = (
    REPO_ROOT / "reports" / "local" / "playability" / "render-debug-report.json"
)

GEOMETRY_COUNT_FIELDS = (
    "invalid_geometry_range_count",
    "non_finite_position_draw_count",
    "collapsed_x_draw_count",
    "collapsed_y_draw_count",
    "zero_area_draw_count",
    "exact_center_origin_draw_count",
    "fully_offscreen_draw_count",
    "outside_viewport_draw_count",
)

PRESENTER_EVENT_NAMES = (
    "nv2a_presented_geometry_anomalies",
    "live_render_stream_reloaded",
    "live_render_stream_reload_skipped",
    "frame_presented",
    "nv2a_native_resources_created",
    "render_validation",
    "d3d8_stream_interpreted",
    "frame_readback_captured",
    "nv2a_vertex_transform_diagnostics",
    "nv2a_render_state_diagnostics",
    "nv2a_presented_transform_diagnostics",
    "nv2a_presented_transform_constant",
    "nv2a_fixed_function_vertex_diagnostics",
    "nv2a_offscreen_render_targets_created",
    "nv2a_gpu_raw_attribute_validation",
    "nv2a_gpu_raw_dirty_range_validation",
    "nv2a_raw_vertex_buffers_refreshed",
    "nv2a_texture_resources_refreshed",
    "nv2a_graphics_pipeline_created",
    "nv2a_gpu_texture_conversion_pipeline_created",
    "nv2a_gpu_texture_conversion_batch",
    "nv2a_gpu_texture_conversion_validation",
    "hot_path_profile_capture",
    "hotkey_screenshot_queued",
    "hotkey_render_capture_retained",
    "render_stream_continuation_bootstrapped",
    "render_stream_analysis_complete",
)

RELOAD_STAGE_FIELDS = (
    "wait_us",
    "source_residency_us",
    "command_load_us",
    "interpret_us",
    "method_interpret_us",
    "push_buffer_collect_us",
    "method_apply_us",
    "method_finalize_us",
    "indexed_materialize_us",
    "resource_update_us",
    "resource_prepare_us",
    "resource_preflight_us",
    "resource_destroy_us",
    "native_resource_create_us",
    "vertex_resource_prepare_us",
    "state_resource_prepare_us",
    "texture_resource_prepare_us",
    "texture_refresh_us",
    "offscreen_resource_prepare_us",
    "resource_bookkeeping_us",
    "pipeline_prepare_us",
    "pipeline_state_discovery_us",
    "feedback_spec_build_us",
    "texture_binding_update_us",
    "render_validation_us",
    "vertex_transform_us",
    "vertex_state_upload_us",
    "raw_vertex_upload_us",
    "vertex_map_us",
    "vertex_copy_us",
    "command_record_us",
    "total_us",
)

FRAME_BOUNDARY_FIELDS = (
    "live_reload_us",
    "window_message_pump_us",
    "controller_poll_us",
    "keyboard_latch_us",
    "reload_probe_us",
    "pre_render_unattributed_us",
    "reload_fence_wait_us",
    "draw_fence_wait_us",
    "gpu_query_us",
    "fence_reset_us",
    "acquire_us",
    "submit_us",
    "readback_wait_us",
    "readback_process_us",
    "present_us",
    "audit_ack_us",
    "draw_unattributed_us",
)

NATIVE_COUNTER_FIELDS = (
    "native_dispatch_count",
    "native_module_call_count",
    "profiled_native_module_call_count",
    "native_host_service_call_count",
    "native_cold_host_call_count",
    "handler_call_count",
    "call_handler_yield_count",
    "slice_yield_count",
    "predicate_yield_count",
    "read_u32_callback_count",
    "read_u8_callback_count",
    "exact_read_u32_callback_count",
    "exact_read_u8_callback_count",
    "page_miss_read_u32_callback_count",
    "page_miss_read_u8_callback_count",
    "write_u32_callback_count",
    "write_u8_callback_count",
    "native_observed_write_count",
    "native_observed_write_batch_count",
    "native_observed_write_span_count",
    "native_observed_write_span_batch_count",
    "native_observed_write_span_byte_count",
    "observer_callback_count",
    "page_cache_fill_count",
    "dirty_sync_call_count",
    "dirty_sync_no_work_count",
    "selective_dirty_sync_call_count",
    "selective_dirty_sync_no_match_count",
    "selective_dirty_sync_retained_page_count",
    "selective_dirty_sync_page_writeback_count",
    "dirty_page_scan_count",
    "dirty_page_writeback_count",
    "dirty_byte_writeback_count",
    "invalidation_call_count",
    "invalidation_page_scan_count",
    "invalidated_page_count",
    "cached_range_refresh_count",
    "cached_range_refresh_byte_count",
    "cached_range_refresh_no_change_count",
    "observer_drain_deferred_count",
    "empty_dependency_sync_bypass_count",
    "memory_callback_policy_cache_hit_count",
    "memory_callback_policy_cache_miss_count",
    "observed_write_packet_yield_count",
    "zero_read_callback_bypass_count",
    "direct_observed_write_count",
    "direct_observed_write_byte_count",
)


def _read_presenter_events(
    path: Path,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    retained_events = {name: [] for name in PRESENTER_EVENT_NAMES}
    event_counts: Counter[str] = Counter()
    invalid_line_count = 0
    if path.is_file():
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    invalid_line_count += 1
                    continue
                event_name = str(event.get("event", ""))
                event_counts[event_name] += 1
                if event_name in retained_events:
                    retained_events[event_name].append(event)
    return retained_events, {
        "path": str(path),
        "exists": path.is_file(),
        "event_count": sum(event_counts.values()),
        "invalid_line_count": invalid_line_count,
        "event_counts": dict(sorted(event_counts.items())),
    }


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _profile_capture_window(events: list[dict[str, Any]]) -> dict[str, Any]:
    armed = next(
        (event for event in events if event.get("state") == "armed"),
        None,
    )
    active = next(
        (event for event in events if event.get("state") == "active"),
        None,
    )
    active_sequence = (
        _as_int(active.get("sequence"), -1) if active is not None else None
    )
    complete = next(
        (
            event
            for event in events
            if event.get("state") == "complete"
            and active_sequence is not None
            and _as_int(event.get("sequence"), -1) > active_sequence
        ),
        None,
    )
    complete_sequence = (
        _as_int(complete.get("sequence"), -1)
        if complete is not None
        else None
    )
    if active_sequence is not None:
        state = (
            "complete"
            if complete_sequence is not None
            else "active_until_shutdown"
        )
    elif armed is not None:
        state = "armed"
    else:
        state = "full_session"
    return {
        "controlled": bool(events),
        "state": state,
        "start_sequence": active_sequence,
        "end_sequence": complete_sequence,
        "started_guest_flip_count": (
            _as_int(active.get("guest_flip_count"))
            if active is not None
            else None
        ),
        "completed_guest_flip_count": (
            _as_int(complete.get("guest_flip_count"))
            if complete is not None
            else None
        ),
    }


def _events_in_profile_capture_window(
    events: list[dict[str, Any]],
    window: dict[str, Any],
) -> list[dict[str, Any]]:
    if not window.get("controlled"):
        return events
    start_sequence = window.get("start_sequence")
    if start_sequence is None:
        return []
    end_sequence = window.get("end_sequence")
    return [
        event
        for event in events
        if _as_int(event.get("sequence"), -1) > int(start_sequence)
        and (
            end_sequence is None
            or _as_int(event.get("sequence"), -1) < int(end_sequence)
        )
    ]


def _percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction)))
    return ordered[index]


def _duration_summary(values: list[int]) -> dict[str, Any]:
    total = sum(values)
    return {
        "sample_count": len(values),
        "total_us": total,
        "average_us": round(total / len(values), 3) if values else None,
        "p50_us": _percentile(values, 0.50),
        "p95_us": _percentile(values, 0.95),
        "p99_us": _percentile(values, 0.99),
        "maximum_us": max(values) if values else None,
    }


def _summarize_presenter_performance(
    reloads: list[dict[str, Any]],
    frames: list[dict[str, Any]],
    skipped_reloads: list[dict[str, Any]],
    raw_vertex_uploads: list[dict[str, Any]],
    texture_refreshes: list[dict[str, Any]],
    graphics_pipeline_creations: list[dict[str, Any]],
    compute_pipeline_creations: list[dict[str, Any]],
    texture_conversion_batches: list[dict[str, Any]],
    texture_conversion_validations: list[dict[str, Any]],
    *,
    profile_capture_window: dict[str, Any] | None = None,
) -> dict[str, Any]:
    stages = {
        field: _duration_summary(
            [_as_int(event.get(field)) for event in reloads if event.get(field) is not None]
        )
        for field in RELOAD_STAGE_FIELDS
    }
    non_overlapping = [
        field for field in RELOAD_STAGE_FIELDS if field != "total_us"
    ]
    hot_paths = sorted(
        (
            {
                "stage": field.removesuffix("_us"),
                **stages[field],
            }
            for field in non_overlapping
        ),
        key=lambda item: -_as_int(item.get("total_us")),
    )
    elapsed_us = [
        (
            _as_int(event.get("elapsed_us"))
            if event.get("elapsed_us") is not None
            else _as_int(event.get("elapsed_ms")) * 1000
        )
        for event in frames
        if event.get("elapsed_us") is not None
        or event.get("elapsed_ms") is not None
    ]
    target_us = [
        (
            _as_int(event.get("target_frame_us"))
            if event.get("target_frame_us") is not None
            else int(float(event.get("target_frame_ms", 16)) * 1000)
        )
        for event in frames
        if event.get("elapsed_us") is not None
        or event.get("elapsed_ms") is not None
    ]
    missed_target = sum(
        elapsed > target
        for elapsed, target in zip(elapsed_us, target_us, strict=True)
    )
    reload_cursor = 0
    previous_frame_sequence = -1
    attributed_frames: list[dict[str, Any]] = []
    for frame in frames:
        attributed = dict(frame)
        frame_sequence = _as_int(frame.get("sequence"), -1)
        live_reload_us = 0
        if frame_sequence >= 0:
            while reload_cursor < len(reloads):
                reload_sequence = _as_int(
                    reloads[reload_cursor].get("sequence"), -1
                )
                if reload_sequence < 0:
                    reload_cursor += 1
                    continue
                if reload_sequence >= frame_sequence:
                    break
                if reload_sequence > previous_frame_sequence:
                    live_reload_us += _as_int(
                        reloads[reload_cursor].get("total_us")
                    )
                reload_cursor += 1
            previous_frame_sequence = frame_sequence
        if live_reload_us or reloads:
            attributed["live_reload_us"] = live_reload_us
        attributed_frames.append(attributed)
    boundary_frames = [
        event
        for event in attributed_frames
        if any(event.get(field) is not None for field in FRAME_BOUNDARY_FIELDS)
    ]
    boundary_stages = {
        field: _duration_summary(
            [_as_int(event.get(field)) for event in boundary_frames]
        )
        for field in FRAME_BOUNDARY_FIELDS
    }
    slow_boundary_frames: list[dict[str, Any]] = []
    dominant_boundary_counts: Counter[str] = Counter()
    for event in boundary_frames:
        elapsed = _as_int(event.get("elapsed_us"))
        target = _as_int(event.get("target_frame_us"), 16_667)
        if elapsed <= target * 2:
            continue
        boundary_values = {
            field: _as_int(event.get(field))
            for field in FRAME_BOUNDARY_FIELDS
        }
        dominant_boundary = max(
            FRAME_BOUNDARY_FIELDS,
            key=lambda field: boundary_values[field],
        )
        dominant_boundary_counts[dominant_boundary.removesuffix("_us")] += 1
        slow_boundary_frames.append(
            {
                "sequence": event.get("sequence"),
                "frame": event.get("frame"),
                "elapsed_us": elapsed,
                "draw_us": _as_int(event.get("draw_us")),
                "cpu_render_us": _as_int(event.get("cpu_render_us")),
                "dominant_boundary": dominant_boundary.removesuffix("_us"),
                "dominant_boundary_us": boundary_values[dominant_boundary],
                **boundary_values,
            }
        )
    slow_boundary_frames.sort(
        key=lambda event: -_as_int(event.get("elapsed_us"))
    )
    reload_busy_us = sum(
        _as_int(event.get("total_us"))
        for event in reloads
        if event.get("total_us") is not None
    )
    elapsed_total_us = sum(elapsed_us)
    reloads_by_manifest_flip: dict[int, list[dict[str, Any]]] = {}
    for event in reloads:
        manifest_flip = _as_int(event.get("manifest_guest_flip_count"))
        if manifest_flip <= 0:
            continue
        reloads_by_manifest_flip.setdefault(manifest_flip, []).append(event)
    repeated_manifest_flips = sorted(
        (
            {
                "manifest_guest_flip_count": manifest_flip,
                "reload_count": len(events),
                "redundant_reload_count": max(0, len(events) - 1),
                "total_us": sum(_as_int(event.get("total_us")) for event in events),
                "first_writes": events[0].get("writes"),
                "last_writes": events[-1].get("writes"),
            }
            for manifest_flip, events in reloads_by_manifest_flip.items()
            if len(events) > 1
        ),
        key=lambda item: (-item["reload_count"], item["manifest_guest_flip_count"]),
    )
    latest_reload = reloads[-1] if reloads else {}
    gpu_program_vertices = _as_int(
        latest_reload.get("gpu_vertex_program_vertices")
    )
    cpu_fallback_vertices = _as_int(
        latest_reload.get("cpu_vertex_program_fallback_vertices")
    )
    programmable_vertices = gpu_program_vertices + cpu_fallback_vertices
    gpu_raw_attribute_vertices = _as_int(
        latest_reload.get("gpu_raw_attribute_vertices")
    )
    detailed_raw_uploads = [
        event
        for event in raw_vertex_uploads
        if event.get("resource_upload_bytes") is not None
    ]
    resource_upload_bytes = sum(
        _as_int(event.get("resource_upload_bytes"))
        for event in detailed_raw_uploads
    )
    index_upload_bytes = sum(
        _as_int(event.get("index_upload_bytes"))
        for event in detailed_raw_uploads
    )
    eligible_resource_bytes = sum(
        _as_int(event.get("resource_bytes"))
        for event in detailed_raw_uploads
    )
    latest_raw_upload = detailed_raw_uploads[-1] if detailed_raw_uploads else {}
    gpu_texture_input_bytes = sum(
        _as_int(event.get("input_bytes"))
        for event in texture_conversion_batches
    )
    gpu_texture_output_bytes = sum(
        _as_int(event.get("output_bytes"))
        for event in texture_conversion_batches
    )
    latest_texture_validation = (
        texture_conversion_validations[-1]
        if texture_conversion_validations
        else {}
    )
    offscreen_reload_samples = [
        event
        for event in reloads
        if event.get("offscreen_render_target_count") is not None
    ]
    offscreen_target_reloads = [
        event
        for event in offscreen_reload_samples
        if _as_int(event.get("offscreen_render_target_count")) > 0
    ]
    reused_offscreen_target_reloads = [
        event
        for event in offscreen_target_reloads
        if event.get("offscreen_render_targets_reused") is True
    ]
    recreated_offscreen_target_reloads = [
        event
        for event in offscreen_target_reloads
        if event.get("offscreen_render_targets_reused") is not True
    ]
    presentation_pipeline_reloads = [
        event
        for event in reloads
        if event.get("presentation_pipeline_depth") is not None
    ]
    pipelined_presentation_reloads = [
        event
        for event in presentation_pipeline_reloads
        if _as_int(event.get("presentation_pipeline_depth")) == 2
    ]
    presentation_pipeline_depth_counts = Counter(
        _as_int(event.get("presentation_pipeline_depth"))
        for event in presentation_pipeline_reloads
    )
    diagnostic_sampling_reloads = [
        event
        for event in reloads
        if event.get("presented_diagnostics_sampled") is not None
    ]
    sampled_diagnostic_reloads = [
        event
        for event in diagnostic_sampling_reloads
        if event.get("presented_diagnostics_sampled") is True
    ]
    unsampled_diagnostic_reloads = [
        event
        for event in diagnostic_sampling_reloads
        if event.get("presented_diagnostics_sampled") is not True
    ]
    changed_resource_reloads = [
        event
        for event in diagnostic_sampling_reloads
        if event.get("resource_generation_changed") is True
    ]
    command_loading_reloads = [
        event for event in reloads if event.get("command_transport") is not None
    ]
    command_transport_counts = Counter(
        str(event.get("command_transport")) for event in command_loading_reloads
    )
    command_file_reuse_reloads = [
        event
        for event in command_loading_reloads
        if event.get("command_file_reused") is not None
    ]
    reused_command_file_reloads = [
        event
        for event in command_file_reuse_reloads
        if event.get("command_file_reused") is True
    ]
    reopened_command_file_reloads = [
        event
        for event in command_file_reuse_reloads
        if event.get("command_file_reused") is not True
    ]
    command_read_bytes = sum(
        _as_int(event.get("command_read_bytes"))
        for event in command_loading_reloads
    )
    command_file_read_us = sum(
        _as_int(event.get("command_file_read_us"))
        for event in command_loading_reloads
        if event.get("command_file_read_us") is not None
    )
    resource_snapshot_reuse_reloads = [
        event
        for event in reloads
        if event.get("resource_snapshot_reused_resources") is not None
        and event.get("resource_snapshot_reused_bytes") is not None
    ]
    reused_resource_snapshot_reloads = [
        event
        for event in resource_snapshot_reuse_reloads
        if _as_int(event.get("resource_snapshot_reused_resources")) > 0
    ]
    texture_binding_reloads = [
        event
        for event in reloads
        if event.get("texture_binding_set_reused") is not None
    ]
    reused_texture_binding_reloads = [
        event
        for event in texture_binding_reloads
        if event.get("texture_binding_set_reused") is True
    ]
    feedback_image_cache_reloads = [
        event
        for event in reloads
        if event.get("render_target_feedback_image_cache_hits") is not None
    ]
    feedback_image_cache_hit_count = sum(
        _as_int(event.get("render_target_feedback_image_cache_hits"))
        for event in feedback_image_cache_reloads
    )
    feedback_image_cache_miss_count = sum(
        _as_int(event.get("render_target_feedback_image_cache_misses"))
        for event in feedback_image_cache_reloads
    )
    feedback_image_cache_lookup_count = (
        feedback_image_cache_hit_count + feedback_image_cache_miss_count
    )
    method_interpretation_reloads = [
        event
        for event in reloads
        if event.get("interpreted_method_delta") is not None
    ]
    interpreted_method_count = sum(
        _as_int(event.get("interpreted_method_delta"))
        for event in method_interpretation_reloads
    )
    bulk_indexed_method_count = sum(
        _as_int(event.get("bulk_indexed_method_delta"))
        for event in method_interpretation_reloads
    )
    bulk_inline_method_count = sum(
        _as_int(event.get("bulk_inline_method_delta"))
        for event in method_interpretation_reloads
    )
    bulk_method_count = bulk_indexed_method_count + bulk_inline_method_count
    state_seed_status_reloads = [
        event
        for event in method_interpretation_reloads
        if event.get("state_seed_updates_required") is not None
    ]
    state_seed_update_reload_count = sum(
        1
        for event in state_seed_status_reloads
        if event.get("state_seed_updates_required") is True
    )
    resource_breakdown_reloads = [
        event
        for event in reloads
        if event.get("native_resource_create_us") is not None
    ]
    texture_indexed_lookup_count = sum(
        _as_int(event.get("texture_indexed_lookup_count"))
        for event in resource_breakdown_reloads
    )
    texture_indexed_lookup_candidate_count = sum(
        _as_int(event.get("texture_indexed_lookup_candidates"))
        for event in resource_breakdown_reloads
    )
    return {
        "profile_capture_window": profile_capture_window,
        "reload_count": len(reloads),
        "reload_stages": stages,
        "hot_paths_by_total_time": hot_paths,
        "reload_busy_seconds": round(reload_busy_us / 1_000_000, 6),
        "reload_busy_ratio": (
            round(reload_busy_us / elapsed_total_us, 6)
            if elapsed_total_us
            else None
        ),
        "redundant_same_flip_reload_count": sum(
            item["redundant_reload_count"] for item in repeated_manifest_flips
        ),
        "skipped_redundant_reload_count": len(skipped_reloads),
        "latest_skipped_reload": skipped_reloads[-1] if skipped_reloads else None,
        "repeated_manifest_flips": repeated_manifest_flips[:32],
        "presentation_pipeline": {
            "instrumented_reload_count": len(presentation_pipeline_reloads),
            "depth_counts": {
                str(depth): count
                for depth, count in sorted(
                    presentation_pipeline_depth_counts.items()
                )
            },
            "latest_depth": (
                _as_int(
                    presentation_pipeline_reloads[-1].get(
                        "presentation_pipeline_depth"
                    )
                )
                if presentation_pipeline_reloads
                else None
            ),
            "pipelined_reload_count": len(pipelined_presentation_reloads),
            "acknowledgement_publish_count": sum(
                event.get("presentation_ack_published") is True
                for event in presentation_pipeline_reloads
            ),
            "ack_write_us": _duration_summary(
                [
                    _as_int(event.get("presentation_ack_us"))
                    for event in presentation_pipeline_reloads
                    if event.get("presentation_ack_us") is not None
                ]
            ),
            "pre_ack_us": _duration_summary(
                [
                    _as_int(event.get("pre_ack_us"))
                    for event in presentation_pipeline_reloads
                    if event.get("pre_ack_us") is not None
                ]
            ),
            "source_residency_us": _duration_summary(
                [
                    _as_int(event.get("source_residency_us"))
                    for event in presentation_pipeline_reloads
                    if event.get("source_residency_us") is not None
                ]
            ),
            "pipelined_post_ack_us": _duration_summary(
                [
                    _as_int(event.get("post_ack_us"))
                    for event in pipelined_presentation_reloads
                    if event.get("post_ack_us") is not None
                ]
            ),
            "latest_ack_phase": (
                presentation_pipeline_reloads[-1].get(
                    "presentation_ack_phase"
                )
                if presentation_pipeline_reloads
                else None
            ),
        },
        "command_stream_loading": {
            "instrumented_reload_count": len(command_loading_reloads),
            "transport_counts": dict(sorted(command_transport_counts.items())),
            "file_reuse_instrumented_reload_count": len(
                command_file_reuse_reloads
            ),
            "file_reused_reload_count": len(reused_command_file_reloads),
            "file_reopened_reload_count": len(reopened_command_file_reloads),
            "file_reuse_ratio": (
                round(
                    len(reused_command_file_reloads)
                    / len(command_file_reuse_reloads),
                    6,
                )
                if command_file_reuse_reloads
                else None
            ),
            "read_bytes": command_read_bytes,
            "file_open_us": _duration_summary(
                [
                    _as_int(event.get("command_file_open_us"))
                    for event in command_loading_reloads
                    if event.get("command_file_open_us") is not None
                ]
            ),
            "file_read_us": _duration_summary(
                [
                    _as_int(event.get("command_file_read_us"))
                    for event in command_loading_reloads
                    if event.get("command_file_read_us") is not None
                ]
            ),
            "file_reopen_us": _duration_summary(
                [
                    _as_int(event.get("command_file_open_us"))
                    for event in reopened_command_file_reloads
                    if event.get("command_file_open_us") is not None
                ]
            ),
            "record_validation_us": _duration_summary(
                [
                    _as_int(event.get("command_record_validation_us"))
                    for event in command_loading_reloads
                    if event.get("command_record_validation_us") is not None
                ]
            ),
            "read_mib_per_second": (
                round(
                    command_read_bytes
                    / (1024 * 1024)
                    / (command_file_read_us / 1_000_000),
                    3,
                )
                if command_file_read_us
                else None
            ),
        },
        "resource_snapshot_payload_reuse": {
            "instrumented_reload_count": len(resource_snapshot_reuse_reloads),
            "reused_reload_count": len(reused_resource_snapshot_reloads),
            "reused_resource_count": sum(
                _as_int(event.get("resource_snapshot_reused_resources"))
                for event in resource_snapshot_reuse_reloads
            ),
            "reused_payload_bytes": sum(
                _as_int(event.get("resource_snapshot_reused_bytes"))
                for event in resource_snapshot_reuse_reloads
            ),
        },
        "texture_binding_lifecycle": {
            "instrumented_reload_count": len(texture_binding_reloads),
            "reused_reload_count": len(reused_texture_binding_reloads),
            "rebuilt_reload_count": (
                len(texture_binding_reloads)
                - len(reused_texture_binding_reloads)
            ),
            "reuse_ratio": (
                round(
                    len(reused_texture_binding_reloads)
                    / len(texture_binding_reloads),
                    6,
                )
                if texture_binding_reloads
                else None
            ),
            "image_descriptor_update_count": sum(
                _as_int(
                    event.get(
                        "texture_binding_image_descriptor_updates"
                    )
                )
                for event in texture_binding_reloads
            ),
            "descriptor_set_allocation_count": sum(
                _as_int(
                    event.get(
                        "texture_binding_descriptor_sets_allocated"
                    )
                )
                for event in texture_binding_reloads
            ),
            "update_us": _duration_summary(
                [
                    _as_int(event.get("texture_binding_update_us"))
                    for event in texture_binding_reloads
                    if event.get("texture_binding_update_us") is not None
                ]
            ),
        },
        "method_interpretation": {
            "instrumented_reload_count": len(method_interpretation_reloads),
            "interpreted_method_count": interpreted_method_count,
            "bulk_indexed_method_count": bulk_indexed_method_count,
            "bulk_inline_method_count": bulk_inline_method_count,
            "scalar_method_count": max(
                0,
                interpreted_method_count - bulk_method_count,
            ),
            "bulk_method_ratio": (
                round(bulk_method_count / interpreted_method_count, 6)
                if interpreted_method_count
                else None
            ),
            "state_seed_status_reload_count": len(state_seed_status_reloads),
            "state_seed_update_reload_count": state_seed_update_reload_count,
            "state_seed_bypass_reload_count": (
                len(state_seed_status_reloads)
                - state_seed_update_reload_count
            ),
            "nanoseconds_per_method": (
                round(
                    sum(
                        _as_int(event.get("method_interpret_us"))
                        for event in method_interpretation_reloads
                    )
                    * 1000
                    / interpreted_method_count,
                    3,
                )
                if interpreted_method_count
                else None
            ),
            "push_buffer_collect_us": _duration_summary(
                [
                    _as_int(event.get("push_buffer_collect_us"))
                    for event in method_interpretation_reloads
                ]
            ),
            "method_apply_us": _duration_summary(
                [
                    _as_int(event.get("method_apply_us"))
                    for event in method_interpretation_reloads
                ]
            ),
            "method_finalize_us": _duration_summary(
                [
                    _as_int(event.get("method_finalize_us"))
                    for event in method_interpretation_reloads
                ]
            ),
        },
        "pipeline_compilation": {
            "graphics_event_count": len(graphics_pipeline_creations),
            "compute_event_count": len(compute_pipeline_creations),
            "graphics_pipeline_count": sum(
                _as_int(event.get("created_count"))
                for event in graphics_pipeline_creations
            ),
            "compute_pipeline_count": len(compute_pipeline_creations),
            "batched_graphics_event_count": sum(
                event.get("batched") is True
                for event in graphics_pipeline_creations
            ),
            "shader_module_us": _duration_summary(
                [
                    _as_int(event.get("shader_module_us"))
                    for event in (
                        graphics_pipeline_creations
                        + compute_pipeline_creations
                    )
                    if event.get("shader_module_us") is not None
                ]
            ),
            "pipeline_create_us": _duration_summary(
                [
                    _as_int(event.get("pipeline_create_us"))
                    for event in (
                        graphics_pipeline_creations
                        + compute_pipeline_creations
                    )
                    if event.get("pipeline_create_us") is not None
                ]
            ),
            "latest_graphics": (
                graphics_pipeline_creations[-1]
                if graphics_pipeline_creations
                else None
            ),
            "latest_compute": (
                compute_pipeline_creations[-1]
                if compute_pipeline_creations
                else None
            ),
        },
        "render_target_feedback_image_cache": {
            "instrumented_reload_count": len(feedback_image_cache_reloads),
            "hit_count": feedback_image_cache_hit_count,
            "miss_count": feedback_image_cache_miss_count,
            "lookup_hit_ratio": (
                round(
                    feedback_image_cache_hit_count
                    / feedback_image_cache_lookup_count,
                    6,
                )
                if feedback_image_cache_lookup_count
                else None
            ),
            "store_count": sum(
                _as_int(event.get("render_target_feedback_image_cache_stores"))
                for event in feedback_image_cache_reloads
            ),
            "eviction_count": sum(
                _as_int(
                    event.get("render_target_feedback_image_cache_evictions")
                )
                for event in feedback_image_cache_reloads
            ),
            "latest_resident_count": _as_int(
                latest_reload.get(
                    "render_target_feedback_image_cache_resident"
                )
            ),
            "capacity": _as_int(
                latest_reload.get(
                    "render_target_feedback_image_cache_capacity"
                )
            ),
        },
        "resource_update_breakdown": {
            "instrumented_reload_count": len(resource_breakdown_reloads),
            "texture_indexed_lookup_count": texture_indexed_lookup_count,
            "texture_indexed_lookup_candidate_count": (
                texture_indexed_lookup_candidate_count
            ),
            "texture_indexed_lookup_candidates_average": (
                round(
                    texture_indexed_lookup_candidate_count
                    / texture_indexed_lookup_count,
                    6,
                )
                if texture_indexed_lookup_count
                else None
            ),
            "texture_constant_lookup_count": sum(
                _as_int(event.get("texture_constant_lookup_count"))
                for event in resource_breakdown_reloads
            ),
            "latest_pipeline_candidate_draws": _as_int(
                latest_reload.get("pipeline_candidate_draws")
            ),
            "latest_pipeline_unique_states": _as_int(
                latest_reload.get("pipeline_unique_states")
            ),
            "latest_pipeline_missing_states": _as_int(
                latest_reload.get("pipeline_missing_states")
            ),
        },
        "presented_diagnostics_sampling": {
            "instrumented_reload_count": len(diagnostic_sampling_reloads),
            "sampled_reload_count": len(sampled_diagnostic_reloads),
            "sample_ratio": (
                round(
                    len(sampled_diagnostic_reloads)
                    / len(diagnostic_sampling_reloads),
                    6,
                )
                if diagnostic_sampling_reloads
                else None
            ),
            "resource_generation_reload_count": len(changed_resource_reloads),
            "resource_generation_sampled_count": sum(
                event.get("presented_diagnostics_sampled") is True
                for event in changed_resource_reloads
            ),
            "sampled_render_validation_us": _duration_summary(
                [
                    _as_int(event.get("render_validation_us"))
                    for event in sampled_diagnostic_reloads
                    if event.get("render_validation_us") is not None
                ]
            ),
            "unsampled_render_validation_us": _duration_summary(
                [
                    _as_int(event.get("render_validation_us"))
                    for event in unsampled_diagnostic_reloads
                    if event.get("render_validation_us") is not None
                ]
            ),
        },
        "offscreen_target_lifecycle": {
            "instrumented_reload_count": len(offscreen_reload_samples),
            "target_reload_count": len(offscreen_target_reloads),
            "reused_reload_count": len(reused_offscreen_target_reloads),
            "recreated_reload_count": len(recreated_offscreen_target_reloads),
            "reuse_ratio": (
                round(
                    len(reused_offscreen_target_reloads)
                    / len(offscreen_target_reloads),
                    6,
                )
                if offscreen_target_reloads
                else None
            ),
            "reused_resource_update_us": _duration_summary(
                [
                    _as_int(event.get("resource_update_us"))
                    for event in reused_offscreen_target_reloads
                    if event.get("resource_update_us") is not None
                ]
            ),
            "recreated_resource_update_us": _duration_summary(
                [
                    _as_int(event.get("resource_update_us"))
                    for event in recreated_offscreen_target_reloads
                    if event.get("resource_update_us") is not None
                ]
            ),
        },
        "vertex_program_execution": {
            "gpu_draws": _as_int(
                latest_reload.get("gpu_vertex_program_draws")
            ),
            "gpu_vertices": gpu_program_vertices,
            "cpu_fallback_draws": _as_int(
                latest_reload.get("cpu_vertex_program_fallback_draws")
            ),
            "cpu_fallback_vertices": cpu_fallback_vertices,
            "gpu_vertex_ratio": (
                round(gpu_program_vertices / programmable_vertices, 6)
                if programmable_vertices
                else None
            ),
        },
        "raw_attribute_fetch": {
            "gpu_draws": _as_int(
                latest_reload.get("gpu_raw_attribute_draws")
            ),
            "gpu_vertices": gpu_raw_attribute_vertices,
            "gpu_program_vertex_ratio": (
                round(gpu_raw_attribute_vertices / gpu_program_vertices, 6)
                if gpu_program_vertices
                else None
            ),
            "expanded_vertex_bytes_avoided": _as_int(
                latest_reload.get("expanded_vertex_bytes_avoided")
            ),
        },
        "persistent_raw_resource_uploads": {
            "refresh_count": len(raw_vertex_uploads),
            "detailed_refresh_count": len(detailed_raw_uploads),
            "legacy_refresh_count": len(raw_vertex_uploads)
            - len(detailed_raw_uploads),
            "resident_resource_bytes": _as_int(
                latest_raw_upload.get("resource_bytes")
            ),
            "layout_rebuild_count": _as_int(
                latest_raw_upload.get("layout_rebuilds")
            ),
            "buffer_replacement_count": sum(
                event.get("buffer_replaced") is True
                for event in detailed_raw_uploads
            ),
            "dirty_upload_count": sum(
                0 < _as_int(event.get("resource_upload_bytes"))
                < _as_int(event.get("resource_bytes"))
                for event in detailed_raw_uploads
            ),
            "no_resource_upload_count": sum(
                _as_int(event.get("resource_upload_bytes")) == 0
                for event in detailed_raw_uploads
            ),
            "compared_bytes": sum(
                _as_int(event.get("compared_bytes"))
                for event in detailed_raw_uploads
            ),
            "cache_refresh_us": _duration_summary(
                [
                    _as_int(event.get("cache_refresh_us"))
                    for event in detailed_raw_uploads
                    if event.get("cache_refresh_us") is not None
                ]
            ),
            "resource_upload_bytes": resource_upload_bytes,
            "index_upload_bytes": index_upload_bytes,
            "resource_upload_bytes_avoided": max(
                0, eligible_resource_bytes - resource_upload_bytes
            ),
            "resource_upload_ratio": (
                round(resource_upload_bytes / eligible_resource_bytes, 6)
                if eligible_resource_bytes
                else None
            ),
            "latest": latest_raw_upload or None,
        },
        "gpu_texture_conversion": {
            "batch_count": len(texture_conversion_batches),
            "refresh_count": len(texture_refreshes),
            "gpu_converted_textures": sum(
                _as_int(event.get("gpu_converted"))
                for event in texture_refreshes
            ),
            "cpu_converted_textures": sum(
                _as_int(event.get("cpu_converted"))
                for event in texture_refreshes
            ),
            "dxt1_textures": sum(
                _as_int(event.get("dxt1_textures"))
                for event in texture_conversion_batches
            ),
            "dxt5_textures": sum(
                _as_int(event.get("dxt5_textures"))
                for event in texture_conversion_batches
            ),
            "recovered_chain_textures": sum(
                _as_int(event.get("recovered_chain_textures"))
                for event in texture_conversion_batches
            ),
            "generated_chain_textures": sum(
                _as_int(event.get("generated_chain_textures"))
                for event in texture_conversion_batches
            ),
            "mip_count": sum(
                _as_int(event.get("mips"))
                for event in texture_conversion_batches
            ),
            "generated_mip_count": sum(
                _as_int(event.get("generated_mips"))
                for event in texture_conversion_batches
            ),
            "dispatch_count": sum(
                _as_int(event.get("dispatches"))
                for event in texture_conversion_batches
            ),
            "compressed_input_bytes": gpu_texture_input_bytes,
            "dxt1_input_bytes": sum(
                _as_int(event.get("dxt1_input_bytes"))
                for event in texture_conversion_batches
            ),
            "dxt5_input_bytes": sum(
                _as_int(event.get("dxt5_input_bytes"))
                for event in texture_conversion_batches
            ),
            "rgba_output_bytes": gpu_texture_output_bytes,
            "output_to_input_ratio": (
                round(gpu_texture_output_bytes / gpu_texture_input_bytes, 6)
                if gpu_texture_input_bytes
                else None
            ),
            "gpu_batch_us": _duration_summary(
                [
                    _as_int(event.get("conversion_us"))
                    for event in texture_conversion_batches
                    if event.get("conversion_us") is not None
                ]
            ),
            "gpu_submission_us": _duration_summary(
                [
                    _as_int(event.get("gpu_submission_us"))
                    for event in texture_conversion_batches
                    if event.get("gpu_submission_us") is not None
                ]
            ),
            "setup_us": _duration_summary(
                [
                    _as_int(event.get("setup_us"))
                    for event in texture_conversion_batches
                    if event.get("setup_us") is not None
                ]
            ),
            "cpu_texture_path_us": _duration_summary(
                [
                    _as_int(event.get("cpu_texture_path_us"))
                    for event in texture_refreshes
                    if event.get("cpu_texture_path_us") is not None
                ]
            ),
            "backend_counts": dict(
                Counter(
                    str(event.get("gpu_conversion_backend", "unknown"))
                    for event in texture_refreshes
                )
            ),
            "validation": {
                "event_count": len(texture_conversion_validations),
                "passed": (
                    all(
                        event.get("passed") is True
                        for event in texture_conversion_validations
                    )
                    if texture_conversion_validations
                    else None
                ),
                "coverage_complete": latest_texture_validation.get(
                    "coverage_complete"
                ),
                "validated_textures": sum(
                    _as_int(event.get("textures"))
                    for event in texture_conversion_validations
                ),
                "validated_mips": sum(
                    _as_int(event.get("mips"))
                    for event in texture_conversion_validations
                ),
                "validated_bytes": sum(
                    _as_int(event.get("bytes"))
                    for event in texture_conversion_validations
                ),
                "mismatch_bytes": sum(
                    _as_int(event.get("mismatch_bytes"))
                    for event in texture_conversion_validations
                ),
                "cpu_us": _duration_summary(
                    [
                        _as_int(event.get("validation_cpu_us"))
                        for event in texture_conversion_validations
                        if event.get("validation_cpu_us") is not None
                    ]
                ),
                "latest": latest_texture_validation or None,
            },
            "latest_batch": (
                texture_conversion_batches[-1]
                if texture_conversion_batches
                else None
            ),
            "latest_refresh": texture_refreshes[-1] if texture_refreshes else None,
        },
        "frame_pacing": {
            "frame_count": len(elapsed_us),
            "elapsed_total_seconds": round(elapsed_total_us / 1_000_000, 6),
            "target_frame_us": target_us[-1] if target_us else 16_667,
            "target_frame_ms": round(
                (target_us[-1] if target_us else 16_667) / 1000.0,
                6,
            ),
            "missed_target_count": missed_target,
            "missed_target_ratio": (
                round(missed_target / len(elapsed_us), 6) if elapsed_us else None
            ),
            "elapsed_us": _duration_summary(elapsed_us),
            "elapsed_ms": _duration_summary(elapsed_us),
            "boundary_attribution": {
                "instrumented_frame_count": len(boundary_frames),
                "slow_frame_threshold": "elapsed_us > 2 * target_frame_us",
                "slow_frame_count": len(slow_boundary_frames),
                "dominant_boundary_counts": dict(dominant_boundary_counts),
                "stages": boundary_stages,
                "slowest_frames": slow_boundary_frames[:20],
            },
        },
    }


def _geometry_signature(event: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(event.get(field) for field in GEOMETRY_COUNT_FIELDS) + tuple(
        event.get(field)
        for field in ("overall_min_x", "overall_max_x", "overall_min_y", "overall_max_y")
    )


def _summarize_host_geometry(events: list[dict[str, Any]]) -> dict[str, Any]:
    anomalous = [event for event in events if bool(event.get("anomalous"))]
    peaks = {
        field: max((int(event.get(field, 0)) for event in events), default=0)
        for field in GEOMETRY_COUNT_FIELDS
    }
    signatures: dict[tuple[Any, ...], dict[str, Any]] = {}
    for event in events:
        signature = _geometry_signature(event)
        retained = signatures.get(signature)
        if retained is None:
            signatures[signature] = {
                "occurrence_count": 1,
                "first_reload": event.get("reload"),
                "last_reload": event.get("reload"),
                "first_guest_flips": event.get("guest_flips"),
                "last_guest_flips": event.get("guest_flips"),
                **{field: event.get(field) for field in GEOMETRY_COUNT_FIELDS},
                "overall_bounds": {
                    "min_x": event.get("overall_min_x"),
                    "max_x": event.get("overall_max_x"),
                    "min_y": event.get("overall_min_y"),
                    "max_y": event.get("overall_max_y"),
                },
            }
        else:
            retained["occurrence_count"] += 1
            retained["last_reload"] = event.get("reload")
            retained["last_guest_flips"] = event.get("guest_flips")
    return {
        "geometry_event_count": len(events),
        "anomalous_geometry_event_count": len(anomalous),
        "peak_counts": peaks,
        "first_anomalous_event": anomalous[0] if anomalous else None,
        "last_anomalous_event": anomalous[-1] if anomalous else None,
        "latest_event": events[-1] if events else None,
        "geometry_signatures": sorted(
            signatures.values(),
            key=lambda item: (-int(item["occurrence_count"]), int(item["first_reload"] or 0)),
        ),
    }


def _summarize_vertex_transforms(
    events: list[dict[str, Any]],
    draw_events: list[dict[str, Any]],
    constant_events: list[dict[str, Any]],
) -> dict[str, Any]:
    latest = events[-1] if events else None
    collapsed = [
        event
        for event in events
        if bool(
            event.get(
                "indexed_program_tiny_coverage_suspected",
                event.get("coordinate_collapse_suspected"),
            )
        )
    ]
    latest_draw_events_reversed: list[dict[str, Any]] = []
    latest_presented_indices: set[int] = set()
    for event in reversed(draw_events):
        presented_index = _as_int(event.get("presented_index"), -1)
        if presented_index < 0:
            continue
        if presented_index in latest_presented_indices:
            break
        latest_presented_indices.add(presented_index)
        latest_draw_events_reversed.append(event)
    latest_draw_events = list(reversed(latest_draw_events_reversed))
    constants_by_draw: dict[int, dict[int, list[float]]] = {}
    for event in constant_events:
        presented_index = _as_int(event.get("presented_index"), -1)
        constant_index = _as_int(event.get("constant_index"), -1)
        if presented_index < 0 or constant_index < 0:
            continue
        try:
            value = [float(event.get(lane)) for lane in ("x", "y", "z", "w")]
        except (TypeError, ValueError):
            continue
        constants_by_draw.setdefault(presented_index, {})[constant_index] = value
    tiny_draws: list[dict[str, Any]] = []
    near_zero_position_basis_draw_count = 0
    for draw in latest_draw_events:
        if not bool(draw.get("indexed_array")):
            continue
        if _as_int(draw.get("transform_execution_mode")) & 3 != 2:
            continue
        try:
            span_x = float(draw["output_pixel_max_x"]) - float(
                draw["output_pixel_min_x"]
            )
            span_y = float(draw["output_pixel_max_y"]) - float(
                draw["output_pixel_min_y"]
            )
        except (KeyError, TypeError, ValueError):
            continue
        if span_x >= 4.0 or span_y >= 4.0:
            continue
        presented_index = _as_int(draw.get("presented_index"), -1)
        constants = constants_by_draw.get(presented_index, {})
        position_rows = [constants[index] for index in range(96, 100) if index in constants]
        max_abs_basis = (
            max(abs(component) for row in position_rows for component in row[:3])
            if len(position_rows) == 4
            else None
        )
        near_zero_basis = max_abs_basis is not None and max_abs_basis < 0.01
        near_zero_position_basis_draw_count += 1 if near_zero_basis else 0
        tiny_draws.append(
            {
                "presented_index": presented_index,
                "vertex_count": _as_int(draw.get("vertex_count")),
                "input_bounds": {
                    "min_x": draw.get("input_min_x"),
                    "max_x": draw.get("input_max_x"),
                    "min_y": draw.get("input_min_y"),
                    "max_y": draw.get("input_max_y"),
                    "min_z": draw.get("input_min_z"),
                    "max_z": draw.get("input_max_z"),
                },
                "output_span_x": round(span_x, 6),
                "output_span_y": round(span_y, 6),
                "vertex_format_0": draw.get("vertex_format_0"),
                "position_c96_c99_max_abs_basis": (
                    round(max_abs_basis, 9) if max_abs_basis is not None else None
                ),
                "position_c96_c99_near_zero_basis": near_zero_basis,
                "position_c96_c99_rows": position_rows or None,
            }
        )
    tiny_draws.sort(key=lambda draw: (-draw["vertex_count"], draw["presented_index"]))
    defaulted_position_w_draws = [
        draw
        for draw in latest_draw_events
        if _as_int(draw.get("defaulted_position_w_vertex_count")) > 0
    ]
    return {
        "status": (
            "indexed_program_tiny_coverage"
            if collapsed
            else "healthy"
            if events
            else "not_captured"
        ),
        "event_count": len(events),
        "tiny_coverage_event_count": len(collapsed),
        "peak_invalid_program_vertex_count": max(
            (_as_int(event.get("invalid_program_vertex_count")) for event in events),
            default=0,
        ),
        "peak_subpixel_indexed_program_draw_count": max(
            (
                _as_int(event.get("subpixel_indexed_program_draw_count"))
                for event in events
            ),
            default=0,
        ),
        "peak_tiny_indexed_program_draw_count": max(
            (
                _as_int(event.get("tiny_indexed_program_draw_count"))
                for event in events
            ),
            default=0,
        ),
        "draw_event_count": len(draw_events),
        "latest_draw_event_count": len(latest_draw_events),
        "defaulted_position_w_draw_count": len(defaulted_position_w_draws),
        "defaulted_position_w_vertex_count": sum(
            _as_int(draw.get("defaulted_position_w_vertex_count"))
            for draw in defaulted_position_w_draws
        ),
        "invalid_position_w_vertex_count": sum(
            _as_int(draw.get("invalid_position_w_vertex_count"))
            for draw in defaulted_position_w_draws
        ),
        "non_finite_position_w_vertex_count": sum(
            _as_int(draw.get("non_finite_position_w_vertex_count"))
            for draw in defaulted_position_w_draws
        ),
        "implicit_position_w_draw_count": sum(
            1
            for draw in defaulted_position_w_draws
            if _as_int(draw.get("implicit_position_w_vertex_count")) > 0
        ),
        "implicit_position_w_vertex_count": sum(
            _as_int(draw.get("implicit_position_w_vertex_count"))
            for draw in defaulted_position_w_draws
        ),
        "defaulted_position_w_draws": defaulted_position_w_draws[:32],
        "tiny_indexed_program_draws": tiny_draws[:32],
        "near_zero_position_c96_c99_basis_draw_count": (
            near_zero_position_basis_draw_count
        ),
        "latest_event": latest,
        "latest_tiny_coverage_event": collapsed[-1] if collapsed else None,
    }


def _summarize_render_state_diagnostics(
    events: list[dict[str, Any]],
    draw_events: list[dict[str, Any]],
    fixed_vertex_events: list[dict[str, Any]],
) -> dict[str, Any]:
    latest = events[-1] if events else None
    if latest is None:
        return {
            "status": "not_captured",
            "event_count": 0,
            "latest_event": None,
            "mismatch_draw_count": 0,
            "mismatching_draws": [],
        }
    latest_draws_reversed: list[dict[str, Any]] = []
    presented_indices: set[int] = set()
    for event in reversed(draw_events):
        presented_index = _as_int(event.get("presented_index"), -1)
        if presented_index < 0:
            continue
        if presented_index in presented_indices:
            break
        presented_indices.add(presented_index)
        latest_draws_reversed.append(event)
    mismatching_draws = [
        event
        for event in reversed(latest_draws_reversed)
        if event.get("sampler_address_match") is False
        or (
            _as_int(event.get("alpha_test_enable")) != 0
            and event.get("host_alpha_test_applied") is False
        )
        or (
            _as_int(event.get("combiner_stage_count")) != 0
            and event.get("host_register_combiner_applied") is False
        )
        or (
            _as_int(event.get("fog_enable")) != 0
            and event.get("host_fog_factor_applied") is False
        )
        or event.get("host_texture_stage_coverage_complete") is False
        or str(event.get("host_transform_path")) in {"raw_fallback", "filtered"}
    ]
    latest_draws = list(reversed(latest_draws_reversed))
    zero_payload_draws = [
        event
        for event in latest_draws
        if event.get("texture_resource_payload_all_zero") is True
        and event.get("host_render_target_feedback_supported") is not True
    ]
    missing_texture_resource_draws = [
        event
        for event in latest_draws
        if event.get("texture_enabled") is True
        and event.get("texture_resource_matched") is False
        and event.get("host_render_target_feedback_supported") is not True
    ]
    aliased_gpu_produced_draws = [
        event
        for event in zero_payload_draws
        if event.get("texture_address_alias_applied") is True
    ]
    offscreen_replay_draws = [
        event
        for event in aliased_gpu_produced_draws
        if event.get("host_offscreen_target_replay_supported") is True
    ]
    multiple_texture_draws = [
        event
        for event in latest_draws
        if _as_int(event.get("enabled_texture_stage_count")) > 1
    ]
    fog_missing_draws = [
        event
        for event in latest_draws
        if _as_int(event.get("fog_enable")) != 0
        and event.get("host_fog_factor_applied") is False
    ]
    latest_fixed_vertices_reversed: list[dict[str, Any]] = []
    fixed_vertex_keys: set[tuple[int, int]] = set()
    for event in reversed(fixed_vertex_events):
        key = (
            _as_int(event.get("presented_index"), -1),
            _as_int(event.get("vertex_index"), -1),
        )
        if key[0] < 0 or key[1] < 0:
            continue
        if key in fixed_vertex_keys:
            break
        fixed_vertex_keys.add(key)
        latest_fixed_vertices_reversed.append(event)
    mismatch_draw_count = sum(
        _as_int(latest.get(field))
        for field in (
            "sampler_address_mismatch_draw_count",
            "alpha_test_state_mismatch_draw_count",
            "register_combiner_state_mismatch_draw_count",
            "fog_state_mismatch_draw_count",
            "texture_stage_coverage_mismatch_draw_count",
            "fixed_function_raw_fallback_draw_count",
            "fixed_function_filtered_draw_count",
        )
    )
    state_complete = latest.get("analysis_state_complete") is not False
    return {
        "status": (
            "translation_mismatch"
            if mismatch_draw_count or missing_texture_resource_draws
            else "healthy"
            if state_complete
            else "healthy_partial_history"
        ),
        "event_count": len(events),
        "state_history_complete": state_complete,
        "continuation_bootstrap_used": bool(
            latest.get("continuation_bootstrap_used")
        ),
        "mismatch_draw_count": mismatch_draw_count,
        "mismatching_draws": mismatching_draws[:8],
        "missing_texture_resource_draw_count": len(
            missing_texture_resource_draws
        ),
        "missing_texture_resource_draws": missing_texture_resource_draws[:16],
        "zero_payload_draws": zero_payload_draws[:16],
        "aliased_gpu_produced_draws": aliased_gpu_produced_draws[:16],
        "offscreen_replay_draws": offscreen_replay_draws[:16],
        "multiple_texture_draws": multiple_texture_draws[:16],
        "fog_missing_draws": fog_missing_draws[:16],
        "fixed_function_vertices": list(
            reversed(latest_fixed_vertices_reversed)
        )[:128],
        "latest_event": latest,
    }


def _event_guest_flip(event: dict[str, Any]) -> int:
    manifest_flip = _as_int(event.get("manifest_guest_flip_count"))
    return manifest_flip if manifest_flip > 0 else _as_int(event.get("guest_flips"))


def _bounds_cover_viewport(
    item: dict[str, Any] | None,
    *,
    width: int,
    height: int,
) -> bool:
    if not item:
        return False
    try:
        return (
            float(item.get("min_x", item.get("overall_min_x"))) <= 0.5
            and float(item.get("min_y", item.get("overall_min_y"))) <= 0.5
            and float(item.get("max_x", item.get("overall_max_x"))) >= width - 0.5
            and float(item.get("max_y", item.get("overall_max_y"))) >= height - 0.5
        )
    except (TypeError, ValueError):
        return False


def _summarize_composition_coverage(
    *,
    submitted_geometry_by_flip: list[dict[str, Any]],
    submitted_draw: dict[str, Any],
    clear_audit: dict[str, Any],
    builder_geometry_by_flip: list[dict[str, Any]],
    text_draw: dict[str, Any],
    retained_events: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    latest = {
        name: events[-1] if events else None
        for name, events in retained_events.items()
    }
    host_geometry = latest["nv2a_presented_geometry_anomalies"]
    host_flip = _event_guest_flip(host_geometry) if host_geometry else 0
    guest_geometry = next(
        (
            item
            for item in reversed(submitted_geometry_by_flip)
            if _as_int(item.get("guest_flip_count")) + 1 == host_flip
        ),
        None,
    )
    guest_geometry_association = "producer_flip_precedes_presented_flip"
    if guest_geometry is None:
        guest_geometry = next(
            (
                item
                for item in reversed(submitted_geometry_by_flip)
                if _as_int(item.get("guest_flip_count")) == host_flip
            ),
            None,
        )
        guest_geometry_association = "same_flip_legacy_fallback"
    if guest_geometry is None and submitted_geometry_by_flip:
        guest_geometry = submitted_geometry_by_flip[-1]
        guest_geometry_association = "latest_unmatched_fallback"
    if guest_geometry is None:
        guest_geometry_association = "no_submitted_geometry"
    latest_readback = latest["frame_readback_captured"]
    readback = next(
        (
            event
            for event in reversed(retained_events["frame_readback_captured"])
            if host_flip > 0 and _event_guest_flip(event) == host_flip
        ),
        None,
    )
    width_source = readback or latest_readback
    width = _as_int(width_source.get("width"), 640) if width_source else 640
    height = _as_int(width_source.get("height"), 480) if width_source else 480
    host_reload = _as_int(host_geometry.get("reload")) if host_geometry else 0
    live_reload = next(
        (
            event
            for event in reversed(retained_events["live_render_stream_reloaded"])
            if (
                host_reload > 0
                and _as_int(event.get("reload")) == host_reload
            )
            or (
                host_reload == 0
                and host_flip > 0
                and _event_guest_flip(event) == host_flip
            )
        ),
        None,
    )
    guest_fullscreen = _bounds_cover_viewport(
        guest_geometry, width=width, height=height
    )
    host_fullscreen = (
        _as_int(host_geometry.get("fullscreen_draw_count")) > 0
        if host_geometry and host_geometry.get("fullscreen_draw_count") is not None
        else _bounds_cover_viewport(host_geometry, width=width, height=height)
    )
    dominant_ratio = None
    if readback and _as_int(readback.get("pixel_count")):
        dominant_ratio = round(
            _as_int(readback.get("dominant_count"))
            / _as_int(readback.get("pixel_count")),
            6,
        )
    text_samples = text_draw.get("sampled_strings", [])
    interpreter = latest["d3d8_stream_interpreted"]
    if interpreter is not None:
        interpreter = dict(interpreter)
        legacy_indexed_noops = interpreter.pop(
            "zero_count_indexed_array_packets", None
        )
        if (
            "zero_count_indexed_array_noop_packets" not in interpreter
            and legacy_indexed_noops is not None
        ):
            interpreter["zero_count_indexed_array_noop_packets"] = (
                legacy_indexed_noops
            )
    issues: list[dict[str, Any]] = []
    submission_audit_available = bool(submitted_draw.get("draw_address_hex"))
    if not submission_audit_available:
        issues.append(
            {
                "id": "guest_draw_submission_audit_missing",
                "scope": "diagnostic_coverage",
                "evidence": {
                    "cpu_builder_geometry_observed": bool(builder_geometry_by_flip),
                    "host_geometry_observed": host_geometry is not None,
                },
            }
        )
    if guest_fullscreen and not host_fullscreen:
        issues.append(
            {
                "id": "guest_submitted_fullscreen_geometry_missing_on_host",
                "scope": "guest_to_host_composition",
                "evidence": {
                    "guest_flip_count": (
                        guest_geometry.get("guest_flip_count")
                        if guest_geometry
                        else None
                    ),
                    "guest_bounds": guest_geometry,
                    "host_reload": host_geometry.get("reload") if host_geometry else None,
                    "host_bounds": host_geometry,
                    "guest_geometry_association": guest_geometry_association,
                },
            }
        )
    if (
        host_geometry
        and live_reload
        and live_reload.get("exact_completed_flip") is True
        and _as_int(host_geometry.get("presented_draw_count")) > 0
        and not host_fullscreen
        and interpreter
        and interpreter.get("clear_color_valid") is True
        and _as_int(interpreter.get("clear_color_argb")) == 0
    ):
        issues.append(
            {
                "id": "black_clear_without_fullscreen_background_draw",
                "scope": "guest_render_submission",
                "evidence": {
                    "manifest_guest_flip_count": _event_guest_flip(live_reload),
                    "exact_completed_flip": True,
                    "clear_color_argb": interpreter.get("clear_color_argb"),
                    "presented_draw_count": host_geometry.get(
                        "presented_draw_count"
                    ),
                    "presented_vertex_count": host_geometry.get(
                        "presented_vertex_count"
                    ),
                    "fullscreen_draw_count": host_geometry.get(
                        "fullscreen_draw_count"
                    ),
                    "presented_texture_addresses": host_geometry.get(
                        "presented_texture_addresses", []
                    ),
                    "host_bounds": host_geometry,
                },
            }
        )
    if (
        readback
        and dominant_ratio is not None
        and dominant_ratio >= 0.90
        and (_as_int(readback.get("dominant_rgba")) == 0 or readback.get("near_solid"))
    ):
        issues.append(
            {
                "id": "dominant_black_or_near_solid_readback",
                "scope": "presented_frame",
                "evidence": {
                    "output": readback.get("output"),
                    "dominant_rgba": readback.get("dominant_rgba"),
                    "dominant_pixel_ratio": dominant_ratio,
                    "unique_colors": readback.get("unique_colors"),
                },
            }
        )
    if host_geometry and host_geometry.get("manifest_guest_flip_count") is None:
        issues.append(
            {
                "id": "manifest_flip_identity_missing",
                "scope": "diagnostic_correlation",
                "evidence": {"host_reload": host_geometry.get("reload")},
            }
        )
    if host_geometry and latest_readback is not None and readback is None:
        issues.append(
            {
                "id": "current_presented_flip_readback_missing",
                "scope": "diagnostic_correlation",
                "evidence": {
                    "presented_guest_flip_count": host_flip,
                    "latest_readback_guest_flip_count": _event_guest_flip(
                        latest_readback
                    ),
                    "latest_readback_output": latest_readback.get("output"),
                },
            }
        )
    if text_samples and all(sample.get("guest_flip_count") is None for sample in text_samples):
        issues.append(
            {
                "id": "text_provenance_not_flip_correlated",
                "scope": "diagnostic_correlation",
                "evidence": {
                    "sample_count": len(text_samples),
                    "invocation_count": text_draw.get("invocation_count", 0),
                },
            }
        )
    if (
        _as_int(clear_audit.get("invocation_count")) > 0
        and clear_audit.get("execution_mode") != "recovered_guest_code"
    ):
        issues.append(
            {
                "id": "guest_clear_replaced_by_return_only_fast_path",
                "scope": "guest_recomp_accuracy",
                "evidence": {
                    "invocation_count": clear_audit.get("invocation_count"),
                    "execution_mode": clear_audit.get(
                        "execution_mode", "legacy_return_only_fast_path"
                    ),
                    "clear_address_hex": clear_audit.get("draw_address_hex"),
                },
            }
        )
    validation = latest["render_validation"]
    if validation and (
        _as_int(validation.get("unmatched_presented_texture_draw_count"))
        or _as_int(validation.get("unsupported_presented_primitive_count"))
        or _as_int(
            validation.get("missing_presented_indexed_resource_draw_count")
        )
        or _as_int(validation.get("unsupported_draw_arrays_count"))
    ):
        issues.append(
            {
                "id": "host_render_validation_failure",
                "scope": "host_translation",
                "evidence": validation,
            }
        )
    if live_reload is not None:
        interpreted_count = _as_int(
            live_reload.get("interpreted_source_commands")
        )
        presentable_count = _as_int(
            live_reload.get(
                "presentable_command_record_count",
                live_reload.get("writes"),
            )
        )
        decoded_flip_count = _as_int(live_reload.get("guest_flips"))
        manifest_flip_count = _event_guest_flip(live_reload)
        exact_completed_flip = live_reload.get("exact_completed_flip")
        replay_inexact = (
            exact_completed_flip is False
            or (presentable_count > 0 and interpreted_count != presentable_count)
            or (
                manifest_flip_count > 0
                and decoded_flip_count != manifest_flip_count
            )
        )
        if replay_inexact:
            issues.append(
                {
                    "id": "host_completed_flip_replay_inexact",
                    "scope": "guest_to_host_composition",
                    "evidence": {
                        "reload": live_reload.get("reload"),
                        "manifest_guest_flip_count": manifest_flip_count,
                        "decoded_guest_flip_count": decoded_flip_count,
                        "presentable_command_record_count": presentable_count,
                        "interpreted_source_commands": interpreted_count,
                        "sidecar_write_count": live_reload.get("writes"),
                        "exact_completed_flip": exact_completed_flip,
                    },
                }
            )
    return {
        "status": "issues_detected" if issues else "no_issue_detected",
        "viewport": {"width": width, "height": height},
        "guest_draw_submission_audit_available": submission_audit_available,
        "guest_fullscreen_geometry_observed": guest_fullscreen,
        "host_fullscreen_geometry_observed": host_fullscreen,
        "dominant_readback_pixel_ratio": dominant_ratio,
        "issues": issues,
        "latest_guest_geometry": guest_geometry,
        "latest_guest_builder_geometry": (
            builder_geometry_by_flip[-1] if builder_geometry_by_flip else None
        ),
        "guest_geometry_association": guest_geometry_association,
        "latest_host_geometry": host_geometry,
        "latest_live_reload": live_reload,
        "latest_native_resources": latest["nv2a_native_resources_created"],
        "latest_render_validation": validation,
        "latest_interpreter": interpreter,
        "correlated_readback": readback,
        "latest_readback": latest_readback,
    }


def _summarize_world_matrix_audit(execution: dict[str, Any]) -> dict[str, Any]:
    contexts: list[dict[str, Any]] = []
    entry_audit = execution.get("title_world_matrix_audit")
    if isinstance(entry_audit, dict):
        contexts.append(
            {
                "scope": "entry_execution",
                "thread_index": None,
                "audit": entry_audit,
            }
        )
    for thread in execution.get("guest_thread_executions", []):
        audit = thread.get("title_world_matrix_audit")
        if not isinstance(audit, dict):
            continue
        contexts.append(
            {
                "scope": "guest_thread_execution",
                "thread_index": thread.get("thread_index"),
                "audit": audit,
            }
        )
    source_counts: Counter[tuple[str, str, str]] = Counter()
    source_singular_counts: Counter[tuple[str, str, str]] = Counter()
    caller_counts: Counter[str] = Counter()
    singular_samples: list[dict[str, Any]] = []
    write_instruction_counts: Counter[str] = Counter()
    write_addresses: set[str] = set()
    write_samples: list[dict[str, Any]] = []
    transition_samples: list[dict[str, Any]] = []
    write_count = 0
    singularizing_write_count = 0
    restoring_write_count = 0
    rotation_enabled = False
    rotation_matrix_addresses: set[str] = set()
    rotation_entry_addresses: set[str] = set()
    rotation_builder_addresses: set[str] = set()
    rotation_return_addresses: set[str] = set()
    rotation_entry_count = 0
    rotation_completion_count = 0
    rotation_pending_count = 0
    rotation_unmatched_completion_count = 0
    rotation_singular_input_count = 0
    rotation_singular_output_count = 0
    rotation_nonfinite_output_count = 0
    rotation_return_counts: Counter[str] = Counter()
    rotation_stage_counts: Counter[tuple[str, bool, bool]] = Counter()
    rotation_samples: list[dict[str, Any]] = []
    world_draw_callback_count = 0
    world_draw_callback_zero_index_count = 0
    indexed_draw_count = 0
    zero_index_draw_count = 0
    indexed_draw_caller_counts: Counter[str] = Counter()
    zero_index_draw_caller_counts: Counter[str] = Counter()
    world_draw_callback_caller_counts: Counter[str] = Counter()
    world_draw_callback_samples: list[dict[str, Any]] = []
    indexed_draw_samples: list[dict[str, Any]] = []
    first_healthy_sample = None
    latest_healthy_sample = None
    for context in contexts:
        audit = context["audit"]
        if first_healthy_sample is None and audit.get("first_healthy_sample"):
            first_healthy_sample = audit["first_healthy_sample"]
        if audit.get("latest_healthy_sample"):
            latest_healthy_sample = audit["latest_healthy_sample"]
        singular_samples.extend(audit.get("singular_samples", []))
        indexed_draw_audit = audit.get("indexed_draw_audit")
        if isinstance(indexed_draw_audit, dict):
            world_draw_callback_count += _as_int(
                indexed_draw_audit.get("world_draw_callback_count")
            )
            world_draw_callback_zero_index_count += _as_int(
                indexed_draw_audit.get("world_draw_callback_zero_index_count")
            )
            indexed_draw_count += _as_int(
                indexed_draw_audit.get("indexed_draw_count")
            )
            zero_index_draw_count += _as_int(
                indexed_draw_audit.get("zero_index_draw_count")
            )
            for record in indexed_draw_audit.get("indexed_draw_caller_counts", []):
                address = str(record.get("caller_return_address_hex"))
                indexed_draw_caller_counts[address] += _as_int(
                    record.get("invocation_count")
                )
                zero_index_draw_caller_counts[address] += _as_int(
                    record.get("zero_index_count")
                )
            for record in indexed_draw_audit.get(
                "world_draw_callback_caller_counts", []
            ):
                world_draw_callback_caller_counts[
                    str(record.get("caller_return_address_hex"))
                ] += _as_int(record.get("invocation_count"))
            world_draw_callback_samples.extend(
                indexed_draw_audit.get("world_draw_callback_samples", [])
            )
            indexed_draw_samples.extend(
                indexed_draw_audit.get("indexed_draw_samples", [])
            )
        write_audit = audit.get("write_audit")
        if isinstance(write_audit, dict):
            address = write_audit.get("address_hex")
            if address:
                write_addresses.add(str(address))
            write_count += _as_int(write_audit.get("write_count"))
            singularizing_write_count += _as_int(
                write_audit.get("singularizing_write_count")
            )
            restoring_write_count += _as_int(
                write_audit.get("restoring_write_count")
            )
            write_samples.extend(write_audit.get("write_samples", []))
            transition_samples.extend(write_audit.get("transition_samples", []))
            for record in write_audit.get("instruction_counts", []):
                write_instruction_counts[
                    str(record.get("instruction_address_hex"))
                ] += _as_int(record.get("write_count"))
        rotation_audit = audit.get("rotation_audit")
        if isinstance(rotation_audit, dict):
            rotation_enabled = rotation_enabled or bool(
                rotation_audit.get("enabled")
            )
            matrix_address = rotation_audit.get("matrix_address_hex")
            if matrix_address:
                rotation_matrix_addresses.add(str(matrix_address))
            entry_address = rotation_audit.get("entry_address_hex")
            if entry_address:
                rotation_entry_addresses.add(str(entry_address))
            builder_address = rotation_audit.get("builder_address_hex")
            if builder_address:
                rotation_builder_addresses.add(str(builder_address))
            rotation_return_addresses.update(
                str(address)
                for address in rotation_audit.get("return_addresses_hex", [])
            )
            rotation_entry_count += _as_int(rotation_audit.get("entry_count"))
            rotation_completion_count += _as_int(
                rotation_audit.get("completion_count")
            )
            rotation_pending_count += _as_int(rotation_audit.get("pending_count"))
            rotation_unmatched_completion_count += _as_int(
                rotation_audit.get("unmatched_completion_count")
            )
            rotation_singular_input_count += _as_int(
                rotation_audit.get("singular_input_count")
            )
            rotation_singular_output_count += _as_int(
                rotation_audit.get("singular_output_count")
            )
            rotation_nonfinite_output_count += _as_int(
                rotation_audit.get("nonfinite_output_count")
            )
            for record in rotation_audit.get("return_counts", []):
                rotation_return_counts[
                    str(record.get("return_address_hex"))
                ] += _as_int(record.get("entry_count"))
            for record in rotation_audit.get("stage_records", []):
                key = (
                    str(record.get("return_address_hex")),
                    bool(record.get("input_singular")),
                    bool(record.get("output_singular")),
                )
                rotation_stage_counts[key] += _as_int(
                    record.get("completion_count")
                )
            rotation_samples.extend(rotation_audit.get("samples", []))
        for caller in audit.get("caller_counts", []):
            caller_counts[str(caller.get("caller_return_address_hex"))] += _as_int(
                caller.get("invocation_count")
            )
        for source in audit.get("source_records", []):
            key = (
                str(source.get("caller_return_address_hex")),
                str(source.get("source_address_hex")),
                str(source.get("matrix_owner_candidate_hex")),
            )
            source_counts[key] += _as_int(source.get("invocation_count"))
            source_singular_counts[key] += _as_int(
                source.get("singular_matrix_count")
            )
    return {
        "enabled": any(bool(context["audit"].get("enabled")) for context in contexts),
        "context_count": len(contexts),
        "invocation_count": sum(
            _as_int(context["audit"].get("invocation_count"))
            for context in contexts
        ),
        "singular_matrix_count": sum(
            _as_int(context["audit"].get("singular_matrix_count"))
            for context in contexts
        ),
        "nonfinite_matrix_count": sum(
            _as_int(context["audit"].get("nonfinite_matrix_count"))
            for context in contexts
        ),
        "caller_counts": [
            {
                "caller_return_address_hex": address,
                "invocation_count": count,
            }
            for address, count in caller_counts.most_common()
        ],
        "source_records": [
            {
                "caller_return_address_hex": key[0],
                "source_address_hex": key[1],
                "matrix_owner_candidate_hex": key[2],
                "invocation_count": count,
                "singular_matrix_count": source_singular_counts[key],
            }
            for key, count in source_counts.most_common()
        ],
        "first_healthy_sample": first_healthy_sample,
        "latest_healthy_sample": latest_healthy_sample,
        "singular_samples": singular_samples[-64:],
        "indexed_draw_audit": {
            "world_draw_callback_count": world_draw_callback_count,
            "world_draw_callback_zero_index_count": (
                world_draw_callback_zero_index_count
            ),
            "indexed_draw_count": indexed_draw_count,
            "zero_index_draw_count": zero_index_draw_count,
            "world_draw_callback_caller_counts": [
                {
                    "caller_return_address_hex": address,
                    "invocation_count": count,
                }
                for address, count in world_draw_callback_caller_counts.most_common()
            ],
            "indexed_draw_caller_counts": [
                {
                    "caller_return_address_hex": address,
                    "invocation_count": count,
                    "zero_index_count": zero_index_draw_caller_counts[address],
                }
                for address, count in indexed_draw_caller_counts.most_common()
            ],
            "world_draw_callback_samples": world_draw_callback_samples[-64:],
            "indexed_draw_samples": indexed_draw_samples[-64:],
        },
        "write_audit": {
            "enabled": bool(write_addresses),
            "addresses_hex": sorted(write_addresses),
            "write_count": write_count,
            "singularizing_write_count": singularizing_write_count,
            "restoring_write_count": restoring_write_count,
            "instruction_counts": [
                {
                    "instruction_address_hex": address,
                    "write_count": count,
                }
                for address, count in write_instruction_counts.most_common()
            ],
            "write_samples": write_samples[-64:],
            "transition_samples": transition_samples[-64:],
        },
        "rotation_audit": {
            "enabled": rotation_enabled,
            "matrix_addresses_hex": sorted(rotation_matrix_addresses),
            "entry_addresses_hex": sorted(rotation_entry_addresses),
            "builder_addresses_hex": sorted(rotation_builder_addresses),
            "return_addresses_hex": sorted(rotation_return_addresses),
            "entry_count": rotation_entry_count,
            "completion_count": rotation_completion_count,
            "pending_count": rotation_pending_count,
            "unmatched_completion_count": rotation_unmatched_completion_count,
            "singular_input_count": rotation_singular_input_count,
            "singular_output_count": rotation_singular_output_count,
            "nonfinite_output_count": rotation_nonfinite_output_count,
            "return_counts": [
                {
                    "return_address_hex": address,
                    "entry_count": count,
                }
                for address, count in rotation_return_counts.most_common()
            ],
            "stage_records": [
                {
                    "return_address_hex": key[0],
                    "input_singular": key[1],
                    "output_singular": key[2],
                    "completion_count": count,
                }
                for key, count in rotation_stage_counts.most_common()
            ],
            "samples": rotation_samples[-64:],
        },
        "contexts": [
            {
                "scope": context["scope"],
                "thread_index": context["thread_index"],
                "enabled": context["audit"].get("enabled"),
                "invocation_count": context["audit"].get("invocation_count"),
                "singular_matrix_count": context["audit"].get(
                    "singular_matrix_count"
                ),
            }
            for context in contexts
        ],
    }


def _summarize_guest_performance(execution: dict[str, Any]) -> dict[str, Any]:
    contexts = [
        {
            "scope": "entry_execution",
            "thread_index": None,
            "status": execution.get("status"),
            "execution": execution,
        }
    ]
    contexts.extend(
        {
            "scope": "guest_thread_execution",
            "thread_index": thread.get("thread_index"),
            "status": thread.get("status"),
            "execution": thread,
        }
        for thread in execution.get("guest_thread_executions", [])
    )
    native_runs: list[dict[str, Any]] = []
    live_bridges: list[dict[str, Any]] = []
    latest_live_bridge_summary: dict[str, Any] | None = None
    for context in contexts:
        context_execution = context["execution"]
        context_runs = context_execution.get("native_runs", [])
        if not context_runs and context_execution.get("native_run"):
            context_runs = [context_execution["native_run"]]
        native_runs.extend(
            {
                "scope": context["scope"],
                "thread_index": context["thread_index"],
                "status": context["status"],
                "reason": run.get("reason"),
                "steps": run.get("steps"),
                "performance": run.get("performance"),
            }
            for run in context_runs
        )
        bridge = context_execution.get("live_host_bridge")
        if bridge and bridge.get("performance"):
            latest_live_bridge_summary = bridge
            live_bridges.append(
                {
                    "scope": context["scope"],
                    "thread_index": context["thread_index"],
                    "performance": bridge["performance"],
                }
            )
    aggregate_handler_timings: dict[tuple[str, str], dict[str, Any]] = {}
    for run in native_runs:
        for handler in (run.get("performance") or {}).get(
            "handler_hot_paths", []
        ):
            key = (str(handler.get("target_hex")), str(handler.get("name")))
            aggregate = aggregate_handler_timings.setdefault(
                key,
                {
                    "target_hex": handler.get("target_hex"),
                    "name": handler.get("name"),
                    "count": 0,
                    "total_us": 0,
                    "max_us": 0,
                },
            )
            aggregate["count"] += _as_int(handler.get("count"))
            aggregate["total_us"] += _as_int(handler.get("total_us"))
            aggregate["max_us"] = max(
                aggregate["max_us"], _as_int(handler.get("max_us"))
            )
    handler_hot_paths = sorted(
        (
            {
                **handler,
                "average_us": round(
                    handler["total_us"] / max(1, handler["count"]), 3
                ),
            }
            for handler in aggregate_handler_timings.values()
        ),
        key=lambda item: (-item["total_us"], str(item["target_hex"])),
    )
    total_steps = sum(_as_int(run.get("steps")) for run in native_runs)
    total_elapsed_us = sum(
        _as_int((run.get("performance") or {}).get("elapsed_us"))
        for run in native_runs
    )
    counter_totals = {field: 0 for field in NATIVE_COUNTER_FIELDS}
    observed_counter_fields: set[str] = set()
    aggregate_timings: dict[str, dict[str, Any]] = {}
    read_callback_sample_counts: Counter[tuple[str, int]] = Counter()
    read_callback_sample_intervals: set[int] = set()
    memory_callback_samples: dict[tuple[str, int], dict[str, int]] = {}
    dispatch_targets: dict[int, dict[str, Any]] = {}
    native_service_targets: dict[tuple[int, int, int], dict[str, Any]] = {}
    dispatch_edges: dict[tuple[int, int], dict[str, Any]] = {}
    module_exit_reason_counts: Counter[str] = Counter()
    module_exit_profiled_run_count = 0
    module_exit_unclassified_call_count = 0
    module_edge_profiled_run_count = 0
    module_edge_profiles_exact = True
    module_edge_overflow_call_count = 0
    module_edge_unclassified_call_count = 0
    module_edge_table_capacities: set[int] = set()
    profiled_native_run_count = 0
    profile_capture_windows: list[dict[str, Any]] = []
    native_observer_dispatch_values: list[bool] = []
    for run in native_runs:
        performance = run.get("performance") or {}
        if performance.get("hot_path_profiling_enabled"):
            profiled_native_run_count += 1
            profile_capture_windows.append(
                performance.get("profile_capture_window") or {}
            )
            native_observer_dispatch_values.append(
                performance.get("native_observer_dispatch_enabled") is True
            )
        module_exit_profile = performance.get("native_module_exit_profile") or {}
        if module_exit_profile.get("enabled"):
            module_exit_profiled_run_count += 1
            module_exit_unclassified_call_count += _as_int(
                module_exit_profile.get("unclassified_module_calls")
            )
            module_exit_reason_counts.update(
                {
                    str(name): _as_int(count)
                    for name, count in (
                        module_exit_profile.get("reason_counts") or {}
                    ).items()
                }
            )
        module_edge_profile = performance.get("native_module_edge_profile") or {}
        if module_edge_profile.get("enabled"):
            module_edge_profiled_run_count += 1
            module_edge_profiles_exact = (
                module_edge_profiles_exact
                and module_edge_profile.get("exact") is True
            )
            module_edge_overflow_call_count += _as_int(
                module_edge_profile.get("overflow_module_calls")
            )
            module_edge_unclassified_call_count += _as_int(
                module_edge_profile.get("unclassified_module_calls")
            )
            table_capacity = _as_int(module_edge_profile.get("table_capacity"))
            if table_capacity:
                module_edge_table_capacities.add(table_capacity)
            for edge in module_edge_profile.get("edges", []):
                entry_target = _as_int(edge.get("entry_target"), -1)
                exit_target = _as_int(edge.get("exit_target"), -1)
                if entry_target < 0 or exit_target < 0:
                    continue
                aggregate = dispatch_edges.setdefault(
                    (entry_target, exit_target),
                    {
                        "module_calls": 0,
                        "module_exit_reasons": Counter(),
                    },
                )
                aggregate["module_calls"] += _as_int(edge.get("module_calls"))
                aggregate["module_exit_reasons"].update(
                    {
                        str(name): _as_int(count)
                        for name, count in (
                            edge.get("module_exit_reasons") or {}
                        ).items()
                    }
                )
        for field in NATIVE_COUNTER_FIELDS:
            if field in performance:
                observed_counter_fields.add(field)
                counter_totals[field] += _as_int(performance.get(field))
        for name, timing in performance.get("timings", {}).items():
            aggregate = aggregate_timings.setdefault(
                str(name),
                {"count": 0, "total_us": 0, "max_us": 0},
            )
            aggregate["count"] += _as_int(timing.get("count"))
            aggregate["total_us"] += _as_int(timing.get("total_us"))
            aggregate["max_us"] = max(
                aggregate["max_us"], _as_int(timing.get("max_us"))
            )
        read_sampling = performance.get("read_callback_sampling") or {}
        interval = _as_int(read_sampling.get("interval"))
        if interval:
            read_callback_sample_intervals.add(interval)
        for sample in read_sampling.get("hot_addresses", []):
            try:
                address = int(sample.get("address"))
            except (TypeError, ValueError):
                continue
            read_callback_sample_counts[(str(sample.get("kind")), address)] += (
                _as_int(sample.get("sample_count"))
            )
        callback_sampling = performance.get("memory_callback_sampling") or {}
        for sample in callback_sampling.get("hot_addresses", []):
            try:
                address = int(sample.get("address"))
            except (TypeError, ValueError):
                continue
            key = (str(sample.get("kind")), address)
            aggregate = memory_callback_samples.setdefault(
                key,
                {"sample_count": 0, "sampled_total_us": 0, "sampled_max_us": 0},
            )
            aggregate["sample_count"] += _as_int(sample.get("sample_count"))
            aggregate["sampled_total_us"] += _as_int(
                sample.get("sampled_total_us")
            )
            aggregate["sampled_max_us"] = max(
                aggregate["sampled_max_us"],
                _as_int(sample.get("sampled_max_us")),
            )
        for target in performance.get("native_dispatch_hot_targets", []):
            address = _as_int(target.get("target"), -1)
            if address < 0:
                continue
            aggregate = dispatch_targets.setdefault(
                address,
                {
                    "module_calls": 0,
                    "guest_steps": 0,
                    "timing_sample_count": 0,
                    "sampled_total_ns": 0,
                    "sampled_total_ns_squared": 0.0,
                    "sampled_min_ns": None,
                    "sampled_max_ns": None,
                    "sampled_guest_steps": 0,
                    "sampled_min_guest_steps": None,
                    "sampled_max_guest_steps": None,
                    "timing_sample_intervals": set(),
                    "module_exit_reasons": Counter(),
                    "execution_lanes": {
                        lane: {"module_calls": 0, "guest_steps": 0}
                        for lane in ("primary", "worker", "vblank")
                    },
                },
            )
            aggregate["module_calls"] += _as_int(target.get("module_calls"))
            aggregate["guest_steps"] += _as_int(target.get("guest_steps"))
            aggregate["timing_sample_count"] += _as_int(
                target.get("timing_sample_count")
            )
            aggregate["sampled_total_ns"] += _as_int(
                target.get("sampled_total_ns")
            )
            aggregate["sampled_total_ns_squared"] += float(
                target.get("sampled_total_ns_squared") or 0.0
            )
            for field in (
                "sampled_min_ns",
                "sampled_min_guest_steps",
            ):
                value = target.get(field)
                if value is not None:
                    aggregate[field] = (
                        _as_int(value)
                        if aggregate[field] is None
                        else min(aggregate[field], _as_int(value))
                    )
            for field in (
                "sampled_max_ns",
                "sampled_max_guest_steps",
            ):
                value = target.get(field)
                if value is not None:
                    aggregate[field] = (
                        _as_int(value)
                        if aggregate[field] is None
                        else max(aggregate[field], _as_int(value))
                    )
            aggregate["sampled_guest_steps"] += _as_int(
                target.get("sampled_guest_steps")
            )
            for lane_name, lane in (
                target.get("execution_lanes") or {}
            ).items():
                if lane_name not in aggregate["execution_lanes"]:
                    continue
                aggregate["execution_lanes"][lane_name][
                    "module_calls"
                ] += _as_int((lane or {}).get("module_calls"))
                aggregate["execution_lanes"][lane_name][
                    "guest_steps"
                ] += _as_int((lane or {}).get("guest_steps"))
            timing_sample_interval = _as_int(
                target.get("timing_sample_interval")
            )
            if timing_sample_interval:
                aggregate["timing_sample_intervals"].add(
                    timing_sample_interval
                )
            aggregate["module_exit_reasons"].update(
                {
                    str(name): _as_int(count)
                    for name, count in (
                        target.get("module_exit_reasons") or {}
                    ).items()
                }
            )
        for service in performance.get(
            "native_host_service_hot_targets", []
        ):
            key = (
                _as_int(service.get("target")),
                _as_int(service.get("kind")),
                _as_int(service.get("value")),
            )
            aggregate = native_service_targets.setdefault(
                key,
                {
                    "kind_name": service.get("kind_name"),
                    "calls": 0,
                    "timing_sample_count": 0,
                    "sampled_total_ns": 0,
                    "sampled_total_ns_squared": 0.0,
                    "sampled_min_ns": None,
                    "sampled_max_ns": None,
                    "execution_lanes": {
                        lane: {"calls": 0}
                        for lane in ("primary", "worker", "vblank")
                    },
                },
            )
            aggregate["calls"] += _as_int(service.get("calls"))
            aggregate["timing_sample_count"] += _as_int(
                service.get("timing_sample_count")
            )
            aggregate["sampled_total_ns"] += _as_int(
                service.get("sampled_total_ns")
            )
            aggregate["sampled_total_ns_squared"] += float(
                service.get("sampled_total_ns_squared") or 0.0
            )
            for field, operation in (
                ("sampled_min_ns", min),
                ("sampled_max_ns", max),
            ):
                value = service.get(field)
                if value is None:
                    continue
                aggregate[field] = (
                    _as_int(value)
                    if aggregate[field] is None
                    else operation(aggregate[field], _as_int(value))
                )
            for lane_name, lane in (
                service.get("execution_lanes") or {}
            ).items():
                if lane_name in aggregate["execution_lanes"]:
                    aggregate["execution_lanes"][lane_name][
                        "calls"
                    ] += _as_int((lane or {}).get("calls"))
    timing_hot_paths = sorted(
        (
            {
                "name": name,
                **timing,
                "average_us": round(
                    timing["total_us"] / max(1, timing["count"]), 3
                ),
            }
            for name, timing in aggregate_timings.items()
        ),
        key=lambda item: (-item["total_us"], item["name"]),
    )
    native_dispatch_inclusive_us = _as_int(
        aggregate_timings.get("native_dispatch", {}).get("total_us")
    )
    native_dispatch_self_us = _as_int(
        aggregate_timings.get("native_dispatch_self", {}).get("total_us")
    )
    native_dispatch_self_available = (
        "native_dispatch_self" in aggregate_timings
    )
    dirty_sync_calls = counter_totals["dirty_sync_call_count"]
    memory_read_callbacks = (
        counter_totals["read_u32_callback_count"]
        + counter_totals["read_u8_callback_count"]
    )
    exact_read_callbacks = (
        counter_totals["exact_read_u32_callback_count"]
        + counter_totals["exact_read_u8_callback_count"]
    )
    dirty_page_sync_total_us = _as_int(
        aggregate_timings.get("dirty_page_sync", {}).get("total_us")
    )
    module_calls = counter_totals["native_module_call_count"]
    observed_write_batches = counter_totals["native_observed_write_batch_count"]
    observed_writes = counter_totals["native_observed_write_count"]
    callback_policy_cache_hits = counter_totals[
        "memory_callback_policy_cache_hit_count"
    ]
    callback_policy_cache_misses = counter_totals[
        "memory_callback_policy_cache_miss_count"
    ]
    callback_policy_cache_queries = (
        callback_policy_cache_hits + callback_policy_cache_misses
    )
    empty_dependency_sync_bypasses = counter_totals[
        "empty_dependency_sync_bypass_count"
    ]
    callback_sync_boundaries = (
        counter_totals["selective_dirty_sync_call_count"]
        + empty_dependency_sync_bypasses
    )
    dispatch_edge_records = [
        {
            "entry_target": entry_target,
            "entry_target_hex": f"0x{entry_target:08X}",
            "exit_target": exit_target,
            "exit_target_hex": f"0x{exit_target:08X}",
            "module_calls": metric["module_calls"],
            "module_exit_reasons": dict(
                sorted(metric["module_exit_reasons"].items())
            ),
        }
        for (entry_target, exit_target), metric in sorted(
            dispatch_edges.items(),
            key=lambda item: (
                -item[1]["module_calls"],
                item[0][0],
                item[0][1],
            ),
        )
    ]
    dispatch_target_records = []
    for address, metric in dispatch_targets.items():
        timing_sample_count = metric["timing_sample_count"]
        sampled_total_ns = metric["sampled_total_ns"]
        sampled_total_ns_squared = metric["sampled_total_ns_squared"]
        sampled_average_ns = (
            sampled_total_ns / timing_sample_count
            if timing_sample_count
            else 0.0
        )
        sample_variance_ns_squared = (
            max(
                0.0,
                (
                    sampled_total_ns_squared
                    - sampled_total_ns * sampled_total_ns / timing_sample_count
                )
                / (timing_sample_count - 1),
            )
            if timing_sample_count > 1
            else None
        )
        sample_standard_deviation_ns = (
            math.sqrt(sample_variance_ns_squared)
            if sample_variance_ns_squared is not None
            else None
        )
        sample_standard_error_ns = (
            sample_standard_deviation_ns / math.sqrt(timing_sample_count)
            if sample_standard_deviation_ns is not None
            else None
        )
        confidence_margin_ns = (
            1.96 * sample_standard_error_ns
            if sample_standard_error_ns is not None
            else None
        )
        relative_margin = (
            confidence_margin_ns / sampled_average_ns
            if confidence_margin_ns is not None and sampled_average_ns
            else None
        )
        estimated_native_time_us = (
            sampled_average_ns * metric["module_calls"] / 1_000.0
        )
        timing_confidence = (
            "high"
            if timing_sample_count >= 64
            and relative_margin is not None
            and relative_margin <= 0.10
            else "medium"
            if timing_sample_count >= 16
            and relative_margin is not None
            and relative_margin <= 0.25
            else "low"
        )
        execution_lanes = {
            lane_name: {
                **lane,
                "call_ratio": (
                    round(lane["module_calls"] / metric["module_calls"], 6)
                    if metric["module_calls"]
                    else None
                ),
                "step_ratio": (
                    round(lane["guest_steps"] / metric["guest_steps"], 6)
                    if metric["guest_steps"]
                    else None
                ),
            }
            for lane_name, lane in metric["execution_lanes"].items()
        }
        dispatch_target_records.append(
            {
                "target": address,
                "target_hex": f"0x{address:08X}",
                "module_calls": metric["module_calls"],
                "guest_steps": metric["guest_steps"],
                "module_exit_reasons": dict(
                    sorted(metric["module_exit_reasons"].items())
                ),
                "average_guest_steps_per_call": round(
                    metric["guest_steps"] / max(1, metric["module_calls"]),
                    3,
                ),
                "timing_mode": "deterministic_sampled",
                "timing_sample_intervals": sorted(
                    metric["timing_sample_intervals"]
                ),
                "timing_sample_count": timing_sample_count,
                "timing_sample_coverage_ratio": (
                    round(timing_sample_count / metric["module_calls"], 9)
                    if metric["module_calls"]
                    else None
                ),
                "sampled_total_ns": sampled_total_ns,
                "sampled_total_ns_squared": round(
                    sampled_total_ns_squared,
                    3,
                ),
                "sampled_average_ns": round(sampled_average_ns, 3),
                "sampled_min_ns": metric["sampled_min_ns"],
                "sampled_max_ns": metric["sampled_max_ns"],
                "sample_standard_deviation_ns": (
                    round(sample_standard_deviation_ns, 3)
                    if sample_standard_deviation_ns is not None
                    else None
                ),
                "sample_standard_error_ns": (
                    round(sample_standard_error_ns, 3)
                    if sample_standard_error_ns is not None
                    else None
                ),
                "relative_margin_of_error_95": (
                    round(relative_margin, 6)
                    if relative_margin is not None
                    else None
                ),
                "sampled_guest_steps": metric["sampled_guest_steps"],
                "sampled_min_guest_steps": metric[
                    "sampled_min_guest_steps"
                ],
                "sampled_max_guest_steps": metric[
                    "sampled_max_guest_steps"
                ],
                "sampled_ns_per_guest_step": (
                    round(
                        sampled_total_ns / metric["sampled_guest_steps"],
                        3,
                    )
                    if metric["sampled_guest_steps"]
                    else None
                ),
                "estimated_native_time_us": round(
                    estimated_native_time_us,
                    3,
                ),
                "estimated_native_time_95_interval_us": (
                    {
                        "lower": round(
                            max(0.0, sampled_average_ns - confidence_margin_ns)
                            * metric["module_calls"]
                            / 1_000.0,
                            3,
                        ),
                        "upper": round(
                            (sampled_average_ns + confidence_margin_ns)
                            * metric["module_calls"]
                            / 1_000.0,
                            3,
                        ),
                    }
                    if confidence_margin_ns is not None
                    else None
                ),
                "timing_confidence": timing_confidence,
                "execution_lanes": execution_lanes,
            }
        )
    native_service_target_records: list[dict[str, Any]] = []
    for (address, kind, value), metric in native_service_targets.items():
        sample_count = metric["timing_sample_count"]
        sampled_total_ns = metric["sampled_total_ns"]
        sampled_average_ns = (
            sampled_total_ns / sample_count if sample_count else 0.0
        )
        variance = (
            max(
                0.0,
                (
                    metric["sampled_total_ns_squared"]
                    - sampled_total_ns * sampled_total_ns / sample_count
                )
                / (sample_count - 1),
            )
            if sample_count > 1
            else None
        )
        standard_deviation_ns = (
            math.sqrt(variance) if variance is not None else None
        )
        standard_error_ns = (
            standard_deviation_ns / math.sqrt(sample_count)
            if standard_deviation_ns is not None
            else None
        )
        margin_ns = (
            1.96 * standard_error_ns
            if standard_error_ns is not None
            else None
        )
        relative_margin = (
            margin_ns / sampled_average_ns
            if margin_ns is not None and sampled_average_ns
            else None
        )
        lanes = {
            lane_name: {
                **lane,
                "call_ratio": (
                    round(lane["calls"] / metric["calls"], 6)
                    if metric["calls"]
                    else None
                ),
            }
            for lane_name, lane in metric["execution_lanes"].items()
        }
        native_service_target_records.append(
            {
                "target": address,
                "target_hex": f"0x{address:08X}",
                "kind": kind,
                "kind_name": metric["kind_name"],
                "value": value,
                "calls": metric["calls"],
                "timing_mode": "deterministic_sampled",
                "timing_sample_interval": 256,
                "timing_sample_count": sample_count,
                "sampled_total_ns": sampled_total_ns,
                "sampled_total_ns_squared": round(
                    metric["sampled_total_ns_squared"], 3
                ),
                "sampled_average_ns": round(sampled_average_ns, 3),
                "sampled_min_ns": metric["sampled_min_ns"],
                "sampled_max_ns": metric["sampled_max_ns"],
                "sample_standard_deviation_ns": (
                    round(standard_deviation_ns, 3)
                    if standard_deviation_ns is not None
                    else None
                ),
                "relative_margin_of_error_95": (
                    round(relative_margin, 6)
                    if relative_margin is not None
                    else None
                ),
                "estimated_native_time_us": round(
                    sampled_average_ns * metric["calls"] / 1_000.0,
                    3,
                ),
                "timing_confidence": (
                    "high"
                    if sample_count >= 64
                    and relative_margin is not None
                    and relative_margin <= 0.10
                    else "medium"
                    if sample_count >= 16
                    and relative_margin is not None
                    and relative_margin <= 0.25
                    else "low"
                ),
                "execution_lanes": lanes,
            }
        )
    native_service_target_records.sort(
        key=lambda item: (
            -item["estimated_native_time_us"],
            -item["calls"],
            item["target"],
        )
    )
    dispatch_targets_by_guest_steps = sorted(
        dispatch_target_records,
        key=lambda item: (
            -item["guest_steps"],
            -item["module_calls"],
            item["target"],
        ),
    )
    dispatch_targets_by_native_time = sorted(
        dispatch_target_records,
        key=lambda item: (
            -item["estimated_native_time_us"],
            -item["guest_steps"],
            item["target"],
        ),
    )
    return {
        "native_run_count": len(native_runs),
        "profiled_native_run_count": profiled_native_run_count,
        "hot_path_profile": {
            "enabled": profiled_native_run_count > 0,
            "profiled_native_run_count": profiled_native_run_count,
            "native_observer_dispatch_all": (
                bool(native_observer_dispatch_values)
                and all(native_observer_dispatch_values)
            ),
            "captured_module_calls": sum(
                _as_int(window.get("captured_module_calls"))
                for window in profile_capture_windows
            ),
            "captured_guest_steps": sum(
                _as_int(window.get("captured_guest_steps"))
                for window in profile_capture_windows
            ),
            "captured_elapsed_us": sum(
                _as_int(window.get("captured_elapsed_us"))
                for window in profile_capture_windows
            ),
            "estimated_profile_bookkeeping_us": sum(
                _as_int(
                    (
                        window.get("profiling_overhead_estimate") or {}
                    ).get("estimated_total_us")
                )
                for window in profile_capture_windows
            ),
            "capture_windows": profile_capture_windows,
        },
        "native_module_exit_profile": {
            "enabled": module_exit_profiled_run_count > 0,
            "profiled_native_run_count": module_exit_profiled_run_count,
            "reason_counts": dict(sorted(module_exit_reason_counts.items())),
            "classified_module_calls": sum(module_exit_reason_counts.values()),
            "unclassified_module_calls": module_exit_unclassified_call_count,
        },
        "native_module_edge_profile": {
            "enabled": module_edge_profiled_run_count > 0,
            "profiled_native_run_count": module_edge_profiled_run_count,
            "exact": (
                module_edge_profiled_run_count > 0
                and module_edge_profiles_exact
                and module_edge_overflow_call_count == 0
                and module_edge_unclassified_call_count == 0
            ),
            "table_capacities": sorted(module_edge_table_capacities),
            "unique_edge_count": len(dispatch_edge_records),
            "classified_module_calls": sum(
                edge["module_calls"] for edge in dispatch_edge_records
            ),
            "unclassified_module_calls": module_edge_unclassified_call_count,
            "overflow_module_calls": module_edge_overflow_call_count,
            "edges": dispatch_edge_records,
        },
        "native_runs": native_runs,
        "total_steps": total_steps,
        "elapsed_total_us": total_elapsed_us,
        "elapsed_total_seconds": round(total_elapsed_us / 1_000_000, 6),
        "effective_steps_per_second": (
            round(total_steps * 1_000_000 / total_elapsed_us, 3)
            if total_elapsed_us
            else None
        ),
        "counter_totals": counter_totals,
        "counter_fields_observed": sorted(observed_counter_fields),
        "timing_hot_paths": timing_hot_paths,
        "native_dispatch_timing": {
            "inclusive_us": native_dispatch_inclusive_us,
            "self_us": (
                native_dispatch_self_us
                if native_dispatch_self_available
                else None
            ),
            "compiled_module_and_call_us": (
                max(
                    0,
                    native_dispatch_inclusive_us - native_dispatch_self_us,
                )
                if native_dispatch_self_available
                else None
            ),
            "self_ratio": (
                round(
                    native_dispatch_self_us / native_dispatch_inclusive_us,
                    6,
                )
                if native_dispatch_inclusive_us
                and native_dispatch_self_available
                else None
            ),
            "exclusive_measurement_available": native_dispatch_self_available,
            "target_measurement_mode": "deterministic_sampled",
        },
        "dirty_sync_no_work_ratio": (
            round(counter_totals["dirty_sync_no_work_count"] / dirty_sync_calls, 6)
            if dirty_sync_calls and "dirty_sync_no_work_count" in observed_counter_fields
            else None
        ),
        "dirty_page_sync_time_ratio": (
            round(dirty_page_sync_total_us / total_elapsed_us, 6)
            if total_elapsed_us
            else None
        ),
        "page_cache_fills_per_million_steps": (
            round(counter_totals["page_cache_fill_count"] * 1_000_000 / total_steps, 3)
            if total_steps
            else None
        ),
        "memory_read_callbacks_per_million_steps": (
            round(memory_read_callbacks * 1_000_000 / total_steps, 3)
            if total_steps
            else None
        ),
        "native_module_calls_per_million_steps": (
            round(module_calls * 1_000_000 / total_steps, 3)
            if total_steps
            else None
        ),
        "average_observed_writes_per_batch": (
            round(observed_writes / observed_write_batches, 3)
            if observed_write_batches
            else None
        ),
        "memory_callback_policy_cache_hit_ratio": (
            round(callback_policy_cache_hits / callback_policy_cache_queries, 6)
            if callback_policy_cache_queries
            else None
        ),
        "empty_dependency_sync_bypass_ratio": (
            round(empty_dependency_sync_bypasses / callback_sync_boundaries, 6)
            if callback_sync_boundaries
            and "empty_dependency_sync_bypass_count" in observed_counter_fields
            else None
        ),
        "exact_read_callback_ratio": (
            round(exact_read_callbacks / memory_read_callbacks, 6)
            if memory_read_callbacks
            and (
                "exact_read_u32_callback_count" in observed_counter_fields
                or "exact_read_u8_callback_count" in observed_counter_fields
            )
            else None
        ),
        "read_callback_sampling": {
            "intervals": sorted(read_callback_sample_intervals),
            "sample_count": sum(read_callback_sample_counts.values()),
            "high_address_sample_count": sum(
                count
                for (_kind, address), count in read_callback_sample_counts.items()
                if address >= 0x80000000
            ),
            "hot_addresses": [
                {
                    "kind": kind,
                    "address": address,
                    "address_hex": f"0x{address:08X}",
                    "sample_count": count,
                }
                for (kind, address), count in read_callback_sample_counts.most_common(
                    32
                )
            ],
        },
        "memory_callback_sampling": {
            "sample_count": sum(
                metric["sample_count"] for metric in memory_callback_samples.values()
            ),
            "hot_addresses": [
                {
                    "kind": kind,
                    "address": address,
                    "address_hex": f"0x{address:08X}",
                    **metric,
                    "sampled_average_us": round(
                        metric["sampled_total_us"]
                        / max(1, metric["sample_count"]),
                        3,
                    ),
                }
                for (kind, address), metric in sorted(
                    memory_callback_samples.items(),
                    key=lambda item: (
                        -item[1]["sampled_total_us"],
                        item[0][0],
                        item[0][1],
                    ),
                )[:32]
            ],
        },
        "native_dispatch_targets": dispatch_targets_by_guest_steps,
        "native_dispatch_hot_targets": dispatch_targets_by_guest_steps[:128],
        "native_dispatch_timing_targets": (
            dispatch_targets_by_native_time[:128]
        ),
        "native_host_service_hot_targets": native_service_target_records,
        "handler_hot_paths": handler_hot_paths,
        "live_host_bridge": live_bridges[-1]["performance"] if live_bridges else None,
        "live_host_bridge_summary": latest_live_bridge_summary,
        "live_host_bridges": live_bridges,
    }


def _summarize_live_capture_coverage(
    retained_events: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    f12_readbacks = [
        event
        for event in retained_events["frame_readback_captured"]
        if event.get("trigger") == "f12"
    ]
    retention_events = retained_events["hotkey_render_capture_retained"]
    offscreen_events = retained_events[
        "nv2a_offscreen_render_targets_created"
    ]
    retained_manifests = [
        event
        for event in retention_events
        if event.get("manifest")
        and event.get("command_snapshot_copied") is True
        and event.get("resource_snapshot_copied") is True
    ]
    failed_retention_events = [
        event
        for event in retention_events
        if event.get("command_snapshot_copied") is not True
        or event.get("resource_snapshot_copied") is not True
        or not event.get("manifest")
    ]
    unretained_readbacks = [
        event
        for event in f12_readbacks
        if not event.get("render_capture_manifest")
    ]
    latest_offscreen = offscreen_events[-1] if offscreen_events else None
    offscreen_depth_isolated = None
    if latest_offscreen is not None:
        target_count = _as_int(latest_offscreen.get("count"))
        dedicated_depth_count = latest_offscreen.get("dedicated_depth_count")
        shares_presented_depth = latest_offscreen.get("shares_presented_depth")
        if shares_presented_depth is not None:
            offscreen_depth_isolated = (
                shares_presented_depth is False
                and _as_int(dedicated_depth_count) == target_count
            )
    return {
        "status": (
            "capture_retained"
            if retained_manifests
            else "capture_not_retained"
            if f12_readbacks
            else "not_captured"
        ),
        "f12_readback_count": len(f12_readbacks),
        "retained_capture_count": len(retained_manifests),
        "failed_retention_count": len(failed_retention_events),
        "unretained_f12_readback_count": len(unretained_readbacks),
        "latest_f12_readback": f12_readbacks[-1] if f12_readbacks else None,
        "latest_retention": retention_events[-1] if retention_events else None,
        "latest_offscreen_target_state": latest_offscreen,
        "offscreen_depth_isolated": offscreen_depth_isolated,
        "retained_manifests": [
            event.get("manifest") for event in retained_manifests
        ],
    }


def _summarize_render_target_feedback_cache(
    retained_events: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    resource_events = retained_events["nv2a_native_resources_created"]
    state_events = retained_events["nv2a_render_state_diagnostics"]
    latest_resource = resource_events[-1] if resource_events else None
    required_state_events = [
        event
        for event in state_events
        if event.get("render_target_feedback_required") is not None
    ]
    latest_required_state = (
        required_state_events[-1] if required_state_events else None
    )
    required_source = latest_resource
    if (
        required_source is None
        or required_source.get("render_target_feedback_required") is None
    ):
        required_source = latest_required_state
    active_count = (
        _as_int(latest_resource.get("render_target_feedback_textures"))
        if latest_resource is not None
        and latest_resource.get("render_target_feedback_textures") is not None
        else None
    )
    required_count = (
        _as_int(required_source.get("render_target_feedback_required"))
        if required_source is not None
        else None
    )
    explicit_missing = (
        latest_resource.get("render_target_feedback_missing")
        if latest_resource is not None
        else None
    )
    explicit_stale = (
        latest_resource.get("render_target_feedback_stale")
        if latest_resource is not None
        else None
    )
    missing_count = (
        _as_int(explicit_missing)
        if explicit_missing is not None
        else max((required_count or 0) - (active_count or 0), 0)
        if active_count is not None and required_count is not None
        else None
    )
    stale_count = (
        _as_int(explicit_stale)
        if explicit_stale is not None
        else max((active_count or 0) - (required_count or 0), 0)
        if active_count is not None and required_count is not None
        else None
    )
    instrumented = active_count is not None and required_count is not None
    mismatch = instrumented and (
        active_count != required_count
        or (missing_count or 0) != 0
        or (stale_count or 0) != 0
    )
    evidence = None
    if instrumented:
        evidence = {
            "active_count": active_count,
            "required_count": required_count,
            "missing_count": missing_count,
            "stale_count": stale_count,
            "active_addresses": latest_resource.get(
                "render_target_feedback_active_addresses", []
            )
            if latest_resource is not None
            else [],
            "required_addresses": required_source.get(
                "render_target_feedback_required_addresses", []
            )
            if required_source is not None
            else [],
            "active_event": latest_resource,
            "required_event": required_source,
        }
    return {
        "status": (
            "mismatch"
            if mismatch
            else "healthy"
            if instrumented
            else "not_instrumented"
            if resource_events or state_events
            else "not_captured"
        ),
        "event_count": len(resource_events),
        "instrumented_event_count": 1 if instrumented else 0,
        "mismatch_event_count": 1 if mismatch else 0,
        "active_count": active_count,
        "required_count": required_count,
        "missing_count": missing_count,
        "stale_count": stale_count,
        "pruned_count": (
            _as_int(latest_resource.get("render_target_feedback_pruned"))
            if latest_resource is not None
            and latest_resource.get("render_target_feedback_pruned") is not None
            else None
        ),
        "required_addresses": (
            required_source.get("render_target_feedback_required_addresses", [])
            if required_source is not None
            else []
        ),
        "active_addresses": (
            latest_resource.get("render_target_feedback_active_addresses", [])
            if latest_resource is not None
            else []
        ),
        "latest_event": latest_resource,
        "latest_required_event": required_source,
        "latest_mismatch": evidence if mismatch else None,
    }


def _rank_diagnostic_findings(
    *,
    vertex_transforms: dict[str, Any],
    render_state_coverage: dict[str, Any],
    transform_constant_upload_provenance: dict[str, Any],
    world_matrix_audit: dict[str, Any],
    guest_performance: dict[str, Any],
    presenter_performance: dict[str, Any],
    composition_coverage: dict[str, Any],
    live_capture_coverage: dict[str, Any],
    render_target_feedback_cache: dict[str, Any],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if render_target_feedback_cache.get("status") == "mismatch":
        findings.append(
            {
                "id": "render_target_feedback_cache_mismatch",
                "severity": "critical",
                "category": "rendering",
                "evidence": render_target_feedback_cache.get(
                    "latest_mismatch"
                ),
            }
        )
    if live_capture_coverage.get("offscreen_depth_isolated") is False:
        findings.append(
            {
                "id": "offscreen_target_shares_presented_depth",
                "severity": "critical",
                "category": "rendering",
                "evidence": live_capture_coverage.get(
                    "latest_offscreen_target_state"
                ),
            }
        )
    if _as_int(live_capture_coverage.get("failed_retention_count")):
        findings.append(
            {
                "id": "hotkey_render_capture_copy_failed",
                "severity": "critical",
                "category": "diagnostics",
                "evidence": live_capture_coverage.get("latest_retention"),
            }
        )
    elif _as_int(
        live_capture_coverage.get("unretained_f12_readback_count")
    ):
        findings.append(
            {
                "id": "hotkey_render_capture_not_retained",
                "severity": "high",
                "category": "diagnostics",
                "evidence": live_capture_coverage.get("latest_f12_readback"),
            }
        )
    latest_transform = vertex_transforms.get("latest_event") or {}
    invalid_vertices = _as_int(latest_transform.get("invalid_program_vertex_count"))
    if invalid_vertices:
        findings.append(
            {
                "id": "invalid_nv2a_vertex_program_execution",
                "severity": "critical",
                "category": "rendering",
                "evidence": {"invalid_program_vertex_count": invalid_vertices},
            }
        )
    if (
        _as_int(latest_transform.get("indexed_program_draw_count")) > 0
        and latest_transform.get("viewport_constants_valid") is False
    ):
        findings.append(
            {
                "id": "missing_or_zero_nv2a_viewport_constants",
                "severity": "critical",
                "category": "rendering",
                "evidence": {
                    key: latest_transform.get(key)
                    for key in (
                        "viewport_scale_x",
                        "viewport_scale_y",
                        "viewport_offset_x",
                        "viewport_offset_y",
                    )
                },
            }
        )
    singular_world_matrix_count = _as_int(
        world_matrix_audit.get("singular_matrix_count")
    )
    if world_matrix_audit.get("enabled") and singular_world_matrix_count:
        findings.append(
            {
                "id": "singular_title_world_matrix_input",
                "severity": "critical",
                "category": "rendering",
                "evidence": {
                    "invocation_count": world_matrix_audit.get(
                        "invocation_count"
                    ),
                    "singular_matrix_count": singular_world_matrix_count,
                    "nonfinite_matrix_count": world_matrix_audit.get(
                        "nonfinite_matrix_count"
                    ),
                    "source_records": world_matrix_audit.get(
                        "source_records", []
                    )[:16],
                    "latest_samples": world_matrix_audit.get(
                        "singular_samples", []
                    )[-8:],
                },
            }
        )
    indexed_draw_audit = world_matrix_audit.get("indexed_draw_audit") or {}
    if (
        world_matrix_audit.get("enabled")
        and _as_int(indexed_draw_audit.get("zero_index_draw_count"))
    ):
        findings.append(
            {
                "id": "zero_index_world_draw_submissions",
                "severity": "critical",
                "category": "rendering",
                "evidence": {
                    "world_draw_callback_count": indexed_draw_audit.get(
                        "world_draw_callback_count"
                    ),
                    "world_draw_callback_zero_index_count": indexed_draw_audit.get(
                        "world_draw_callback_zero_index_count"
                    ),
                    "indexed_draw_count": indexed_draw_audit.get(
                        "indexed_draw_count"
                    ),
                    "zero_index_draw_count": indexed_draw_audit.get(
                        "zero_index_draw_count"
                    ),
                    "indexed_draw_caller_counts": indexed_draw_audit.get(
                        "indexed_draw_caller_counts", []
                    )[:16],
                    "world_draw_callback_caller_samples": indexed_draw_audit.get(
                        "world_draw_callback_caller_samples", []
                    )[:16],
                    "latest_world_draw_callback_samples": indexed_draw_audit.get(
                        "world_draw_callback_samples", []
                    )[-8:],
                    "latest_indexed_draw_samples": indexed_draw_audit.get(
                        "indexed_draw_samples", []
                    )[-8:],
                },
            }
        )
    world_matrix_write_audit = world_matrix_audit.get("write_audit") or {}
    if _as_int(world_matrix_write_audit.get("singularizing_write_count")):
        findings.append(
            {
                "id": "title_world_matrix_became_singular_on_write",
                "severity": "critical",
                "category": "rendering",
                "evidence": {
                    "addresses_hex": world_matrix_write_audit.get(
                        "addresses_hex", []
                    ),
                    "singularizing_write_count": world_matrix_write_audit.get(
                        "singularizing_write_count"
                    ),
                    "instruction_counts": world_matrix_write_audit.get(
                        "instruction_counts", []
                    )[:16],
                    "transition_samples": world_matrix_write_audit.get(
                        "transition_samples", []
                    )[-8:],
                },
            }
        )
    world_matrix_rotation_audit = world_matrix_audit.get("rotation_audit") or {}
    if _as_int(world_matrix_rotation_audit.get("singular_output_count")):
        findings.append(
            {
                "id": "title_world_matrix_rotation_output_singular",
                "severity": "critical",
                "category": "rendering",
                "evidence": {
                    "matrix_addresses_hex": world_matrix_rotation_audit.get(
                        "matrix_addresses_hex", []
                    ),
                    "entry_count": world_matrix_rotation_audit.get("entry_count"),
                    "completion_count": world_matrix_rotation_audit.get(
                        "completion_count"
                    ),
                    "singular_input_count": world_matrix_rotation_audit.get(
                        "singular_input_count"
                    ),
                    "singular_output_count": world_matrix_rotation_audit.get(
                        "singular_output_count"
                    ),
                    "stage_records": world_matrix_rotation_audit.get(
                        "stage_records", []
                    ),
                    "latest_samples": world_matrix_rotation_audit.get(
                        "samples", []
                    )[-8:],
                },
            }
        )
    near_zero_position_basis_draw_count = _as_int(
        vertex_transforms.get("near_zero_position_c96_c99_basis_draw_count")
    )
    if near_zero_position_basis_draw_count:
        findings.append(
            {
                "id": "indexed_program_position_basis_near_zero",
                "severity": "critical",
                "category": "rendering",
                "evidence": {
                    "draw_count": near_zero_position_basis_draw_count,
                    "largest_draws": vertex_transforms.get(
                        "tiny_indexed_program_draws", []
                    )[:8],
                    "guest_upload_provenance": {
                        key: transform_constant_upload_provenance.get(key)
                        for key in (
                            "status",
                            "matrix_upload_count",
                            "near_zero_basis_matrix_upload_count",
                            "near_zero_producer_instruction_counts",
                        )
                    },
                },
            }
        )
    if bool(
        latest_transform.get(
            "indexed_program_tiny_coverage_suspected",
            latest_transform.get("coordinate_collapse_suspected"),
        )
    ):
        findings.append(
            {
                "id": "indexed_program_world_geometry_tiny_coverage",
                "severity": "critical",
                "category": "rendering",
                "evidence": {
                    key: latest_transform.get(key)
                    for key in (
                        "indexed_program_draw_count",
                        "indexed_program_vertex_count",
                        "subpixel_indexed_program_draw_count",
                        "tiny_indexed_program_draw_count",
                        "indexed_program_output_span_x",
                        "indexed_program_output_span_y",
                        "indexed_program_width_coverage",
                        "indexed_program_height_coverage",
                    )
                },
            }
        )
    latest_render_state = render_state_coverage.get("latest_event") or {}
    sampler_mismatch_count = _as_int(
        latest_render_state.get("sampler_address_mismatch_draw_count")
    )
    if sampler_mismatch_count:
        findings.append(
            {
                "id": "texture_sampler_address_mode_mismatch",
                "severity": "critical",
                "category": "rendering",
                "evidence": {
                    "mismatch_draw_count": sampler_mismatch_count,
                    "out_of_unit_texture_coordinate_draw_count": (
                        latest_render_state.get(
                            "out_of_unit_texture_coordinate_draw_count"
                        )
                    ),
                    "mismatching_draws": render_state_coverage.get(
                        "mismatching_draws", []
                    ),
                },
            }
        )
    alpha_test_mismatch_count = _as_int(
        latest_render_state.get("alpha_test_state_mismatch_draw_count")
    )
    if alpha_test_mismatch_count:
        findings.append(
            {
                "id": "alpha_test_state_not_applied",
                "severity": "critical",
                "category": "rendering",
                "evidence": {
                    "mismatch_draw_count": alpha_test_mismatch_count,
                    "alpha_test_enabled_draw_count": latest_render_state.get(
                        "alpha_test_enabled_draw_count"
                    ),
                    "alpha_test_applied_draw_count": latest_render_state.get(
                        "alpha_test_applied_draw_count"
                    ),
                    "alpha_blend_enabled_draw_count": latest_render_state.get(
                        "alpha_blend_enabled_draw_count"
                    ),
                    "mismatching_draws": render_state_coverage.get(
                        "mismatching_draws", []
                    ),
                },
            }
        )
    combiner_mismatch_count = _as_int(
        latest_render_state.get("register_combiner_state_mismatch_draw_count")
    )
    if combiner_mismatch_count:
        findings.append(
            {
                "id": "register_combiner_state_not_applied",
                "severity": "critical",
                "category": "rendering",
                "evidence": {
                    "mismatch_draw_count": combiner_mismatch_count,
                    "programmed_draw_count": latest_render_state.get(
                        "register_combiner_programmed_draw_count"
                    ),
                    "applied_draw_count": latest_render_state.get(
                        "register_combiner_applied_draw_count"
                    ),
                    "mismatching_draws": render_state_coverage.get(
                        "mismatching_draws", []
                    ),
                },
            }
        )
    fog_mismatch_count = _as_int(
        latest_render_state.get("fog_state_mismatch_draw_count")
    )
    if fog_mismatch_count:
        findings.append(
            {
                "id": "fog_factor_state_incomplete",
                "severity": (
                    "warning"
                    if not render_state_coverage.get("state_history_complete", True)
                    else "critical"
                ),
                "category": "rendering",
                "evidence": {
                    "mismatch_draw_count": fog_mismatch_count,
                    "fog_enabled_draw_count": latest_render_state.get(
                        "fog_enabled_draw_count"
                    ),
                    "fog_factor_applied_draw_count": latest_render_state.get(
                        "fog_factor_applied_draw_count"
                    ),
                    "state_history_complete": render_state_coverage.get(
                        "state_history_complete"
                    ),
                    "draws": render_state_coverage.get(
                        "fog_missing_draws", []
                    ),
                },
            }
        )
    texture_stage_mismatch_count = _as_int(
        latest_render_state.get("texture_stage_coverage_mismatch_draw_count")
    )
    if texture_stage_mismatch_count:
        findings.append(
            {
                "id": "multiple_texture_stages_not_fully_replayed",
                "severity": "high",
                "category": "rendering",
                "evidence": {
                    "mismatch_draw_count": texture_stage_mismatch_count,
                    "multiple_texture_stage_draw_count": latest_render_state.get(
                        "multiple_texture_stage_draw_count"
                    ),
                    "draws": render_state_coverage.get(
                        "multiple_texture_draws", []
                    ),
                },
            }
        )
    unproduced_zero_payload_count = _as_int(
        latest_render_state.get("unproduced_zero_payload_texture_draw_count")
    )
    missing_texture_resource_count = _as_int(
        render_state_coverage.get("missing_texture_resource_draw_count")
    )
    if missing_texture_resource_count:
        findings.append(
            {
                "id": "presented_texture_resources_missing",
                "severity": "critical",
                "category": "rendering",
                "evidence": {
                    "draw_count": missing_texture_resource_count,
                    "draws": render_state_coverage.get(
                        "missing_texture_resource_draws", []
                    ),
                },
            }
        )
    if unproduced_zero_payload_count:
        findings.append(
            {
                "id": "zero_texture_payload_without_gpu_producer",
                "severity": "critical",
                "category": "rendering",
                "evidence": {
                    "draw_count": unproduced_zero_payload_count,
                    "zero_payload_textured_draw_count": latest_render_state.get(
                        "zero_payload_textured_draw_count"
                    ),
                    "draws": [
                        draw
                        for draw in render_state_coverage.get(
                            "zero_payload_draws", []
                        )
                        if _as_int(draw.get("texture_producer_draw_count")) == 0
                    ],
                },
            }
        )
    aliased_gpu_produced_count = _as_int(
        latest_render_state.get("aliased_gpu_produced_texture_draw_count")
    )
    offscreen_replay_count = _as_int(
        latest_render_state.get("offscreen_render_target_replay_draw_count")
    )
    if aliased_gpu_produced_count > offscreen_replay_count:
        findings.append(
            {
                "id": "aliased_gpu_target_not_replayed",
                "severity": "critical",
                "category": "rendering",
                "evidence": {
                    "aliased_gpu_produced_draw_count": (
                        aliased_gpu_produced_count
                    ),
                    "offscreen_render_target_replay_draw_count": (
                        offscreen_replay_count
                    ),
                    "draws": render_state_coverage.get(
                        "aliased_gpu_produced_draws", []
                    ),
                },
            }
        )
    fixed_fallback_count = _as_int(
        latest_render_state.get("fixed_function_raw_fallback_draw_count")
    )
    fixed_filtered_count = _as_int(
        latest_render_state.get("fixed_function_filtered_draw_count")
    )
    fixed_invalid_vertex_count = _as_int(
        latest_render_state.get("fixed_function_invalid_vertex_count")
    )
    if fixed_fallback_count or fixed_filtered_count or fixed_invalid_vertex_count:
        findings.append(
            {
                "id": "fixed_function_draws_not_host_transformed",
                "severity": "critical",
                "category": "rendering",
                "evidence": {
                    "fixed_function_draw_count": latest_render_state.get(
                        "fixed_function_draw_count"
                    ),
                    "fixed_function_transformed_draw_count": (
                        latest_render_state.get(
                            "fixed_function_transformed_draw_count"
                        )
                    ),
                    "raw_fallback_draw_count": fixed_fallback_count,
                    "filtered_draw_count": fixed_filtered_count,
                    "invalid_vertex_count": fixed_invalid_vertex_count,
                    "mismatching_draws": render_state_coverage.get(
                        "mismatching_draws", []
                    ),
                },
            }
        )
    composition_issue_ids = {
        str(issue.get("id")) for issue in composition_coverage.get("issues", [])
    }
    if "dominant_black_or_near_solid_readback" in composition_issue_ids:
        findings.append(
            {
                "id": "dominant_black_or_near_solid_readback",
                "severity": "critical",
                "category": "rendering",
                "evidence": {
                    "dominant_readback_pixel_ratio": composition_coverage.get(
                        "dominant_readback_pixel_ratio"
                    )
                },
            }
        )
    dirty_sync_time_ratio = guest_performance.get("dirty_page_sync_time_ratio")
    if dirty_sync_time_ratio is not None and dirty_sync_time_ratio >= 0.10:
        findings.append(
            {
                "id": "dirty_page_sync_hot_path",
                "severity": "high",
                "category": "performance",
                "evidence": {
                    "dirty_page_sync_time_ratio": dirty_sync_time_ratio,
                    "dirty_sync_no_work_ratio": guest_performance.get(
                        "dirty_sync_no_work_ratio"
                    ),
                    "dirty_sync_call_count": guest_performance["counter_totals"].get(
                        "dirty_sync_call_count"
                    ),
                    "dirty_page_writeback_count": guest_performance[
                        "counter_totals"
                    ].get("dirty_page_writeback_count"),
                },
            }
        )
    page_fills_per_million = guest_performance.get(
        "page_cache_fills_per_million_steps"
    )
    if page_fills_per_million is not None and page_fills_per_million >= 1_000:
        findings.append(
            {
                "id": "high_native_page_cache_refill_rate",
                "severity": "high",
                "category": "performance",
                "evidence": {
                    "page_cache_fills_per_million_steps": page_fills_per_million,
                    "page_cache_fill_count": guest_performance["counter_totals"].get(
                        "page_cache_fill_count"
                    ),
                },
            }
        )
    read_callbacks_per_million = guest_performance.get(
        "memory_read_callbacks_per_million_steps"
    )
    if read_callbacks_per_million is not None and read_callbacks_per_million >= 10_000:
        findings.append(
            {
                "id": "high_native_memory_read_callback_rate",
                "severity": "high",
                "category": "performance",
                "evidence": {
                    "memory_read_callbacks_per_million_steps": read_callbacks_per_million,
                    "exact_read_callback_ratio": guest_performance.get(
                        "exact_read_callback_ratio"
                    ),
                },
            }
        )
    reload_busy_ratio = presenter_performance.get("reload_busy_ratio")
    if reload_busy_ratio is not None and reload_busy_ratio >= 0.10:
        findings.append(
            {
                "id": "presenter_reload_hot_path",
                "severity": "high",
                "category": "performance",
                "evidence": {
                    "reload_busy_ratio": reload_busy_ratio,
                    "reload_busy_seconds": presenter_performance.get(
                        "reload_busy_seconds"
                    ),
                    "redundant_same_flip_reload_count": presenter_performance.get(
                        "redundant_same_flip_reload_count"
                    ),
                    "skipped_redundant_reload_count": presenter_performance.get(
                        "skipped_redundant_reload_count"
                    ),
                    "top_stage": (
                        presenter_performance.get("hot_paths_by_total_time") or [None]
                    )[0],
                },
            }
        )
    severity_order = {"critical": 0, "high": 1, "warning": 2, "info": 3}
    findings.sort(
        key=lambda finding: (
            severity_order.get(str(finding.get("severity")), 99),
            str(finding.get("category")),
            str(finding.get("id")),
        )
    )
    for priority, finding in enumerate(findings, start=1):
        finding["priority"] = priority
    return findings


def _nearest_host_geometry(
    sample: dict[str, Any],
    events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not events:
        return None
    sample_flips = sample.get("guest_flip_count")
    sample_writes = sample.get("render_write_count")

    def distance(event: dict[str, Any]) -> tuple[int, int]:
        event_flips = _event_guest_flip(event)
        event_writes = int(event.get("source_commands", 0))
        flip_distance = (
            abs(event_flips - int(sample_flips)) if sample_flips is not None else 0
        )
        write_distance = (
            abs(event_writes - int(sample_writes)) if sample_writes is not None else 0
        )
        return flip_distance, write_distance

    event = min(events, key=distance)
    return {
        "guest_vertex_invocation": sample.get("invocation_count"),
        "guest_caller_return_address_hex": sample.get("caller_return_address_hex"),
        "guest_flip_count": sample_flips,
        "guest_render_write_count": sample_writes,
        "host_reload": event.get("reload"),
        "host_guest_flips": event.get("guest_flips"),
        "host_manifest_guest_flip_count": event.get("manifest_guest_flip_count"),
        "host_source_commands": event.get("source_commands"),
        **{field: event.get(field) for field in GEOMETRY_COUNT_FIELDS},
    }


def _rank_sample_code_addresses(
    samples: list[dict[str, Any]],
    *,
    collection_field: str,
    address_field: str,
    offset_field: str,
) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    offsets: dict[str, Counter[str]] = {}
    for sample in samples:
        for item in sample.get(collection_field, []):
            address = item.get(address_field)
            if not isinstance(address, str):
                continue
            counts[address] += 1
            offset = item.get(offset_field)
            if isinstance(offset, (str, int)):
                offsets.setdefault(address, Counter())[str(offset)] += 1
    return [
        {
            "address_hex": address,
            "sample_count": count,
            "locations": [
                {"location": location, "sample_count": location_count}
                for location, location_count in offsets.get(address, Counter()).most_common()
            ],
        }
        for address, count in counts.most_common()
    ]


def _static_guest_code_context(
    xbe_path: Path | None,
    addresses: list[int],
) -> dict[str, Any]:
    if xbe_path is None:
        return {"status": "not_requested", "contexts": []}
    if not xbe_path.is_file():
        return {
            "status": "xbe_missing",
            "path": str(xbe_path),
            "contexts": [],
        }
    try:
        from capstone import CS_ARCH_X86, CS_MODE_32, Cs
        from tools.loader.xbe_loader import load_xbe_file

        loaded = load_xbe_file(xbe_path)
        disassembler = Cs(CS_ARCH_X86, CS_MODE_32)
        contexts: list[dict[str, Any]] = []
        for address in addresses[:32]:
            preceding: dict[str, Any] | None = None
            for distance in (5, 6, 2, 7, 3, 4, 1, 8):
                start = address - distance
                try:
                    instructions = list(
                        disassembler.disasm(loaded.arena.read(start, distance), start)
                    )
                except Exception:
                    continue
                if (
                    len(instructions) != 1
                    or instructions[0].address + instructions[0].size != address
                ):
                    continue
                candidate = instructions[0]
                if candidate.mnemonic not in {"call", "jmp"}:
                    continue
                preceding = {
                    "address_hex": f"0x{candidate.address:08X}",
                    "mnemonic": candidate.mnemonic,
                    "operands": candidate.op_str,
                }
                break
            try:
                following = [
                    {
                        "address_hex": f"0x{instruction.address:08X}",
                        "mnemonic": instruction.mnemonic,
                        "operands": instruction.op_str,
                    }
                    for instruction in list(
                        disassembler.disasm(loaded.arena.read(address, 48), address)
                    )[:8]
                ]
            except Exception:
                following = []
            contexts.append(
                {
                    "return_address_hex": f"0x{address:08X}",
                    "preceding_control_transfer": preceding,
                    "following_instructions": following,
                }
            )
        return {
            "status": "decoded",
            "path": str(xbe_path),
            "contexts": contexts,
        }
    except Exception as exc:
        return {
            "status": "decode_failed",
            "path": str(xbe_path),
            "error": str(exc),
            "contexts": [],
        }


def build_render_debug_report(
    *,
    probe_summary_path: Path,
    presenter_events_path: Path,
    stream_analysis_events_path: Path | None = None,
    xbe_path: Path | None = None,
) -> dict[str, Any]:
    probe_exists = probe_summary_path.is_file()
    probe = (
        json.loads(probe_summary_path.read_text(encoding="utf-8"))
        if probe_exists
        else {}
    )
    execution = probe.get("entry_recovery", {}).get("execution", {})
    render_watchpoint = execution.get("render_watchpoint_stream", {})
    transform_constant_upload_provenance = render_watchpoint.get(
        "transform_constant_upload_provenance",
        {},
    )
    world_matrix_audit = _summarize_world_matrix_audit(execution)
    vertex = execution.get("title_vertex_append_fast_path", {})
    clear_audit = execution.get("title_d3d_primitive_draw_fast_path", {})
    submitted_draw = execution.get("title_immediate_draw_audit", {})
    text_draw = execution.get("title_text_draw_fast_path", {})
    abi = probe.get("runtime_abi_bridge", {})
    retained_events, event_file = _read_presenter_events(presenter_events_path)
    if stream_analysis_events_path is not None:
        analysis_events, analysis_event_file = _read_presenter_events(
            stream_analysis_events_path
        )
        for event_name, events in analysis_events.items():
            retained_events[event_name].extend(events)
    else:
        analysis_event_file = {
            "path": None,
            "exists": False,
            "event_count": 0,
            "invalid_line_count": 0,
            "event_counts": {},
        }
    geometry_events = retained_events["nv2a_presented_geometry_anomalies"]
    host_geometry = _summarize_host_geometry(geometry_events)
    vertex_transforms = _summarize_vertex_transforms(
        retained_events["nv2a_vertex_transform_diagnostics"],
        retained_events["nv2a_presented_transform_diagnostics"],
        retained_events["nv2a_presented_transform_constant"],
    )
    render_state_coverage = _summarize_render_state_diagnostics(
        retained_events["nv2a_render_state_diagnostics"],
        retained_events["nv2a_presented_transform_diagnostics"],
        retained_events["nv2a_fixed_function_vertex_diagnostics"],
    )
    raw_attribute_events = retained_events[
        "nv2a_gpu_raw_attribute_validation"
    ]
    raw_attribute_validation = (
        raw_attribute_events[-1]
        if raw_attribute_events
        else {"status": "not_captured", "passed": None}
    )
    dirty_range_events = retained_events[
        "nv2a_gpu_raw_dirty_range_validation"
    ]
    dirty_range_validation = (
        dirty_range_events[-1]
        if dirty_range_events
        else {"status": "not_captured", "passed": None}
    )
    samples = vertex.get("anomalous_samples", [])
    builder_geometry_by_flip = vertex.get("geometry_by_guest_flip", [])
    guest_geometry_by_flip = submitted_draw.get("geometry_by_guest_flip", [])
    correlations = [
        correlation
        for sample in samples
        if (correlation := _nearest_host_geometry(sample, geometry_events)) is not None
    ]
    producer_callers = vertex.get("producer_callers", [])
    upstream_producers = vertex.get("upstream_producers", [])
    ranked_source_producers = upstream_producers or producer_callers
    ranked_producers = [
        {
            "caller_return_address_hex": producer.get(
                "upstream_return_address_hex",
                producer.get("caller_return_address_hex"),
            ),
            "invocation_count": producer.get("invocation_count"),
            "anomaly_count": producer.get("anomaly_count"),
            "anomaly_counts": producer.get("anomaly_counts", {}),
            "geometry_bounds": {
                "min_x": producer.get("min_x"),
                "max_x": producer.get("max_x"),
                "min_y": producer.get("min_y"),
                "max_y": producer.get("max_y"),
            },
        }
        for producer in ranked_source_producers
        if int(producer.get("anomaly_count", 0)) > 0
    ]
    ranked_stack_candidates = _rank_sample_code_addresses(
        samples,
        collection_field="stack_code_candidates",
        address_field="address_hex",
        offset_field="stack_offset_hex",
    )
    ranked_frame_returns = _rank_sample_code_addresses(
        samples,
        collection_field="frame_chain",
        address_field="return_address_hex",
        offset_field="depth",
    )
    context_address_text = [
        item.get("caller_return_address_hex") for item in ranked_producers
    ] + [
        item.get("upstream_return_address_hex") for item in upstream_producers
    ] + [
        item.get("address_hex")
        for item in [*ranked_stack_candidates, *ranked_frame_returns]
    ] + [
        item.get("guest_return_address_hex")
        for item in abi.get("failed_caller_counts", [])
    ] + [
        item.get("parent_return_address_hex")
        for item in text_draw.get("frontend_renderer_parent_return_counts", [])
    ] + [
        item.get("caller_return_address_hex")
        for item in text_draw.get("caller_return_counts", [])
    ] + [
        item.get("caller_return_address_hex")
        for item in clear_audit.get("caller_return_counts", [])
    ] + [
        item.get("caller_return_address_hex")
        for item in submitted_draw.get("caller_return_counts", [])
    ] + [
        item.get("instruction_address_hex")
        for item in transform_constant_upload_provenance.get(
            "near_zero_producer_instruction_counts",
            [],
        )
    ] + [
        item.get("caller_return_address_hex")
        for item in world_matrix_audit.get("source_records", [])
    ]
    context_addresses: list[int] = []
    for address_text in context_address_text:
        try:
            address = int(str(address_text), 0)
        except (TypeError, ValueError):
            continue
        if address not in context_addresses:
            context_addresses.append(address)
    static_guest_code = _static_guest_code_context(xbe_path, context_addresses)
    last_host_geometry_by_flip = {
        _event_guest_flip(event): event for event in geometry_events
    }
    guest_host_flip_timeline = []
    for guest_flip in guest_geometry_by_flip:
        flip_count = int(guest_flip.get("guest_flip_count", 0))
        host_event = last_host_geometry_by_flip.get(
            flip_count + 1,
            last_host_geometry_by_flip.get(flip_count),
        )
        guest_host_flip_timeline.append(
            {
                **guest_flip,
                "expected_presented_guest_flip_count": flip_count + 1,
                "host_geometry": {
                    "reload": host_event.get("reload"),
                    "source_commands": host_event.get("source_commands"),
                    **{
                        field: host_event.get(field)
                        for field in GEOMETRY_COUNT_FIELDS
                    },
                }
                if host_event is not None
                else None,
            }
        )
    guest_performance = _summarize_guest_performance(execution)
    profile_capture_window = _profile_capture_window(
        retained_events["hot_path_profile_capture"]
    )

    def performance_events(name: str) -> list[dict[str, Any]]:
        return _events_in_profile_capture_window(
            retained_events[name],
            profile_capture_window,
        )

    presenter_performance = _summarize_presenter_performance(
        performance_events("live_render_stream_reloaded"),
        performance_events("frame_presented"),
        performance_events("live_render_stream_reload_skipped"),
        performance_events("nv2a_raw_vertex_buffers_refreshed"),
        performance_events("nv2a_texture_resources_refreshed"),
        performance_events("nv2a_graphics_pipeline_created"),
        performance_events("nv2a_gpu_texture_conversion_pipeline_created"),
        performance_events("nv2a_gpu_texture_conversion_batch"),
        performance_events("nv2a_gpu_texture_conversion_validation"),
        profile_capture_window=profile_capture_window,
    )
    composition_coverage = _summarize_composition_coverage(
        submitted_geometry_by_flip=guest_geometry_by_flip,
        submitted_draw=submitted_draw,
        clear_audit=clear_audit,
        builder_geometry_by_flip=builder_geometry_by_flip,
        text_draw=text_draw,
        retained_events=retained_events,
    )
    live_capture_coverage = _summarize_live_capture_coverage(retained_events)
    render_target_feedback_cache = _summarize_render_target_feedback_cache(
        retained_events
    )
    diagnostic_findings = _rank_diagnostic_findings(
        vertex_transforms=vertex_transforms,
        render_state_coverage=render_state_coverage,
        transform_constant_upload_provenance=(
            transform_constant_upload_provenance
        ),
        world_matrix_audit=world_matrix_audit,
        guest_performance=guest_performance,
        presenter_performance=presenter_performance,
        composition_coverage=composition_coverage,
        live_capture_coverage=live_capture_coverage,
        render_target_feedback_cache=render_target_feedback_cache,
    )
    if raw_attribute_validation.get("passed") is False:
        diagnostic_findings.insert(
            0,
            {
                "id": "gpu_raw_attribute_validation_failed",
                "severity": "critical",
                "category": "rendering",
                "evidence": {
                    "mismatch_vertices": raw_attribute_validation.get(
                        "mismatch_vertices"
                    ),
                    "compared_vertices": raw_attribute_validation.get(
                        "compared_vertices"
                    ),
                    "mapping_fallback_draws": raw_attribute_validation.get(
                        "mapping_fallback_draws"
                    ),
                },
            },
        )
    if dirty_range_validation.get("passed") is False:
        diagnostic_findings.insert(
            0,
            {
                "id": "gpu_raw_dirty_range_validation_failed",
                "severity": "critical",
                "category": "rendering",
                "evidence": {
                    "unchanged_generation_passed": dirty_range_validation.get(
                        "unchanged_generation_passed"
                    ),
                    "changed_generation_passed": dirty_range_validation.get(
                        "changed_generation_passed"
                    ),
                    "layout_change_passed": dirty_range_validation.get(
                        "layout_change_passed"
                    ),
                },
            },
        )
    texture_conversion_validation = (
        retained_events["nv2a_gpu_texture_conversion_validation"][-1]
        if retained_events["nv2a_gpu_texture_conversion_validation"]
        else {}
    )
    if texture_conversion_validation.get("passed") is False:
        diagnostic_findings.insert(
            0,
            {
                "id": "gpu_texture_conversion_validation_failed",
                "severity": "critical",
                "category": "rendering",
                "evidence": texture_conversion_validation,
            },
        )
    status = (
        "correlated_guest_and_host_geometry"
        if guest_geometry_by_flip and geometry_events
        else "guest_provenance_only"
        if guest_geometry_by_flip or samples
        else "host_geometry_only"
        if geometry_events
        else "render_stream_analysis_only"
        if vertex_transforms["event_count"] or render_state_coverage["event_count"]
        else "diagnostic_data_missing"
    )
    return {
        "format": "b2-recomp-render-debug-report-v17",
        "status": status,
        "probe_summary": {
            "path": str(probe_summary_path),
            "exists": probe_exists,
        },
        "presenter_events": event_file,
        "stream_analysis_events": analysis_event_file,
        "diagnostic_findings": diagnostic_findings,
        "guest_vertex_provenance": {
            "invocation_count": vertex.get("invocation_count", 0),
            "geometry_bounds": vertex.get("geometry_bounds"),
            "anomaly_counts": vertex.get("anomaly_counts", {}),
            "producer_callers": producer_callers,
            "upstream_producers": upstream_producers,
            "geometry_by_guest_flip": builder_geometry_by_flip,
            "anomalous_samples": samples,
        },
        "guest_transform_constant_provenance": (
            transform_constant_upload_provenance
        ),
        "guest_world_matrix_provenance": world_matrix_audit,
        "guest_draw_provenance": {
            "execution_mode": submitted_draw.get("execution_mode"),
            "invocation_count": submitted_draw.get("invocation_count", 0),
            "invalid_submission_count": submitted_draw.get(
                "invalid_submission_count", 0
            ),
            "caller_return_counts": submitted_draw.get("caller_return_counts", []),
            "sampled_invocations": submitted_draw.get("sampled_invocations", []),
            "geometry_by_guest_flip": guest_geometry_by_flip,
        },
        "guest_clear_provenance": {
            "execution_mode": clear_audit.get("execution_mode"),
            "invocation_count": clear_audit.get("invocation_count", 0),
            "caller_return_counts": clear_audit.get("caller_return_counts", []),
            "sampled_invocations": clear_audit.get("sampled_invocations", []),
        },
        "guest_text_provenance": {
            "invocation_count": text_draw.get("invocation_count", 0),
            "caller_return_counts": text_draw.get("caller_return_counts", []),
            "frontend_renderer_parent_return_counts": text_draw.get(
                "frontend_renderer_parent_return_counts", []
            ),
            "source_text_counts": text_draw.get("source_text_counts", []),
            "sampled_strings": text_draw.get("sampled_strings", []),
        },
        "runtime_abi_provenance": {
            "invocation_count": abi.get("invocation_count", 0),
            "failed_caller_counts": abi.get("failed_caller_counts", []),
        },
        "performance": {
            "accuracy_policy": "instrumentation_only_no_guest_semantic_shortcuts",
            "guest": guest_performance,
            "presenter": presenter_performance,
        },
        "composition_coverage": composition_coverage,
        "live_capture_coverage": live_capture_coverage,
        "render_target_feedback_cache": render_target_feedback_cache,
        "host_geometry": host_geometry,
        "vertex_transforms": vertex_transforms,
        "gpu_raw_attribute_validation": raw_attribute_validation,
        "gpu_raw_dirty_range_validation": dirty_range_validation,
        "gpu_texture_conversion_validation": texture_conversion_validation,
        "render_state_coverage": render_state_coverage,
        "guest_host_correlations": correlations,
        "guest_host_flip_timeline": guest_host_flip_timeline,
        "ranked_malformed_vertex_producers": ranked_producers,
        "ranked_upstream_vertex_producers": upstream_producers,
        "ranked_upstream_stack_code_candidates": ranked_stack_candidates,
        "ranked_frame_chain_returns": ranked_frame_returns,
        "static_guest_code": static_guest_code,
    }


def write_render_debug_report(
    report: dict[str, Any],
    output_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Correlate malformed guest vertices with host-presented draws."
    )
    parser.add_argument("--probe-summary", type=Path, default=DEFAULT_PROBE_SUMMARY)
    parser.add_argument("--presenter-events", type=Path, default=DEFAULT_PRESENTER_EVENTS)
    parser.add_argument(
        "--stream-analysis-events",
        type=Path,
        default=DEFAULT_STREAM_ANALYSIS_EVENTS,
    )
    parser.add_argument("--xbe", type=Path, default=DEFAULT_XBE)
    parser.add_argument("--json-output", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    report = build_render_debug_report(
        probe_summary_path=args.probe_summary,
        presenter_events_path=args.presenter_events,
        stream_analysis_events_path=args.stream_analysis_events,
        xbe_path=args.xbe,
    )
    write_render_debug_report(report, args.json_output)
    console_summary = {
        "status": report["status"],
        "json_output": str(args.json_output),
        "guest_vertex_invocation_count": report["guest_vertex_provenance"][
            "invocation_count"
        ],
        "guest_vertex_anomaly_counts": report["guest_vertex_provenance"][
            "anomaly_counts"
        ],
        "host_geometry_event_count": report["host_geometry"][
            "geometry_event_count"
        ],
        "host_peak_counts": report["host_geometry"]["peak_counts"],
        "vertex_transform_status": report["vertex_transforms"]["status"],
        "render_state_status": report["render_state_coverage"]["status"],
        "diagnostic_findings": report["diagnostic_findings"][:8],
        "ranked_malformed_vertex_producers": report[
            "ranked_malformed_vertex_producers"
        ][:8],
        "failed_runtime_callers": report["runtime_abi_provenance"][
            "failed_caller_counts"
        ][:8],
    }
    print(
        json.dumps(
            console_summary,
            indent=2 if args.pretty else None,
            sort_keys=True,
        )
    )
    return 0 if report["status"] != "diagnostic_data_missing" else 1


if __name__ == "__main__":
    raise SystemExit(main())
