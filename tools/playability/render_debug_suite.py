#!/usr/bin/env python3
"""Run frozen NV2A analysis and build one correlated render/performance report."""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.host.first_frame_smoke import (
    DEFAULT_EXE,
    DEFAULT_SOURCE,
    compile_first_frame,
    discover_toolchain,
    run_first_frame,
    summarize_smoke,
)
from tools.playability.render_debug_report import (
    DEFAULT_PRESENTER_EVENTS,
    DEFAULT_PROBE_SUMMARY,
    DEFAULT_REPORT,
    DEFAULT_STREAM_ANALYSIS_EVENTS,
    DEFAULT_XBE,
    build_render_debug_report,
    write_render_debug_report,
)

DEFAULT_RENDER_MANIFEST = REPO_ROOT / "reports" / "local" / "live" / "render.json"
DEFAULT_ANALYSIS_SUMMARY = (
    REPO_ROOT
    / "reports"
    / "local"
    / "playability"
    / "render-stream-analysis-summary.json"
)

APPEND_ONLY_COMMAND_MAGIC = b"B2APPND1"
APPEND_ONLY_COMMAND_HEADER_SIZE = 8
APPEND_ONLY_COMMAND_RECORD_SIZE = 16
SPAN_COMMAND_MAGIC = b"B2SPAN01"
SPAN_COMMAND_HEADER_SIZE = 16


def latest_retained_render_manifest(presenter_events: Path) -> Path | None:
    if not presenter_events.is_file():
        return None
    latest: Path | None = None
    with presenter_events.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if event.get("event") != "hotkey_render_capture_retained":
                continue
            manifest_text = event.get("manifest")
            if not isinstance(manifest_text, str) or not manifest_text:
                continue
            candidate = Path(manifest_text)
            if not candidate.is_absolute():
                candidate = REPO_ROOT / candidate
            if candidate.is_file():
                latest = candidate
    return latest


