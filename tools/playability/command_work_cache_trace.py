#!/usr/bin/env python3
"""Simulate command-work cache capacities from an opt-in presenter trace."""

from __future__ import annotations

import argparse
import json
import math
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable

TRACE_SCHEMA_VERSION = 1
DEFAULT_WINDOW_KIB = (4, 8, 16, 32)
DEFAULT_PLAN_LIMITS = (256, 512, 1024)
DEFAULT_BYTE_LIMIT = 64 * 1024 * 1024
COMMAND_SPAN_DESCRIPTOR_BYTES = 20
WORD_LAYOUT_BYTES = 12


class CommandWorkCacheTraceError(RuntimeError):
    """Raised when a trace cannot support a trustworthy simulation."""


def _mix_hash(value: int, word: int) -> int:
    for shift in range(0, 32, 8):
        value ^= (word >> shift) & 0xFF
        value = (value * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return value


def layout_id(layout: tuple[tuple[int, int], ...]) -> str:
    value = 1469598103934665603
    for address, size in layout:
        for word in (1, 0, address, 0, size, 0):
            value = _mix_hash(value, word)
    return f"{value:016x}"


def _vector_capacity(size: int) -> int:
    """Model the 1.5x MSVC STL growth used by the supported Windows host."""
    capacity = 0
    for required in range(1, size + 1):
        if required <= capacity:
            continue
        geometric = capacity + capacity // 2
        capacity = max(required, geometric)
    return capacity


def _plan_word_count(descriptors: tuple[tuple[int, int], ...]) -> int:
    pending: list[tuple[int, int]] = []
    emitted = 0
    pending_max_address = 0

    def covered_word_count(intervals: list[tuple[int, int]]) -> int:
        if not intervals:
            return 0
        total = 0
        ordered = sorted(intervals)
        merged_begin, merged_end = ordered[0]
        for begin, end in ordered[1:]:
            if begin > merged_end:
                total += (merged_end - merged_begin) // 4
                merged_begin, merged_end = begin, end
            else:
                merged_end = max(merged_end, end)
        return total + (merged_end - merged_begin) // 4

    for address, size in descriptors:
        if pending and address + 0x1000 < pending_max_address:
            emitted += covered_word_count(pending)
            pending.clear()
            pending_max_address = 0
        pending.append((address, address + size))
        pending_max_address = max(pending_max_address, address + size)
    return emitted + covered_word_count(pending)


def _plan_bytes(layout: tuple[tuple[int, int], ...]) -> int:
    word_count = _plan_word_count(layout)
    return (
        len(layout) * COMMAND_SPAN_DESCRIPTOR_BYTES
        + _vector_capacity(word_count) * WORD_LAYOUT_BYTES
    )


def partition_segment(
    segment: list[dict[str, Any]], window_bytes: int
) -> list[tuple[tuple[int, int], ...]]:
    descriptors = [
        (int(item["address"]), int(item["size"])) for item in segment
    ]
    if not descriptors:
        return []
    for address, size in descriptors:
        if address < 0 or size <= 0 or address % 4 or size % 4:
            raise CommandWorkCacheTraceError(
                "trace contains an unaligned or empty reconstruction descriptor"
            )
    aperture_begin = min(address for address, _ in descriptors)
    aperture_end = max(address + size for address, size in descriptors)
    layouts: list[tuple[tuple[int, int], ...]] = []
    window_begin = aperture_begin
    while window_begin < aperture_end:
        window_end = min(aperture_end, window_begin + window_bytes)
        fragments: list[tuple[int, int]] = []
        for address, size in descriptors:
            overlap_begin = max(address, window_begin)
            overlap_end = min(address + size, window_end)
            if overlap_begin < overlap_end:
                fragments.append((overlap_begin, overlap_end - overlap_begin))
        if fragments:
            anchor = fragments[0][0]
            layouts.append(tuple((address - anchor, size) for address, size in fragments))
        window_begin = window_end
    return layouts


def read_trace(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    header: dict[str, Any] | None = None
    reloads: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as trace_file:
        for line_number, line in enumerate(trace_file, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CommandWorkCacheTraceError(
                    f"invalid JSON on trace line {line_number}: {exc}"
                ) from exc
            if event.get("schema_version") != TRACE_SCHEMA_VERSION:
                raise CommandWorkCacheTraceError(
                    f"unsupported trace schema on line {line_number}"
                )
            if event.get("event") == "command_work_cache_trace_start":
                header = event
            elif event.get("event") == "command_work_cache_reload":
                reloads.append(event)
    if header is None:
        raise CommandWorkCacheTraceError("trace header is missing")
    if not reloads:
        raise CommandWorkCacheTraceError("trace contains no cache reloads")
    return header, reloads


def profile_capture_reloads(
    reloads: list[dict[str, Any]],
    presenter_events_path: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    states = {event.get("profile_capture_state") for event in reloads}
    if "active" in states:
        selected = [
            event
            for event in reloads
            if event.get("profile_capture_state") == "active"
        ]
        return selected, {
            "source": "trace_profile_capture_state",
            "reload_count": len(selected),
        }
    if presenter_events_path is None:
        raise CommandWorkCacheTraceError(
            "trace has no embedded F10 capture state; provide --presenter-events "
            "for this legacy trace or use --full-session"
        )

    active_sequence: int | None = None
    complete_sequence: int | None = None
    presenter_reloads: list[dict[str, Any]] = []
    with presenter_events_path.open("r", encoding="utf-8") as events_file:
        for line_number, line in enumerate(events_file, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CommandWorkCacheTraceError(
                    f"invalid presenter event JSON on line {line_number}: {exc}"
                ) from exc
            if event.get("event") == "hot_path_profile_capture":
                if event.get("state") == "active":
                    active_sequence = int(event.get("sequence", -1))
                elif event.get("state") == "complete" and active_sequence is not None:
                    complete_sequence = int(event.get("sequence", -1))
            elif event.get("event") == "live_render_stream_reloaded":
                presenter_reloads.append(event)
    if active_sequence is None:
        raise CommandWorkCacheTraceError(
            "presenter events contain no active F10 capture marker"
        )
    selected_presenter_reloads = [
        event
        for event in presenter_reloads
        if int(event.get("sequence", -1)) > active_sequence
        and (
            complete_sequence is None
            or int(event.get("sequence", -1)) < complete_sequence
        )
    ]
    offset = len(reloads) - len(presenter_reloads)
    if offset not in (0, 1):
        raise CommandWorkCacheTraceError(
            "cannot align cache trace reloads with presenter reload events"
        )
    selected_numbers = {
        int(event.get("reload", -1)) + offset
        for event in selected_presenter_reloads
    }
    selected = [
        event for event in reloads if int(event.get("reload", -1)) in selected_numbers
    ]
    if len(selected) != len(selected_presenter_reloads):
        raise CommandWorkCacheTraceError(
            "cache trace and presenter capture reloads did not align exactly"
        )
    return selected, {
        "source": "presenter_event_sequence",
        "presenter_events": str(presenter_events_path.resolve()),
        "trace_reload_offset": offset,
        "start_sequence": active_sequence,
        "end_sequence": complete_sequence,
        "reload_count": len(selected),
    }


def _percentile(values: Iterable[int], percentile: float) -> int | None:
    ordered = sorted(values)
    if not ordered:
        return None
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def captured_trace_summary(reloads: list[dict[str, Any]]) -> dict[str, Any]:
    windows = [
        window
        for reload_event in reloads
        for window in reload_event.get("windows", [])
    ]
    plan_bytes = [int(window.get("plan_bytes", 0)) for window in windows]
    reuse_distances = [
        int(window["reuse_distance"])
        for window in windows
        if window.get("reuse_distance") is not None
    ]
    evictions = [
        eviction
        for window in windows
        for eviction in window.get("evictions", [])
    ]
    unused_lookups = [int(item.get("unused_lookups", 0)) for item in evictions]
    hits = sum(bool(window.get("hit")) for window in windows)
    return {
        "reload_count": len(reloads),
        "window_lookup_count": len(windows),
        "hit_count": hits,
        "hit_ratio": round(hits / len(windows), 6) if windows else None,
        "eviction_count": len(evictions),
        "plan_bytes": {
            "p50": _percentile(plan_bytes, 0.50),
            "p95": _percentile(plan_bytes, 0.95),
            "maximum": max(plan_bytes, default=None),
        },
        "reuse_distance": {
            "sample_count": len(reuse_distances),
            "p50": _percentile(reuse_distances, 0.50),
            "p95": _percentile(reuse_distances, 0.95),
            "maximum": max(reuse_distances, default=None),
        },
        "eviction_unused_lookups": {
            "sample_count": len(unused_lookups),
            "p50": _percentile(unused_lookups, 0.50),
            "p95": _percentile(unused_lookups, 0.95),
            "maximum": max(unused_lookups, default=None),
        },
    }


def validate_runtime_model(
    header: dict[str, Any], reloads: list[dict[str, Any]]
) -> dict[str, Any]:
    runtime_window_bytes = int(header.get("runtime_window_bytes", 0))
    if runtime_window_bytes <= 0:
        raise CommandWorkCacheTraceError("trace runtime window size is invalid")
    checked_windows = 0
    plan_byte_mismatches = 0
    maximum_plan_byte_delta = 0
    for reload_event in reloads:
        layouts = [
            layout
            for segment in reload_event.get("segments", [])
            for layout in partition_segment(segment, runtime_window_bytes)
        ]
        recorded = reload_event.get("windows", [])
        recorded_ids = [str(window.get("layout_id", "")) for window in recorded]
        modeled_ids = [layout_id(layout) for layout in layouts]
        if recorded_ids != modeled_ids:
            raise CommandWorkCacheTraceError(
                "runtime layout IDs do not match reconstructed segments at "
                f"reload {reload_event.get('reload')}"
            )
        for layout, window in zip(layouts, recorded):
            delta = abs(_plan_bytes(layout) - int(window.get("plan_bytes", 0)))
            checked_windows += 1
            if delta:
                plan_byte_mismatches += 1
                maximum_plan_byte_delta = max(maximum_plan_byte_delta, delta)
    return {
        "checked_window_count": checked_windows,
        "layout_ids_match": True,
        "plan_byte_mismatch_count": plan_byte_mismatches,
        "maximum_plan_byte_delta": maximum_plan_byte_delta,
    }


def simulate_configuration(
    reloads: list[dict[str, Any]],
    *,
    window_bytes: int,
    plan_limit: int,
    byte_limit: int = DEFAULT_BYTE_LIMIT,
    measured_reload_numbers: set[int] | None = None,
) -> dict[str, Any]:
    cache: OrderedDict[tuple[str, tuple[tuple[int, int], ...]], int] = OrderedDict()
    resident_bytes = 0
    peak_resident_bytes = 0
    peak_resident_plans = 0
    lookup_count = 0
    hit_count = 0
    build_count = 0
    eviction_count = 0
    unretained_count = 0
    epoch_change_count = 0
    current_epoch: str | None = None

    for reload_event in reloads:
        measured = (
            measured_reload_numbers is None
            or int(reload_event.get("reload", -1)) in measured_reload_numbers
        )
        epoch = str(reload_event.get("epoch", ""))
        if current_epoch is None:
            current_epoch = epoch
        elif epoch != current_epoch:
            cache.clear()
            resident_bytes = 0
            current_epoch = epoch
            if measured:
                epoch_change_count += 1
        for segment in reload_event.get("segments", []):
            for layout in partition_segment(segment, window_bytes):
                if measured:
                    lookup_count += 1
                key = (layout_id(layout), layout)
                if key in cache:
                    if measured:
                        hit_count += 1
                    cache.move_to_end(key)
                    continue
                if measured:
                    build_count += 1
                plan_bytes = _plan_bytes(layout)
                if plan_bytes > byte_limit:
                    if measured:
                        unretained_count += 1
                    continue
                while cache and (
                    len(cache) >= plan_limit
                    or resident_bytes + plan_bytes > byte_limit
                ):
                    _, evicted_bytes = cache.popitem(last=False)
                    resident_bytes -= evicted_bytes
                    if measured:
                        eviction_count += 1
                cache[key] = plan_bytes
                resident_bytes += plan_bytes
                if measured:
                    peak_resident_bytes = max(peak_resident_bytes, resident_bytes)
                    peak_resident_plans = max(peak_resident_plans, len(cache))

    return {
        "window_kib": window_bytes // 1024,
        "plan_limit": plan_limit,
        "byte_limit": byte_limit,
        "lookup_count": lookup_count,
        "hit_count": hit_count,
        "hit_ratio": round(hit_count / lookup_count, 6) if lookup_count else None,
        "build_count": build_count,
        "eviction_count": eviction_count,
        "unretained_plan_count": unretained_count,
        "epoch_change_count": epoch_change_count,
        "peak_resident_plan_count": peak_resident_plans,
        "peak_resident_bytes": peak_resident_bytes,
    }


def build_report(
    trace_path: Path,
    *,
    window_kib: Iterable[int] = DEFAULT_WINDOW_KIB,
    plan_limits: Iterable[int] = DEFAULT_PLAN_LIMITS,
    byte_limit: int = DEFAULT_BYTE_LIMIT,
    profile_capture_only: bool = False,
    presenter_events_path: Path | None = None,
) -> dict[str, Any]:
    header, all_reloads = read_trace(trace_path)
    if profile_capture_only:
        reloads, scope = profile_capture_reloads(
            all_reloads, presenter_events_path
        )
    else:
        reloads = all_reloads
        scope = {"source": "full_session", "reload_count": len(reloads)}
    measured_reload_numbers = {
        int(event.get("reload", -1)) for event in reloads
    }
    runtime_validation = validate_runtime_model(header, reloads)
    windows = tuple(window_kib)
    limits = tuple(plan_limits)
    if not windows or any(value <= 0 for value in windows):
        raise CommandWorkCacheTraceError("window sizes must be positive")
    if not limits or any(value <= 0 for value in limits):
        raise CommandWorkCacheTraceError("plan limits must be positive")
    if byte_limit <= 0:
        raise CommandWorkCacheTraceError("byte limit must be positive")
    simulations = [
        simulate_configuration(
            all_reloads,
            window_bytes=size * 1024,
            plan_limit=limit,
            byte_limit=byte_limit,
            measured_reload_numbers=measured_reload_numbers,
        )
        for size in windows
        for limit in limits
    ]
    return {
        "format": "b2-command-work-cache-simulation",
        "schema_version": 1,
        "trace": str(trace_path.resolve()),
        "scope": scope,
        "runtime_configuration": {
            "window_bytes": header.get("runtime_window_bytes"),
            "plan_limit": header.get("runtime_plan_limit"),
            "byte_limit": header.get("runtime_byte_limit"),
        },
        "captured": captured_trace_summary(reloads),
        "runtime_model_validation": runtime_validation,
        "simulation_model": {
            "policy": "collision-safe exact-layout LRU",
            "plan_bytes": "MSVC vector-capacity model for descriptor and word layouts",
            "payload_values_in_key": False,
        },
        "simulations": simulations,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Simulate command-work cache window and plan capacities."
    )
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument(
        "--presenter-events",
        type=Path,
        help="Presenter JSONL used to scope a legacy trace to its F10 window.",
    )
    parser.add_argument(
        "--full-session",
        action="store_true",
        help="Simulate every traced reload instead of only the F10 capture window.",
    )
    parser.add_argument(
        "--window-kib",
        nargs="+",
        type=int,
        default=DEFAULT_WINDOW_KIB,
    )
    parser.add_argument(
        "--plan-limits",
        nargs="+",
        type=int,
        default=DEFAULT_PLAN_LIMITS,
    )
    parser.add_argument("--byte-limit-mib", type=int, default=64)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    try:
        report = build_report(
            args.trace,
            window_kib=args.window_kib,
            plan_limits=args.plan_limits,
            byte_limit=args.byte_limit_mib * 1024 * 1024,
            profile_capture_only=not args.full_session,
            presenter_events_path=args.presenter_events,
        )
    except (OSError, CommandWorkCacheTraceError) as exc:
        parser.error(str(exc))
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(
        json.dumps(report, indent=2 if args.pretty else None, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    print(f"Command-work cache simulation: {args.json_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
