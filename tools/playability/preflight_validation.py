#!/usr/bin/env python3
"""Run automated asset, render, replay, frame, and guest-health gates before manual testing."""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.host.first_frame_smoke import (
    DEFAULT_EXE,
    compile_first_frame,
    compile_shaders,
    discover_toolchain,
    run_first_frame,
    summarize_smoke,
)
from tools.render.d3d8_stream import (
    STREAM_FORMAT,
    decode_render_stream,
    normalize_render_stream,
)


PREFLIGHT_FORMAT = "b2-recomp-pre-manual-preflight"
SUITE_FORMAT = "b2-recomp-preflight-replay-suite"
SUPPORTED_TEXTURE_FORMATS = {
    "DXT1",
    "DXT5",
    "A8R8G8B8",
    "X8R8G8B8",
    "A8R8G8B8_LINEAR",
    "X8R8G8B8_LINEAR",
}
SUPPORTED_PRIMITIVES = {"triangle_strip", "quad_list"}
SUPPORTED_DRAW_SOURCES = {"inline_array"}
TEXTURE_FORMAT_NAMES = {
    0x05: "R5G6B5",
    0x06: "A8R8G8B8",
    0x07: "X8R8G8B8",
    0x0C: "DXT1",
    0x0E: "DXT3",
    0x0F: "DXT5",
    0x12: "A8R8G8B8_LINEAR",
    0x1E: "X8R8G8B8_LINEAR",
}


class PreflightError(RuntimeError):
    """Raised when a preflight input is malformed or cannot be processed."""


@dataclass(frozen=True)
class ReplayStage:
    name: str
    render_stream: Path
    min_native_textures: int = 0
    min_presented_draws: int = 1
    min_unique_colors: int = 16
    max_dominant_fraction: float = 0.98
    required_texture_formats: tuple[str, ...] = ()
    sequence_group: str | None = None
    require_sequence_change: bool = False
    required: bool = True


def _resolve_path(path: str | Path, *, relative_to: Path | None = None) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    if relative_to is not None:
        local = relative_to / candidate
        if local.exists():
            return local
    return REPO_ROOT / candidate


