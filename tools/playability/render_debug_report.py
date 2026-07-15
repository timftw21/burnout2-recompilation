#!/usr/bin/env python3
"""Correlate guest vertex provenance with host NV2A geometry diagnostics."""

from __future__ import annotations

import argparse
import json
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
    "frame_presented",
    "nv2a_native_resources_created",
    "render_validation",
    "d3d8_stream_interpreted",
    "frame_readback_captured",
)

RELOAD_STAGE_FIELDS = (
    "wait_us",
    "command_load_us",
    "interpret_us",
    "resource_update_us",
    "command_record_us",
    "total_us",
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
    return {
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
        "repeated_manifest_flips": repeated_manifest_flips[:32],
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
    return {
        "native_run_count": len(native_runs),
        "native_runs": native_runs,
        "handler_hot_paths": handler_hot_paths,
        "live_host_bridge": live_bridges[-1]["performance"] if live_bridges else None,
        "live_host_bridges": live_bridges,
    }


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
    xbe_path: Path | None = None,
) -> dict[str, Any]:
    probe_exists = probe_summary_path.is_file()
    probe = (
        json.loads(probe_summary_path.read_text(encoding="utf-8"))
        if probe_exists
        else {}
    )
    execution = probe.get("entry_recovery", {}).get("execution", {})
    vertex = execution.get("title_vertex_append_fast_path", {})
    clear_audit = execution.get("title_d3d_primitive_draw_fast_path", {})
    submitted_draw = execution.get("title_immediate_draw_audit", {})
    text_draw = execution.get("title_text_draw_fast_path", {})
    abi = probe.get("runtime_abi_bridge", {})
    retained_events, event_file = _read_presenter_events(presenter_events_path)
    geometry_events = retained_events["nv2a_presented_geometry_anomalies"]
    host_geometry = _summarize_host_geometry(geometry_events)
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
    status = (
        "correlated_guest_and_host_geometry"
        if guest_geometry_by_flip and geometry_events
        else "guest_provenance_only"
        if guest_geometry_by_flip or samples
        else "host_geometry_only"
        if geometry_events
        else "diagnostic_data_missing"
    )
    return {
        "format": "b2-recomp-render-debug-report-v3",
        "status": status,
        "probe_summary": {
            "path": str(probe_summary_path),
            "exists": probe_exists,
        },
        "presenter_events": event_file,
        "guest_vertex_provenance": {
            "invocation_count": vertex.get("invocation_count", 0),
            "geometry_bounds": vertex.get("geometry_bounds"),
            "anomaly_counts": vertex.get("anomaly_counts", {}),
            "producer_callers": producer_callers,
            "upstream_producers": upstream_producers,
            "geometry_by_guest_flip": builder_geometry_by_flip,
            "anomalous_samples": samples,
        },
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
            "guest": _summarize_guest_performance(execution),
            "presenter": _summarize_presenter_performance(
                retained_events["live_render_stream_reloaded"],
                retained_events["frame_presented"],
            ),
        },
        "composition_coverage": _summarize_composition_coverage(
            submitted_geometry_by_flip=guest_geometry_by_flip,
            submitted_draw=submitted_draw,
            clear_audit=clear_audit,
            builder_geometry_by_flip=builder_geometry_by_flip,
            text_draw=text_draw,
            retained_events=retained_events,
        ),
        "host_geometry": host_geometry,
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
    parser.add_argument("--xbe", type=Path, default=DEFAULT_XBE)
    parser.add_argument("--json-output", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    report = build_render_debug_report(
        probe_summary_path=args.probe_summary,
        presenter_events_path=args.presenter_events,
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