def inspect_render_snapshot_integrity(render_manifest: Path) -> dict[str, Any]:
    """Compare a retained command file with its completed-flip boundary."""
    base: dict[str, Any] = {
        "status": "not_applicable",
        "accepted": True,
        "render_manifest": str(render_manifest),
        "command_snapshot_path": None,
        "manifest_record_count": None,
        "snapshot_record_count": None,
        "trailing_record_count": 0,
        "missing_record_count": 0,
        "exact_prefix": None,
        "command_snapshot_base_record_count": 0,
        "standalone_replay_state_complete": None,
        "interpreter_bootstrap_path": None,
        "interpreter_bootstrap_status": "not_required",
    }
    try:
        manifest = json.loads(render_manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {
            **base,
            "status": "invalid_manifest",
            "accepted": False,
            "error": str(exc),
        }
    if not isinstance(manifest, dict):
        return {
            **base,
            "status": "invalid_manifest",
            "accepted": False,
            "error": "manifest root is not an object",
        }
    declared_count = manifest.get("command_snapshot_record_count")
    command_path_text = manifest.get("command_snapshot_path")
    if declared_count is None and command_path_text is None:
        return base
    try:
        manifest_record_count = int(declared_count)
    except (TypeError, ValueError, OverflowError):
        return {
            **base,
            "status": "invalid_manifest_record_count",
            "accepted": False,
            "error": "command_snapshot_record_count is not an integer",
        }
    if manifest_record_count < 0:
        return {
            **base,
            "status": "invalid_manifest_record_count",
            "accepted": False,
            "manifest_record_count": manifest_record_count,
            "error": "command_snapshot_record_count is negative",
        }
    try:
        command_base_record_count = int(
            manifest.get("command_snapshot_base_record_count", 0)
        )
    except (TypeError, ValueError, OverflowError):
        command_base_record_count = -1
    standalone_state_complete = manifest.get(
        "standalone_replay_state_complete"
    )
    interpreter_bootstrap_text = manifest.get("interpreter_bootstrap_path")
    if not isinstance(command_path_text, str) or not command_path_text:
        return {
            **base,
            "status": "missing_snapshot_path",
            "accepted": False,
            "manifest_record_count": manifest_record_count,
        }
    command_path = Path(command_path_text)
    if not command_path.is_absolute():
        local_candidate = render_manifest.parent / command_path
        repo_candidate = REPO_ROOT / command_path
        command_path = (
            local_candidate if local_candidate.is_file() else repo_candidate
        )
    result = {
        **base,
        "command_snapshot_path": str(command_path),
        "manifest_record_count": manifest_record_count,
        "declared_exact_prefix": manifest.get("command_snapshot_exact_prefix"),
        "declared_source_record_count": manifest.get(
            "command_snapshot_source_record_count"
        ),
        "declared_trimmed_record_count": manifest.get(
            "command_snapshot_trimmed_record_count"
        ),
        "command_snapshot_base_record_count": command_base_record_count,
        "standalone_replay_state_complete": standalone_state_complete,
    }
    try:
        snapshot_size = command_path.stat().st_size
        with command_path.open("rb") as handle:
            magic = handle.read(APPEND_ONLY_COMMAND_HEADER_SIZE)
            snapshot_transport: str | None = None
            snapshot_record_count: int | None = None
            if magic == APPEND_ONLY_COMMAND_MAGIC:
                snapshot_transport = "packed_direct"
                payload_size = snapshot_size - APPEND_ONLY_COMMAND_HEADER_SIZE
                if payload_size >= 0 and not (
                    payload_size % APPEND_ONLY_COMMAND_RECORD_SIZE
                ):
                    snapshot_record_count = (
                        payload_size // APPEND_ONLY_COMMAND_RECORD_SIZE
                    )
            elif magic == SPAN_COMMAND_MAGIC:
                snapshot_transport = "bulk_span_v1"
                logical_write_count = 0
                while True:
                    span_header = handle.read(SPAN_COMMAND_HEADER_SIZE)
                    if not span_header:
                        snapshot_record_count = logical_write_count
                        break
                    if len(span_header) != SPAN_COMMAND_HEADER_SIZE:
                        break
                    (
                        kind,
                        flags,
                        reserved,
                        _address,
                        span_payload_size,
                        span_write_count,
                    ) = struct.unpack("<BBHIII", span_header)
                    if (
                        kind > 1
                        or flags & ~1
                        or reserved != 0
                        or span_payload_size <= 0
                        or span_write_count <= 0
                    ):
                        break
                    if len(handle.read(span_payload_size)) != span_payload_size:
                        break
                    logical_write_count += span_write_count
    except OSError as exc:
        return {
            **result,
            "status": "missing_snapshot",
            "accepted": False,
            "error": str(exc),
        }
    if snapshot_transport is None:
        return {
            **result,
            "status": "invalid_snapshot_header",
            "accepted": False,
            "snapshot_byte_count": snapshot_size,
        }
    if snapshot_record_count is None:
        return {
            **result,
            "status": "invalid_snapshot_size",
            "accepted": False,
            "snapshot_byte_count": snapshot_size,
        }
    trailing_record_count = max(0, snapshot_record_count - manifest_record_count)
    missing_record_count = max(0, manifest_record_count - snapshot_record_count)
    if trailing_record_count:
        status = "contains_unpresented_records"
    elif missing_record_count:
        status = "snapshot_short"
    else:
        status = "exact"
    interpreter_bootstrap_path: Path | None = None
    interpreter_bootstrap_status = "not_required"
    interpreter_bootstrap_method_count: int | None = None
    if isinstance(interpreter_bootstrap_text, str) and interpreter_bootstrap_text:
        interpreter_bootstrap_path = Path(interpreter_bootstrap_text)
        if not interpreter_bootstrap_path.is_absolute():
            local_candidate = render_manifest.parent / interpreter_bootstrap_path
            repo_candidate = REPO_ROOT / interpreter_bootstrap_path
            interpreter_bootstrap_path = (
                local_candidate
                if local_candidate.is_file()
                else repo_candidate
            )
        try:
            with interpreter_bootstrap_path.open("rb") as handle:
                bootstrap_header = handle.read(8)
                bootstrap_count_bytes = handle.read(4)
            if bootstrap_header != b"B2NVST01" or len(bootstrap_count_bytes) != 4:
                interpreter_bootstrap_status = "invalid"
            else:
                interpreter_bootstrap_method_count = int.from_bytes(
                    bootstrap_count_bytes,
                    "little",
                )
                expected_bootstrap_size = 12 + interpreter_bootstrap_method_count * 8
                interpreter_bootstrap_status = (
                    "valid"
                    if interpreter_bootstrap_path.stat().st_size
                    == expected_bootstrap_size
                    else "invalid"
                )
        except OSError:
            interpreter_bootstrap_status = "missing"
    elif command_base_record_count > 0:
        interpreter_bootstrap_status = "missing"
    if status == "exact" and command_base_record_count < 0:
        status = "invalid_manifest_record_count"
    elif status == "exact" and command_base_record_count > 0:
        if standalone_state_complete is not True:
            status = "incomplete_state_history"
        elif interpreter_bootstrap_status != "valid":
            status = "invalid_interpreter_bootstrap"
    return {
        **result,
        "status": status,
        "accepted": status == "exact",
        "snapshot_byte_count": snapshot_size,
        "snapshot_transport": snapshot_transport,
        "snapshot_record_count": snapshot_record_count,
        "trailing_record_count": trailing_record_count,
        "missing_record_count": missing_record_count,
        "exact_prefix": trailing_record_count == 0 and missing_record_count == 0,
        "interpreter_bootstrap_path": (
            str(interpreter_bootstrap_path)
            if interpreter_bootstrap_path is not None
            else None
        ),
        "interpreter_bootstrap_status": interpreter_bootstrap_status,
        "interpreter_bootstrap_method_count": interpreter_bootstrap_method_count,
    }


def render_snapshot_integrity_finding(
    integrity: dict[str, Any],
) -> dict[str, Any] | None:
    status = integrity["status"]
    if integrity["accepted"]:
        return None
    finding_id = {
        "contains_unpresented_records": (
            "retained_command_snapshot_contains_unpresented_records"
        ),
        "snapshot_short": "retained_command_snapshot_is_short",
        "incomplete_state_history": (
            "retained_command_snapshot_missing_interpreter_state"
        ),
        "invalid_interpreter_bootstrap": (
            "retained_command_snapshot_has_invalid_interpreter_state"
        ),
    }.get(status, "retained_command_snapshot_is_invalid")
    return {
        "priority": 1,
        "severity": "critical",
        "category": "capture_integrity",
        "id": finding_id,
    }


def run_render_debug_suite(
    *,
    render_manifest: Path,
    probe_summary: Path = DEFAULT_PROBE_SUMMARY,
    presenter_events: Path = DEFAULT_PRESENTER_EVENTS,
    analysis_events: Path = DEFAULT_STREAM_ANALYSIS_EVENTS,
    analysis_summary: Path = DEFAULT_ANALYSIS_SUMMARY,
    report_output: Path = DEFAULT_REPORT,
    xbe_path: Path | None = DEFAULT_XBE,
    executable: Path = DEFAULT_EXE,
    source: Path = DEFAULT_SOURCE,
    skip_build: bool = False,
    timeout_seconds: int = 30,
    width: int = 640,
    height: int = 480,
    clangxx: Path | None = None,
    vulkan_sdk: Path | None = None,
) -> dict[str, Any]:
    if not render_manifest.is_file():
        raise FileNotFoundError(f"live render manifest is missing: {render_manifest}")
    snapshot_integrity = inspect_render_snapshot_integrity(render_manifest)
    compile_result = None
    if not skip_build:
        toolchain = discover_toolchain(clangxx=clangxx, vulkan_sdk=vulkan_sdk)
        compile_result = compile_first_frame(
            source=source,
            output=executable,
            toolchain=toolchain,
        )
    run_result = run_first_frame(
        executable=executable,
        debug_json=analysis_events,
        width=width,
        height=height,
        max_frames=0,
        inject_input=False,
        timeout_seconds=timeout_seconds,
        render_stream_json=render_manifest,
        screenshot=None,
        hotkey_screenshot_directory=None,
        analyze_render_stream=True,
    )
    smoke_summary = summarize_smoke(
        compile_result=compile_result,
        run_result=run_result,
        summary_output=analysis_summary,
    )
    if run_result["returncode"] != 0:
        raise RuntimeError(
            "headless render-stream analysis failed with exit code "
            f"{run_result['returncode']}: {run_result.get('stderr') or 'no stderr'}"
        )
    report = build_render_debug_report(
        probe_summary_path=probe_summary,
        presenter_events_path=presenter_events,
        stream_analysis_events_path=analysis_events,
        xbe_path=xbe_path,
    )
    write_render_debug_report(report, report_output)
    guest_performance = report["performance"]["guest"]
    presenter_performance = report["performance"]["presenter"]
    vertex_transforms = report["vertex_transforms"]
    render_state_coverage = report["render_state_coverage"]
    live_capture_coverage = report["live_capture_coverage"]
    render_target_feedback_cache = report["render_target_feedback_cache"]
    render_state = render_state_coverage.get("latest_event") or {}
    transform_provenance = report["guest_transform_constant_provenance"]
    world_matrix_provenance = report["guest_world_matrix_provenance"]
    guest_counters = guest_performance.get("counter_totals", {})
    integrity_finding = render_snapshot_integrity_finding(snapshot_integrity)
    diagnostic_findings = []
    if integrity_finding is not None:
        diagnostic_findings.append(integrity_finding)
    for finding in report["diagnostic_findings"]:
        compact_finding = {
            key: finding.get(key)
            for key in ("priority", "severity", "category", "id")
        }
        if integrity_finding is not None:
            compact_finding["priority"] = int(
                compact_finding.get("priority") or 0
            ) + 1
        diagnostic_findings.append(compact_finding)
    return {
        "format": "b2-recomp-render-debug-suite-v11",
        "render_manifest": str(render_manifest),
        "render_snapshot_integrity": snapshot_integrity,
        "analysis_events": str(analysis_events),
        "analysis_summary": str(analysis_summary),
        "report_output": str(report_output),
        "analysis_completed": smoke_summary["run"][
            "render_stream_analysis_completed"
        ],
        "vertex_transform_status": vertex_transforms["status"],
        "vertex_transforms": {
            "latest_draw_event_count": vertex_transforms.get(
                "latest_draw_event_count"
            ),
            "near_zero_position_c96_c99_basis_draw_count": (
                vertex_transforms.get(
                    "near_zero_position_c96_c99_basis_draw_count"
                )
            ),
        },
        "render_state_coverage": {
            "status": render_state_coverage.get("status"),
            "state_history_complete": render_state_coverage.get(
                "state_history_complete"
            ),
            "clear_surface_method_count": render_state_coverage.get(
                "clear_surface_method_count", 0
            ),
            "last_clear_surface_method_count": render_state_coverage.get(
                "last_clear_surface_method_count", 0
            ),
            "presented_surface_clear_count": render_state_coverage.get(
                "presented_surface_clear_count", 0
            ),
            "presented_color_clear_count": render_state_coverage.get(
                "presented_color_clear_count", 0
            ),
            "latest_presented_surface_clear_address": (
                render_state_coverage.get(
                    "latest_presented_surface_clear_address"
                )
            ),
            "latest_presented_surface_clear_flags": (
                render_state_coverage.get(
                    "latest_presented_surface_clear_flags"
                )
            ),
            "latest_presented_surface_clear_color_argb": (
                render_state_coverage.get(
                    "latest_presented_surface_clear_color_argb"
                )
            ),
            "latest_presented_surface_clear_draw_index": (
                render_state_coverage.get(
                    "latest_presented_surface_clear_draw_index"
                )
            ),
            "presented_draw_begin": render_state_coverage.get(
                "presented_draw_begin", 0
            ),
            "presented_draw_count": render_state_coverage.get(
                "presented_draw_count", 0
            ),
            "retained_presented_surface_available": (
                render_state_coverage.get(
                    "retained_presented_surface_available"
                )
            ),
            "textured_presented_draw_count": render_state.get(
                "textured_presented_draw_count", 0
            ),
            "out_of_unit_texture_coordinate_draw_count": render_state.get(
                "out_of_unit_texture_coordinate_draw_count", 0
            ),
            "repeat_address_draw_count": render_state.get(
                "repeat_address_draw_count", 0
            ),
            "sampler_address_mismatch_draw_count": render_state.get(
                "sampler_address_mismatch_draw_count", 0
            ),
            "projective_texture_draw_count": render_state.get(
                "projective_texture_draw_count", 0
            ),
            "alpha_blend_enabled_draw_count": render_state.get(
                "alpha_blend_enabled_draw_count", 0
            ),
            "alpha_test_enabled_draw_count": render_state.get(
                "alpha_test_enabled_draw_count", 0
            ),
            "alpha_test_applied_draw_count": render_state.get(
                "alpha_test_applied_draw_count", 0
            ),
            "alpha_test_state_mismatch_draw_count": render_state.get(
                "alpha_test_state_mismatch_draw_count", 0
            ),
            "texture_alpha_kill_enabled_draw_count": render_state.get(
                "texture_alpha_kill_enabled_draw_count", 0
            ),
            "texture_alpha_kill_applied_draw_count": render_state.get(
                "texture_alpha_kill_applied_draw_count", 0
            ),
            "opaque_x8_texture_draw_count": render_state.get(
                "opaque_x8_texture_draw_count", 0
            ),
            "opaque_x8_alpha_forced_draw_count": render_state.get(
                "opaque_x8_alpha_forced_draw_count", 0
            ),
            "uncompressed_swizzled_texture_draw_count": render_state.get(
                "uncompressed_swizzled_texture_draw_count", 0
            ),
            "uncompressed_texture_unswizzled_draw_count": render_state.get(
                "uncompressed_texture_unswizzled_draw_count", 0
            ),
            "register_combiner_programmed_draw_count": render_state.get(
                "register_combiner_programmed_draw_count", 0
            ),
            "register_combiner_applied_draw_count": render_state.get(
                "register_combiner_applied_draw_count", 0
            ),
            "fixed_function_texture_combiner_recovered_draw_count": (
                render_state.get(
                    "fixed_function_texture_combiner_recovered_draw_count",
                    0,
                )
            ),
            "register_combiner_state_mismatch_draw_count": render_state.get(
                "register_combiner_state_mismatch_draw_count", 0
            ),
            "fog_enabled_draw_count": render_state.get(
                "fog_enabled_draw_count", 0
            ),
            "fog_factor_applied_draw_count": render_state.get(
                "fog_factor_applied_draw_count", 0
            ),
            "fog_state_mismatch_draw_count": render_state.get(
                "fog_state_mismatch_draw_count", 0
            ),
            "multiple_texture_stage_draw_count": render_state.get(
                "multiple_texture_stage_draw_count", 0
            ),
            "texture_stage_coverage_mismatch_draw_count": render_state.get(
                "texture_stage_coverage_mismatch_draw_count", 0
            ),
            "cubemap_texture_stage_draw_count": render_state.get(
                "cubemap_texture_stage_draw_count", 0
            ),
            "cubemap_texture_stage_coverage_mismatch_draw_count": (
                render_state.get(
                    "cubemap_texture_stage_coverage_mismatch_draw_count", 0
                )
            ),
            "zero_payload_textured_draw_count": render_state.get(
                "zero_payload_textured_draw_count", 0
            ),
            "missing_texture_resource_draw_count": render_state_coverage.get(
                "missing_texture_resource_draw_count", 0
            ),
            "unproduced_zero_payload_texture_draw_count": render_state.get(
                "unproduced_zero_payload_texture_draw_count", 0
            ),
            "aliased_gpu_produced_texture_draw_count": render_state.get(
                "aliased_gpu_produced_texture_draw_count", 0
            ),
            "offscreen_render_target_replay_draw_count": render_state.get(
                "offscreen_render_target_replay_draw_count", 0
            ),
            "fixed_function_draw_count": render_state.get(
                "fixed_function_draw_count", 0
            ),
            "fixed_function_transformed_draw_count": render_state.get(
                "fixed_function_transformed_draw_count", 0
            ),
            "fixed_function_filtered_draw_count": render_state.get(
                "fixed_function_filtered_draw_count", 0
            ),
        },
        "live_capture_coverage": {
            "status": live_capture_coverage.get("status"),
            "f12_readback_count": live_capture_coverage.get(
                "f12_readback_count", 0
            ),
            "retained_capture_count": live_capture_coverage.get(
                "retained_capture_count", 0
            ),
            "unretained_f12_readback_count": live_capture_coverage.get(
                "unretained_f12_readback_count", 0
            ),
            "offscreen_depth_isolated": live_capture_coverage.get(
                "offscreen_depth_isolated"
            ),
            "retained_manifests": live_capture_coverage.get(
                "retained_manifests", []
            ),
        },
        "render_target_feedback_cache": {
            "status": render_target_feedback_cache.get("status"),
            "active_count": render_target_feedback_cache.get("active_count"),
            "required_count": render_target_feedback_cache.get(
                "required_count"
            ),
            "missing_count": render_target_feedback_cache.get("missing_count"),
            "stale_count": render_target_feedback_cache.get("stale_count"),
            "pruned_count": render_target_feedback_cache.get("pruned_count"),
            "mismatch_event_count": render_target_feedback_cache.get(
                "mismatch_event_count", 0
            ),
            "required_addresses": render_target_feedback_cache.get(
                "required_addresses", []
            ),
            "active_addresses": render_target_feedback_cache.get(
                "active_addresses", []
            ),
        },
        "guest_transform_constant_provenance": {
            "status": transform_provenance.get("status", "not_captured"),
            "matrix_upload_count": transform_provenance.get(
                "matrix_upload_count",
                0,
            ),
            "near_zero_basis_matrix_upload_count": transform_provenance.get(
                "near_zero_basis_matrix_upload_count",
                0,
            ),
            "near_zero_producer_instruction_counts": transform_provenance.get(
                "near_zero_producer_instruction_counts",
                [],
            ),
        },
        "guest_world_matrix_provenance": {
            "enabled": world_matrix_provenance.get("enabled", False),
            "invocation_count": world_matrix_provenance.get(
                "invocation_count", 0
            ),
            "singular_matrix_count": world_matrix_provenance.get(
                "singular_matrix_count", 0
            ),
            "source_records": world_matrix_provenance.get(
                "source_records", []
            )[:16],
            "write_audit": world_matrix_provenance.get("write_audit", {}),
        },
        "diagnostic_finding_count": len(diagnostic_findings),
        "diagnostic_findings": diagnostic_findings,
        "guest_performance": {
            "effective_steps_per_second": guest_performance.get(
                "effective_steps_per_second"
            ),
            "profiled_native_run_count": guest_performance.get(
                "profiled_native_run_count"
            ),
            "native_module_calls_per_million_steps": guest_performance.get(
                "native_module_calls_per_million_steps"
            ),
            "page_cache_fills_per_million_steps": guest_performance.get(
                "page_cache_fills_per_million_steps"
            ),
            "memory_read_callbacks_per_million_steps": guest_performance.get(
                "memory_read_callbacks_per_million_steps"
            ),
            "read_callback_sampling": guest_performance.get(
                "read_callback_sampling"
            ),
            "memory_callback_sampling": guest_performance.get(
                "memory_callback_sampling"
            ),
            "native_dispatch_hot_targets": guest_performance.get(
                "native_dispatch_hot_targets", []
            )[:16],
            "dirty_sync_no_work_ratio": guest_performance.get(
                "dirty_sync_no_work_ratio"
            ),
            "dirty_page_sync_time_ratio": guest_performance.get(
                "dirty_page_sync_time_ratio"
            ),
            "selective_dirty_sync_call_count": guest_counters.get(
                "selective_dirty_sync_call_count"
            ),
            "selective_dirty_sync_page_writeback_count": guest_counters.get(
                "selective_dirty_sync_page_writeback_count"
            ),
            "observer_drain_deferred_count": guest_counters.get(
                "observer_drain_deferred_count"
            ),
            "average_observed_writes_per_batch": guest_performance.get(
                "average_observed_writes_per_batch"
            ),
            "memory_callback_policy_cache_hit_ratio": guest_performance.get(
                "memory_callback_policy_cache_hit_ratio"
            ),
        },
        "presenter_performance": {
            "reload_busy_ratio": presenter_performance.get("reload_busy_ratio"),
            "offscreen_target_lifecycle": presenter_performance.get(
                "offscreen_target_lifecycle", {}
            ),
            "redundant_same_flip_reload_count": presenter_performance.get(
                "redundant_same_flip_reload_count"
            ),
            "skipped_redundant_reload_count": presenter_performance.get(
                "skipped_redundant_reload_count"
            ),
            "gpu_texture_conversion": presenter_performance.get(
                "gpu_texture_conversion", {}
            ),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Analyze the frozen live NV2A stream and correlate it with guest and "
            "presenter performance evidence."
        )
    )
    parser.add_argument("--render-manifest", type=Path)
    parser.add_argument("--probe-summary", type=Path, default=DEFAULT_PROBE_SUMMARY)
    parser.add_argument("--presenter-events", type=Path, default=DEFAULT_PRESENTER_EVENTS)
    parser.add_argument("--analysis-events", type=Path, default=DEFAULT_STREAM_ANALYSIS_EVENTS)
    parser.add_argument("--analysis-summary", type=Path, default=DEFAULT_ANALYSIS_SUMMARY)
    parser.add_argument("--report-output", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--xbe", type=Path, default=DEFAULT_XBE)
    parser.add_argument("--exe", type=Path, default=DEFAULT_EXE)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--clangxx", type=Path)
    parser.add_argument("--vulkan-sdk", type=Path)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    render_manifest_source = "explicit"
    render_manifest = args.render_manifest
    if render_manifest is None:
        render_manifest = latest_retained_render_manifest(args.presenter_events)
        if render_manifest is not None:
            render_manifest_source = "latest_retained_f12"
        else:
            render_manifest = DEFAULT_RENDER_MANIFEST
            render_manifest_source = "live_manifest_fallback"
    result = run_render_debug_suite(
        render_manifest=render_manifest,
        probe_summary=args.probe_summary,
        presenter_events=args.presenter_events,
        analysis_events=args.analysis_events,
        analysis_summary=args.analysis_summary,
        report_output=args.report_output,
        xbe_path=args.xbe,
        executable=args.exe,
        source=args.source,
        skip_build=args.skip_build,
        timeout_seconds=args.timeout_seconds,
        width=args.width,
        height=args.height,
        clangxx=args.clangxx,
        vulkan_sdk=args.vulkan_sdk,
    )
    result["render_manifest_source"] = render_manifest_source
    print(json.dumps(result, indent=2 if args.pretty else None, sort_keys=True))
    return 0 if result["render_snapshot_integrity"]["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