def _texture_payload_size(format_name: str, width: int, height: int) -> int | None:
    blocks = ((width + 3) // 4) * ((height + 3) // 4)
    if format_name == "DXT1":
        return blocks * 8
    if format_name in {"DXT3", "DXT5"}:
        return blocks * 16
    if format_name == "R5G6B5":
        return width * height * 2
    if format_name in {
        "A8R8G8B8",
        "X8R8G8B8",
        "A8R8G8B8_LINEAR",
        "X8R8G8B8_LINEAR",
    }:
        return width * height * 4
    return None


def scan_dic_texture_assets(extracted_root: Path) -> dict[str, Any]:
    """Inventory Criterion dictionary textures without decoding proprietary payloads."""

    entries: list[dict[str, Any]] = []
    malformed: list[dict[str, Any]] = []
    for path in sorted(extracted_root.rglob("*.dic")):
        payload = path.read_bytes()
        cursor = 0
        while True:
            header = payload.find(b"\x00\x05\x00\x00", cursor)
            if header < 0:
                break
            cursor = header + 1
            if header + 20 > len(payload):
                continue
            width, height = struct.unpack_from("<HH", payload, header + 8)
            format_raw = payload[header + 15]
            payload_size = struct.unpack_from("<I", payload, header + 16)[0]
            format_name = TEXTURE_FORMAT_NAMES.get(format_raw)
            if (
                format_name is None
                or width <= 0
                or height <= 0
                or width > 4096
                or height > 4096
                or width & (width - 1)
                or height & (height - 1)
            ):
                continue
            expected_size = _texture_payload_size(format_name, width, height)
            if expected_size is None:
                continue
            name_bytes = payload[max(0, header - 64):header].split(b"\x00", 1)[0]
            name = name_bytes.decode("cp1252", errors="replace")
            record = {
                "dictionary": str(path.relative_to(extracted_root)).replace("\\", "/"),
                "header_offset_hex": f"0x{header:08X}",
                "name": name,
                "format": format_name,
                "width": width,
                "height": height,
                "payload_size": payload_size,
                "expected_base_size": expected_size,
                "supported": format_name in SUPPORTED_TEXTURE_FORMATS,
            }
            if payload_size < expected_size or header + 20 + payload_size > len(payload):
                malformed.append(record)
            else:
                entries.append(record)
    format_counts = Counter(entry["format"] for entry in entries)
    unsupported = [entry for entry in entries if not entry["supported"]]
    implementation_requirements = [
        {
            "area": "texture_decode",
            "feature": format_name,
            "observed_count": count,
            "implemented": format_name in SUPPORTED_TEXTURE_FORMATS,
        }
        for format_name, count in sorted(format_counts.items())
    ]
    return {
        "dictionary_count": len({entry["dictionary"] for entry in entries}),
        "texture_count": len(entries),
        "format_counts": dict(sorted(format_counts.items())),
        "supported_formats": sorted(SUPPORTED_TEXTURE_FORMATS),
        "unsupported_texture_count": len(unsupported),
        "unsupported_textures": unsupported,
        "malformed_texture_count": len(malformed),
        "malformed_textures": malformed,
        "implementation_requirements": implementation_requirements,
        "textures": entries,
        "passed": bool(entries) and not unsupported and not malformed,
    }


def _load_live_binary_stream(manifest_path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    command_path = _resolve_path(
        manifest["command_snapshot_path"], relative_to=manifest_path.parent
    )
    resource_path = _resolve_path(
        manifest["resource_snapshot_path"], relative_to=manifest_path.parent
    )
    command_bytes = command_path.read_bytes()
    if len(command_bytes) < 8 or command_bytes[:8] != b"B2APPND1":
        raise PreflightError(f"invalid live command stream: {command_path}")
    if (len(command_bytes) - 8) % 16:
        raise PreflightError(f"truncated live command stream: {command_path}")
    available_record_count = (len(command_bytes) - 8) // 16
    audit_record_count = manifest.get("audit_command_record_count")
    record_count = (
        int(audit_record_count)
        if audit_record_count is not None
        else available_record_count
    )
    if record_count < 0 or record_count > available_record_count:
        raise PreflightError(
            f"live command stream is short: requested {record_count}, "
            f"available {available_record_count}"
        )
    writes: list[dict[str, Any]] = []
    for sequence, offset in enumerate(range(8, 8 + record_count * 16, 16)):
        kind_raw, size, _reserved, address, raw = struct.unpack_from(
            "<BBHI8s", command_bytes, offset
        )
        if size <= 0 or size > 8:
            raise PreflightError(f"invalid live command size {size} at record {sequence}")
        data = raw[:size]
        writes.append(
            {
                "sequence": sequence,
                "kind": "d3d_mmio" if kind_raw == 0 else "d3d_push_buffer",
                "address": address,
                "value": int.from_bytes(data[:4], "little"),
                "size": size,
                "bytes_hex": data.hex().upper(),
            }
        )
    resource_bytes = resource_path.read_bytes()
    if resource_bytes[:8] == b"B2TEX001":
        if len(resource_bytes) < 12:
            raise PreflightError(f"truncated live texture stream: {resource_path}")
        resource_count = struct.unpack_from("<I", resource_bytes, 8)[0]
        resources: list[dict[str, Any]] = []
        offset = 12
        for resource_index in range(resource_count):
            if offset + 56 > len(resource_bytes):
                raise PreflightError(
                    f"truncated live texture header {resource_index}: {resource_path}"
                )
            (
                stage,
                address,
                width,
                height,
                format_size,
                payload_size,
                content_hash,
            ) = struct.unpack_from("<IIIIII32s", resource_bytes, offset)
            offset += 56
            resource_end = offset + format_size + payload_size
            if format_size <= 0 or format_size > 32 or resource_end > len(resource_bytes):
                raise PreflightError(
                    f"invalid live texture record {resource_index}: {resource_path}"
                )
            try:
                format_name = resource_bytes[offset : offset + format_size].decode(
                    "ascii"
                )
            except UnicodeDecodeError as exc:
                raise PreflightError(
                    f"invalid live texture format {resource_index}: {resource_path}"
                ) from exc
            offset += format_size
            texture_bytes = resource_bytes[offset : offset + payload_size]
            offset += payload_size
            resources.append(
                {
                    "stage": stage,
                    "address": address,
                    "address_hex": f"0x{address:08X}",
                    "format": format_name,
                    "width": width,
                    "height": height,
                    "byte_count": payload_size,
                    "sha256": content_hash.hex().upper(),
                    "bytes_hex": texture_bytes.hex().upper(),
                }
            )
        if offset != len(resource_bytes):
            raise PreflightError(f"trailing live texture data: {resource_path}")
    else:
        resource_payload = json.loads(resource_bytes.decode("utf-8"))
        resources = resource_payload.get("resource_snapshots", [])
    return normalize_render_stream(
        {
            "format": STREAM_FORMAT,
            "write_count": record_count,
            "captured_write_count": len(writes),
            "resource_snapshots": resources,
            "writes": writes,
        }
    )


def load_render_stream(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("format") == "b2-recomp-live-render-manifest":
        return _load_live_binary_stream(path, payload)
    return normalize_render_stream(payload)


def _draw_texture_requirement(draw: dict[str, Any]) -> dict[str, Any] | None:
    state = draw.get("texture_state", {})
    stage = state.get("0", {}) if isinstance(state, dict) else {}
    control = stage.get("texture_control0", {}) if isinstance(stage, dict) else {}
    if not bool(control.get("enabled")):
        return None
    offset = stage.get("texture_offset", {})
    texture_format = stage.get("texture_format", {})
    if not isinstance(offset, dict) or not isinstance(texture_format, dict):
        return {
            "address": None,
            "format": None,
            "width": None,
            "height": None,
        }
    format_name = texture_format.get("color_format_name")
    width = texture_format.get("width")
    height = texture_format.get("height")
    if format_name in {"A8R8G8B8_LINEAR", "X8R8G8B8_LINEAR"}:
        image_rect = stage.get("texture_image_rect", {})
        if isinstance(image_rect, dict):
            rect_width = image_rect.get("width")
            rect_height = image_rect.get("height")
            if isinstance(rect_width, int) and rect_width > 0:
                width = rect_width
            if isinstance(rect_height, int) and rect_height > 0:
                height = rect_height
    return {
        "address": offset.get("raw"),
        "format": format_name,
        "width": width,
        "height": height,
    }


def analyze_render_capabilities(path: Path) -> dict[str, Any]:
    stream = load_render_stream(path)
    decoded = decode_render_stream(stream)
    resources = {
        int(resource["address"]): resource
        for resource in stream.get("resource_snapshots", [])
        if isinstance(resource, dict) and isinstance(resource.get("address"), int)
    }
    format_counts = Counter(str(resource.get("format")) for resource in resources.values())
    unsupported_resources = [
        {
            "address_hex": f"0x{address:08X}",
            "format": resource.get("format"),
            "width": resource.get("width"),
            "height": resource.get("height"),
        }
        for address, resource in resources.items()
        if resource.get("format") not in SUPPORTED_TEXTURE_FORMATS
    ]
    draws = decoded.get("frame_state", {}).get("draw_calls", [])
    primitive_counts = Counter(str(draw.get("primitive")) for draw in draws)
    source_counts = Counter(str(draw.get("source")) for draw in draws)
    unsupported_primitives = sorted(set(primitive_counts) - SUPPORTED_PRIMITIVES)
    unsupported_sources = sorted(set(source_counts) - SUPPORTED_DRAW_SOURCES)
    consistency_failures: list[dict[str, Any]] = []
    textured_draw_count = 0
    matched_textured_draw_count = 0
    for draw_index, draw in enumerate(draws):
        requirement = _draw_texture_requirement(draw)
        if requirement is None:
            continue
        textured_draw_count += 1
        address = requirement["address"]
        resource = resources.get(address) if isinstance(address, int) else None
        reasons: list[str] = []
        if resource is None:
            reasons.append("missing_resource")
        else:
            if resource.get("format") != requirement["format"]:
                reasons.append("format_mismatch")
            if int(resource.get("width", 0)) != int(requirement["width"] or 0):
                reasons.append("width_mismatch")
            if int(resource.get("height", 0)) != int(requirement["height"] or 0):
                reasons.append("height_mismatch")
        if reasons:
            consistency_failures.append(
                {
                    "draw_index": draw_index,
                    "address_hex": f"0x{address:08X}" if isinstance(address, int) else None,
                    "required": requirement,
                    "snapshot": {
                        key: resource.get(key)
                        for key in ("format", "width", "height")
                    } if resource else None,
                    "reasons": reasons,
                }
            )
        else:
            matched_textured_draw_count += 1
    gap_inventory = decoded.get("visual_gap_inventory", {})
    warnings: list[str] = []
    if int(gap_inventory.get("unnamed_method_writes", 0)):
        warnings.append("unnamed_nv2a_methods_observed")
    if int(gap_inventory.get("unknown_push_buffer_packets", 0)):
        warnings.append("unknown_push_buffer_packets_observed")
    if int(gap_inventory.get("truncated_commands", 0)):
        warnings.append("truncated_capture_commands_observed")
    implementation_requirements = [
        *(
            {
                "area": "texture_decode",
                "feature": name,
                "observed_count": count,
                "implemented": name in SUPPORTED_TEXTURE_FORMATS,
            }
            for name, count in sorted(format_counts.items())
        ),
        *(
            {
                "area": "primitive",
                "feature": name,
                "observed_count": count,
                "implemented": name in SUPPORTED_PRIMITIVES,
            }
            for name, count in sorted(primitive_counts.items())
        ),
        *(
            {
                "area": "draw_source",
                "feature": name,
                "observed_count": count,
                "implemented": name in SUPPORTED_DRAW_SOURCES,
            }
            for name, count in sorted(source_counts.items())
        ),
    ]
    passed = not (
        unsupported_resources
        or unsupported_primitives
        or unsupported_sources
        or consistency_failures
    )
    return {
        "path": str(path),
        "write_count": stream["write_count"],
        "captured_write_count": stream["captured_write_count"],
        "resource_count": len(resources),
        "texture_format_counts": dict(sorted(format_counts.items())),
        "unsupported_texture_resources": unsupported_resources,
        "draw_count": len(draws),
        "primitive_counts": dict(sorted(primitive_counts.items())),
        "unsupported_primitives": unsupported_primitives,
        "draw_source_counts": dict(sorted(source_counts.items())),
        "unsupported_draw_sources": unsupported_sources,
        "textured_draw_count": textured_draw_count,
        "matched_textured_draw_count": matched_textured_draw_count,
        "draw_resource_consistency_failure_count": len(consistency_failures),
        "draw_resource_consistency_failures": consistency_failures[:64],
        "visual_gap_inventory": gap_inventory,
        "coverage_warnings": warnings,
        "implementation_requirements": implementation_requirements,
        "passed": passed,
    }


def inspect_frame_health(path: Path) -> dict[str, Any]:
    payload = path.read_bytes()
    if len(payload) < 54 or payload[:2] != b"BM":
        raise PreflightError(f"invalid BMP frame: {path}")
    pixel_offset = struct.unpack_from("<I", payload, 10)[0]
    width, signed_height = struct.unpack_from("<ii", payload, 18)
    bits_per_pixel = struct.unpack_from("<H", payload, 28)[0]
    if width <= 0 or signed_height == 0 or bits_per_pixel != 32:
        raise PreflightError(f"unsupported BMP frame: {path}")
    height = abs(signed_height)
    pixels = payload[pixel_offset:pixel_offset + width * height * 4]
    if len(pixels) != width * height * 4:
        raise PreflightError(f"truncated BMP frame: {path}")
    colors = Counter(
        pixels[offset:offset + 4]
        for offset in range(0, len(pixels), 4)
    )
    total = width * height
    dominant_count = max(colors.values(), default=0)
    bright = 0
    dark = 0
    luminance_sum = 0.0
    for offset in range(0, len(pixels), 4):
        blue, green, red = pixels[offset:offset + 3]
        bright += int(red >= 245 and green >= 245 and blue >= 245)
        dark += int(red <= 10 and green <= 10 and blue <= 10)
        luminance_sum += 0.2126 * red + 0.7152 * green + 0.0722 * blue
    dominant_fraction = dominant_count / total
    bright_fraction = bright / total
    dark_fraction = dark / total
    failure_reasons: list[str] = []
    if dominant_fraction >= 0.98:
        failure_reasons.append("near_solid_frame")
    if bright_fraction >= 0.98:
        failure_reasons.append("whiteout_frame")
    if len(colors) < 16:
        failure_reasons.append("low_information_frame")
    return {
        "path": str(path),
        "width": width,
        "height": height,
        "unique_colors": len(colors),
        "dominant_fraction": round(dominant_fraction, 6),
        "bright_fraction": round(bright_fraction, 6),
        "dark_fraction": round(dark_fraction, 6),
        "mean_luminance": round(luminance_sum / total, 3),
        "pixel_sha256": hashlib.sha256(pixels).hexdigest(),
        "failure_reasons": failure_reasons,
        "passed": not failure_reasons,
    }


def analyze_frame_sequence(
    frames: list[dict[str, Any]], *, require_change: bool = False
) -> dict[str, Any]:
    signatures = [frame.get("pixel_sha256") for frame in frames]
    identical = len(frames) > 1 and len(set(signatures)) == 1
    failure_reasons = ["frame_sequence_did_not_change"] if require_change and identical else []
    return {
        "frame_count": len(frames),
        "distinct_pixel_frames": len(set(signatures)),
        "all_frames_identical": identical,
        "change_required": require_change,
        "whiteout_frame_count": sum(
            "whiteout_frame" in frame.get("failure_reasons", []) for frame in frames
        ),
        "failed_frame_count": sum(not frame.get("passed", False) for frame in frames),
        "failure_reasons": failure_reasons,
        "passed": all(frame.get("passed", False) for frame in frames)
        and not failure_reasons,
    }


def analyze_probe_health(path: Path) -> dict[str, Any]:
    summary = json.loads(path.read_text(encoding="utf-8"))
    execution = summary.get("entry_recovery", {}).get("execution", {})
    threads = execution.get("guest_thread_executions", [])
    steps = max((int(thread.get("steps", 0)) for thread in threads), default=0)
    music = execution.get("title_music_mode_fast_path", {})
    recent_music = music.get("recent_invocations", [])
    current_mode = recent_music[-1].get("mode") if recent_music else None
    live_bridge_present = any(thread.get("live_host_bridge") for thread in threads)
    failures: list[str] = []
    if int(execution.get("dynamic_frontier_count", 0)):
        failures.append("dynamic_frontier_present")
    if steps <= 0:
        failures.append("guest_made_no_progress")
    if int(music.get("decode_error_count", 0)):
        failures.append("music_decode_error")
    if (
        live_bridge_present
        and current_mode == 2
        and int(music.get("submitted_track_count", 0)) < 1
    ):
        failures.append("menu_music_not_submitted")
    return {
        "path": str(path),
        "guest_steps": steps,
        "dynamic_frontier_count": int(execution.get("dynamic_frontier_count", 0)),
        "live_bridge_present": live_bridge_present,
        "music_mode": current_mode,
        "music_invocation_count": int(music.get("invocation_count", 0)),
        "music_decode_error_count": int(music.get("decode_error_count", 0)),
        "music_submission_count": int(music.get("submitted_track_count", 0)),
        "menu_music_loop_contract": True,
        "failure_reasons": failures,
        "passed": not failures,
    }


def load_replay_suite(path: Path) -> list[ReplayStage]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("format") != SUITE_FORMAT:
        raise PreflightError(f"invalid replay suite format: {path}")
    stages: list[ReplayStage] = []
    for item in payload.get("stages", []):
        stage_path = _resolve_path(item["render_stream"], relative_to=path.parent)
        stages.append(
            ReplayStage(
                name=str(item["name"]),
                render_stream=stage_path,
                min_native_textures=int(item.get("min_native_textures", 0)),
                min_presented_draws=int(item.get("min_presented_draws", 1)),
                min_unique_colors=int(item.get("min_unique_colors", 16)),
                max_dominant_fraction=float(item.get("max_dominant_fraction", 0.98)),
                required_texture_formats=tuple(item.get("required_texture_formats", [])),
                sequence_group=item.get("sequence_group"),
                require_sequence_change=bool(item.get("require_sequence_change", False)),
                required=bool(item.get("required", True)),
            )
        )
    return stages


def run_replay_suite(
    stages: list[ReplayStage],
    *,
    output_dir: Path,
    executable: Path = DEFAULT_EXE,
    strict_render_validation: bool = True,
    replay_runner: Callable[..., dict[str, Any]] = run_first_frame,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for stage in stages:
        if not stage.render_stream.is_file():
            results.append(
                {
                    "name": stage.name,
                    "render_stream": str(stage.render_stream),
                    "skipped": not stage.required,
                    "failure_reasons": ["missing_render_stream"] if stage.required else [],
                    "passed": not stage.required,
                }
            )
            continue
        screenshot = output_dir / f"{stage.name}.bmp"
        debug_json = output_dir / f"{stage.name}.events.jsonl"
        run = replay_runner(
            executable=executable,
            debug_json=debug_json,
            max_frames=3,
            inject_input=False,
            timeout_seconds=60,
            render_stream_json=stage.render_stream,
            screenshot=screenshot,
            hotkey_screenshot_directory=None,
            strict_render_validation=strict_render_validation,
        )
        smoke = summarize_smoke(compile_result=None, run_result=run)
        host = smoke["run"]
        frame = inspect_frame_health(screenshot) if screenshot.is_file() else None
        capability = analyze_render_capabilities(stage.render_stream)
        failures: list[str] = []
        if run["returncode"] != 0:
            failures.append("renderer_failed")
        if not host.get("render_validation_passed", False):
            failures.append("strict_render_validation_failed")
        if int(host.get("nv2a_native_textures", 0)) < stage.min_native_textures:
            failures.append("insufficient_native_textures")
        if int(host.get("nv2a_presented_draws", 0)) < stage.min_presented_draws:
            failures.append("insufficient_presented_draws")
        observed_formats = set(capability["texture_format_counts"])
        if not set(stage.required_texture_formats).issubset(observed_formats):
            failures.append("required_texture_format_missing")
        if frame is None:
            failures.append("missing_frame_readback")
        else:
            if frame["unique_colors"] < stage.min_unique_colors:
                failures.append("insufficient_frame_color_detail")
            if frame["dominant_fraction"] > stage.max_dominant_fraction:
                failures.append("dominant_frame_color_exceeded")
            failures.extend(frame["failure_reasons"])
        if not capability["passed"]:
            failures.append("render_capability_gate_failed")
        results.append(
            {
                "name": stage.name,
                "render_stream": str(stage.render_stream),
                "sequence_group": stage.sequence_group,
                "require_sequence_change": stage.require_sequence_change,
                "host": host,
                "frame_health": frame,
                "capability": capability,
                "failure_reasons": sorted(set(failures)),
                "passed": not failures,
            }
        )
    groups: dict[str, list[dict[str, Any]]] = {}
    group_change_requirements: dict[str, bool] = {}
    for result in results:
        group = result.get("sequence_group")
        frame = result.get("frame_health")
        if group and isinstance(frame, dict):
            groups.setdefault(str(group), []).append(frame)
            group_change_requirements[str(group)] = (
                group_change_requirements.get(str(group), False)
                or bool(result.get("require_sequence_change"))
            )
    sequences = {
        name: analyze_frame_sequence(
            frames, require_change=group_change_requirements.get(name, False)
        )
        for name, frames in sorted(groups.items())
    }
    return {
        "stage_count": len(results),
        "passed_stage_count": sum(result.get("passed", False) for result in results),
        "stages": results,
        "sequences": sequences,
        "passed": all(result.get("passed", False) for result in results)
        and all(sequence["passed"] for sequence in sequences.values()),
    }


def build_preflight_report(
    *,
    extracted_root: Path | None = None,
    render_streams: list[Path] | None = None,
    probe_summary: Path | None = None,
    frame_paths: list[Path] | None = None,
    replay_suite: dict[str, Any] | None = None,
) -> dict[str, Any]:
    asset = scan_dic_texture_assets(extracted_root) if extracted_root else None
    render = [analyze_render_capabilities(path) for path in render_streams or []]
    probe = analyze_probe_health(probe_summary) if probe_summary else None
    frames = [inspect_frame_health(path) for path in frame_paths or []]
    frame_sequence = analyze_frame_sequence(frames) if frames else None
    checks = [
        check
        for check in (
            asset,
            *render,
            probe,
            frame_sequence,
            replay_suite,
        )
        if check is not None
    ]
    return {
        "format": PREFLIGHT_FORMAT,
        "public_safe": False,
        "asset_preflight": asset,
        "render_capability_inventory": render,
        "probe_health": probe,
        "frame_health": frames,
        "frame_sequence_health": frame_sequence,
        "replay_suite": replay_suite,
        "check_count": len(checks),
        "failed_check_count": sum(not check.get("passed", False) for check in checks),
        "passed": bool(checks) and all(check.get("passed", False) for check in checks),
    }


def summarize_preflight_report(report: dict[str, Any]) -> dict[str, Any]:
    """Keep stdout scannable while the optional JSON artifact retains full evidence."""

    asset = report.get("asset_preflight")
    renders = report.get("render_capability_inventory", [])
    replay = report.get("replay_suite")
    return {
        "format": report.get("format"),
        "passed": report.get("passed"),
        "check_count": report.get("check_count"),
        "failed_check_count": report.get("failed_check_count"),
        "assets": None if not asset else {
            "passed": asset.get("passed"),
            "dictionary_count": asset.get("dictionary_count"),
            "texture_count": asset.get("texture_count"),
            "format_counts": asset.get("format_counts"),
            "unsupported_texture_count": asset.get("unsupported_texture_count"),
            "malformed_texture_count": asset.get("malformed_texture_count"),
        },
        "render_streams": [
            {
                "path": item.get("path"),
                "passed": item.get("passed"),
                "resource_count": item.get("resource_count"),
                "texture_format_counts": item.get("texture_format_counts"),
                "draw_count": item.get("draw_count"),
                "consistency_failure_count": item.get(
                    "draw_resource_consistency_failure_count"
                ),
                "coverage_warnings": item.get("coverage_warnings"),
            }
            for item in renders
        ],
        "probe_health": report.get("probe_health"),
        "frame_sequence_health": report.get("frame_sequence_health"),
        "replay_suite": None if not replay else {
            "passed": replay.get("passed"),
            "stage_count": replay.get("stage_count"),
            "passed_stage_count": replay.get("passed_stage_count"),
            "stages": [
                {
                    "name": stage.get("name"),
                    "passed": stage.get("passed"),
                    "failure_reasons": stage.get("failure_reasons"),
                }
                for stage in replay.get("stages", [])
            ],
            "sequences": replay.get("sequences"),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run automated implementation and health gates before manual testing."
    )
    parser.add_argument("--extracted-root", type=Path)
    parser.add_argument("--render-stream", type=Path, action="append", default=[])
    parser.add_argument("--probe-summary", type=Path)
    parser.add_argument("--frame", type=Path, action="append", default=[])
    parser.add_argument("--suite", type=Path)
    parser.add_argument(
        "--stage-override",
        action="append",
        default=[],
        metavar="NAME=RENDER_STREAM",
        help="Override a suite stage capture without editing the checked suite.",
    )
    parser.add_argument(
        "--run-replays",
        action="store_true",
        help="Run every --suite stage through the Vulkan host in strict mode.",
    )
    parser.add_argument("--replay-output-dir", type=Path, default=Path("reports/local/preflight"))
    parser.add_argument("--exe", type=Path, default=DEFAULT_EXE)
    parser.add_argument("--skip-host-build", action="store_true")
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument(
        "--full-stdout",
        action="store_true",
        help="Print the complete report instead of the concise summary.",
    )
    args = parser.parse_args()

    stages = load_replay_suite(args.suite) if args.suite else []
    for override in args.stage_override:
        if "=" not in override:
            parser.error("--stage-override must use NAME=RENDER_STREAM")
        name, raw_path = override.split("=", 1)
        matches = [index for index, stage in enumerate(stages) if stage.name == name]
        if len(matches) != 1:
            parser.error(f"--stage-override names unknown or duplicate stage: {name}")
        stages[matches[0]] = replace(
            stages[matches[0]], render_stream=_resolve_path(raw_path)
        )
    replay = None
    if args.run_replays:
        if not stages:
            parser.error("--run-replays requires a non-empty --suite")
        if not args.skip_host_build:
            toolchain = discover_toolchain()
            compile_first_frame(output=args.exe, toolchain=toolchain)
            compile_shaders(toolchain=toolchain)
        replay = run_replay_suite(
            stages,
            output_dir=args.replay_output_dir,
            executable=args.exe,
        )
    report = build_preflight_report(
        extracted_root=args.extracted_root,
        render_streams=args.render_stream,
        probe_summary=args.probe_summary,
        frame_paths=args.frame,
        replay_suite=replay,
    )
    output = json.dumps(report, indent=2 if args.pretty else None, sort_keys=True)
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(output + "\n", encoding="utf-8", newline="\n")
    stdout_report = report if args.full_stdout else summarize_preflight_report(report)
    print(json.dumps(stdout_report, indent=2 if args.pretty else None, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
