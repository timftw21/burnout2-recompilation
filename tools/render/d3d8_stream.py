#!/usr/bin/env python3
"""Extract, decode, and replay recovered Direct3D 8 render command streams."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


STREAM_FORMAT = "b2-recomp-render-command-stream"
DECODED_FORMAT = "b2-recomp-render-command-decode"
REPLAY_FORMAT = "b2-recomp-render-command-replay"

D3D_MMIO_BASE = 0xFED00000
D3D_MMIO_END = 0xFED0FFFF
D3D_PUSH_BUFFER_BASE = 0x80000000
D3D_PUSH_BUFFER_END = 0x8000FFFF

MMIO_METHODS = {
    0x0008: {
        "name": "pfifo_kick_or_control",
        "category": "submission",
        "hle_role": "command processor kick/control write",
    },
    0x0018: {
        "name": "dma_put_or_get_shadow",
        "category": "submission",
        "hle_role": "DMA cursor/control shadow write",
    },
    0x0040: {
        "name": "push_buffer_method_or_data",
        "category": "push-buffer-setup",
        "hle_role": "observed push-buffer setup write",
    },
    0x0048: {
        "name": "push_buffer_pitch_or_limit",
        "category": "push-buffer-setup",
        "hle_role": "observed push-buffer range/size write",
    },
    0x004C: {
        "name": "push_buffer_get",
        "category": "push-buffer-setup",
        "hle_role": "observed push-buffer get/base write",
    },
    0x0050: {
        "name": "push_buffer_put",
        "category": "push-buffer-setup",
        "hle_role": "observed push-buffer put/base write",
    },
}

PUSH_BUFFER_METHOD_NAMES = {
    0x0000: "object",
    0x0100: "nop",
    0x0104: "notify",
    0x0180: "dma_notify",
    0x0184: "dma_source",
    0x0188: "dma_color",
    0x018C: "dma_zeta",
    0x0190: "dma_texture0",
    0x0194: "dma_texture1",
    0x0198: "dma_vertex",
    0x01A0: "dma_state",
    0x01D8: "surface_clip_horizontal",
    0x01DC: "surface_clip_vertical",
    0x01E0: "surface_format",
    0x01E4: "surface_pitch",
    0x01E8: "color_offset",
    0x01EC: "zeta_offset",
    0x0300: "clear_surface",
    0x03B0: "viewport_scale",
    0x03C0: "viewport_offset",
    0x0400: "texture_offset",
    0x0418: "texture_format",
    0x041C: "texture_address",
    0x0420: "texture_control0",
    0x0428: "texture_filter",
    0x042C: "texture_image_rect",
    0x0430: "texture_palette",
    0x06B0: "begin_end",
    0x1810: "clear_color",
    0x1814: "clear_depth",
}


class RenderStreamError(RuntimeError):
    """Raised when a render stream artifact is malformed."""


def hex32(value: int | None) -> str | None:
    if value is None:
        return None
    return f"0x{value & 0xFFFFFFFF:08X}"


def normalize_render_stream(data: dict[str, Any]) -> dict[str, Any]:
    """Return a first-class render stream from a stream or probe summary."""

    if data.get("format") == STREAM_FORMAT:
        return _normalize_direct_stream(data)

    streams = extract_render_streams_from_probe_summary(data)
    if not streams:
        raise RenderStreamError("input does not contain a recovered render stream")
    return streams[0]


def extract_render_streams_from_probe_summary(summary: dict[str, Any]) -> list[dict[str, Any]]:
    execution = summary.get("entry_recovery", {}).get("execution", {})
    streams: list[dict[str, Any]] = []
    for index, thread in enumerate(execution.get("guest_thread_executions", [])):
        stream = thread.get("render_command_stream")
        if not isinstance(stream, dict):
            continue
        normalized = _normalize_embedded_stream(
            stream,
            source={
                "kind": "playability_probe_guest_thread",
                "thread_index": index,
                "thread_start": thread.get("start_address_hex"),
                "thread_status": thread.get("status"),
            },
        )
        if normalized["write_count"]:
            streams.append(normalized)

    stream = execution.get("render_command_stream")
    if isinstance(stream, dict):
        normalized = _normalize_embedded_stream(
            stream,
            source={
                "kind": "playability_probe_entry_execution",
                "execution_status": execution.get("status"),
            },
        )
        if normalized["write_count"]:
            streams.append(normalized)
    return streams


def _normalize_direct_stream(stream: dict[str, Any]) -> dict[str, Any]:
    return _normalize_embedded_stream(
        stream,
        source=stream.get("source") if isinstance(stream.get("source"), dict) else {},
    )


def _normalize_embedded_stream(
    stream: dict[str, Any],
    *,
    source: dict[str, Any],
) -> dict[str, Any]:
    writes = [_normalize_write(write, index) for index, write in enumerate(stream.get("writes", []))]
    counts = Counter(write["kind"] for write in writes)
    return {
        "format": STREAM_FORMAT,
        "public_safe": False,
        "source": source,
        "write_count": int(stream.get("write_count", len(writes))),
        "mmio_write_count": int(stream.get("mmio_write_count", counts.get("d3d_mmio", 0))),
        "push_buffer_write_count": int(
            stream.get("push_buffer_write_count", counts.get("d3d_push_buffer", 0))
        ),
        "captured_write_count": len(writes),
        "truncated": bool(stream.get("truncated", stream.get("write_count", len(writes)) > len(writes))),
        "writes": writes,
    }


def _normalize_write(write: dict[str, Any], fallback_sequence: int) -> dict[str, Any]:
    kind = str(write.get("kind", "unknown"))
    address = _coerce_u32(write.get("address"), write.get("address_hex"))
    value = _coerce_u32(write.get("value"), write.get("value_hex"))
    offset = _coerce_u32(write.get("offset"), write.get("offset_hex"))
    if offset is None and address is not None:
        if D3D_MMIO_BASE <= address <= D3D_MMIO_END:
            offset = address - D3D_MMIO_BASE
        elif D3D_PUSH_BUFFER_BASE <= address <= D3D_PUSH_BUFFER_END:
            offset = address - D3D_PUSH_BUFFER_BASE
    return {
        "sequence": write.get("sequence", fallback_sequence),
        "instruction_address_hex": write.get("instruction_address_hex"),
        "kind": kind,
        "address": address,
        "address_hex": hex32(address),
        "offset": offset,
        "offset_hex": hex32(offset),
        "value": value,
        "value_hex": hex32(value),
        "size": int(write.get("size", 4)),
    }


def _coerce_u32(*values: Any) -> int | None:
    for value in values:
        if value is None:
            continue
        if isinstance(value, int):
            return value & 0xFFFFFFFF
        if isinstance(value, str) and value:
            return int(value, 0) & 0xFFFFFFFF
    return None


def decode_render_stream(stream: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_render_stream(stream)
    decoded: list[dict[str, Any]] = []
    pending_push_writes: list[dict[str, Any]] = []

    def flush_pending_push_writes() -> None:
        if not pending_push_writes:
            return
        decoded.extend(_decode_push_buffer_run(pending_push_writes))
        pending_push_writes.clear()

    for write in normalized["writes"]:
        if write["kind"] == "d3d_push_buffer":
            if (
                pending_push_writes
                and write["address"]
                != _write_end_address(pending_push_writes[-1])
            ):
                flush_pending_push_writes()
            pending_push_writes.append(write)
            continue
        flush_pending_push_writes()
        if write["kind"] == "d3d_mmio":
            decoded.append(_decode_mmio_write(write))
        else:
            decoded.append(_decode_unknown_write(write))
    flush_pending_push_writes()

    counts = Counter(item["decode_status"] for item in decoded)
    categories = Counter(item.get("category", "unknown") for item in decoded)
    method_packet_count = sum(
        1 for item in decoded if item.get("category") == "push-buffer-methods"
    )
    zero_count_method_word_count = sum(
        1 for item in decoded if item.get("category") == "push-buffer-zero-count-word"
    )
    interpreted_method_count = sum(
        len(item.get("methods", []))
        for item in decoded
        if item.get("category") == "push-buffer-methods"
    )
    frame_state = _frame_state_from_decoded_commands(decoded)
    return {
        "format": DECODED_FORMAT,
        "public_safe": False,
        "source_format": normalized["format"],
        "write_count": normalized["write_count"],
        "captured_write_count": normalized["captured_write_count"],
        "decoded_command_count": len(decoded),
        "status_counts": dict(sorted(counts.items())),
        "category_counts": dict(sorted(categories.items())),
        "push_buffer_method_packet_count": method_packet_count,
        "interpreted_method_count": interpreted_method_count,
        "zero_count_method_word_count": zero_count_method_word_count,
        "state_update_count": len(frame_state["state_updates"]),
        "frame_state": frame_state,
        "interpretation": "nv4-pre-gf100-dma-pusher-method-packets",
        "commands": decoded,
    }


def _decode_mmio_write(write: dict[str, Any]) -> dict[str, Any]:
    offset = write.get("offset")
    method = MMIO_METHODS.get(offset)
    if method is None:
        return {
            **_decoded_write_base(write),
            "decode_status": "unknown_preserved",
            "category": "mmio-unknown",
            "method": f"mmio_{offset:04X}" if isinstance(offset, int) else "mmio_unknown",
            "hle_role": "preserved for later NV2A/D3D8 method recovery",
        }
    return {
        **_decoded_write_base(write),
        "decode_status": "known",
        "category": method["category"],
        "method": method["name"],
        "hle_role": method["hle_role"],
    }


def _write_end_address(write: dict[str, Any]) -> int | None:
    address = write.get("address")
    if not isinstance(address, int):
        return None
    return address + int(write.get("size", 4))


def _decode_push_buffer_run(writes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    run = _push_buffer_run_from_writes(writes)
    decoded: list[dict[str, Any]] = [
        {
            "decode_status": "known_layout_preserved",
            "category": "push-buffer-run",
            "method": "push_buffer_contiguous_bytes",
            "hle_role": "reassembled contiguous push-buffer bytes before packet decoding",
            "sequence_first": writes[0].get("sequence"),
            "sequence_last": writes[-1].get("sequence"),
            "address": run["address"],
            "address_hex": hex32(run["address"]),
            "end_address": run["end_address"],
            "end_address_hex": hex32(run["end_address"]),
            "offset": run["offset"],
            "offset_hex": hex32(run["offset"]),
            "byte_count": len(run["bytes"]),
            "dword_count": len(run["words"]),
            "trailing_byte_count": len(run["trailing_bytes"]),
            "bytes_hex": run["bytes"].hex().upper(),
        }
    ]
    decoded.extend(_decode_push_buffer_words(run))
    return decoded


def _push_buffer_run_from_writes(writes: list[dict[str, Any]]) -> dict[str, Any]:
    first_address = int(writes[0].get("address") or D3D_PUSH_BUFFER_BASE)
    payload = bytearray()
    for write in writes:
        value = int(write.get("value") or 0)
        size = max(1, min(int(write.get("size", 4)), 4))
        payload.extend(value.to_bytes(4, "little")[:size])
    words: list[dict[str, Any]] = []
    for byte_offset in range(0, len(payload) - (len(payload) % 4), 4):
        address = first_address + byte_offset
        value = int.from_bytes(payload[byte_offset : byte_offset + 4], "little")
        words.append(
            {
                "index": len(words),
                "address": address,
                "address_hex": hex32(address),
                "value": value,
                "value_hex": hex32(value),
            }
        )
    trailing = bytes(payload[len(words) * 4 :])
    return {
        "address": first_address,
        "end_address": first_address + len(payload) - 1,
        "offset": first_address - D3D_PUSH_BUFFER_BASE,
        "bytes": bytes(payload),
        "words": words,
        "trailing_bytes": trailing,
    }


def _decode_push_buffer_words(run: dict[str, Any]) -> list[dict[str, Any]]:
    words: list[dict[str, Any]] = run["words"]
    decoded: list[dict[str, Any]] = []
    index = 0
    while index < len(words):
        command = words[index]
        word = int(command["value"])
        if (word & 0xE0030003) == 0:
            packet, index = _decode_method_packet(
                words,
                index,
                packet_kind="increasing_methods",
                non_increasing=False,
            )
            decoded.append(packet)
        elif (word & 0xE0030003) == 0x40000000:
            packet, index = _decode_method_packet(
                words,
                index,
                packet_kind="non_increasing_methods",
                non_increasing=True,
            )
            decoded.append(packet)
        elif (word & 0xFFFF0003) == 0x00030000:
            packet, index = _decode_long_non_increasing_packet(words, index)
            decoded.append(packet)
        elif (word & 0xE0000003) == 0x20000000:
            decoded.append(_decode_push_control(command, "old_jump", word & 0x1FFFFFFF))
            index += 1
        elif (word & 0x3) == 0x1:
            decoded.append(_decode_push_control(command, "jump", word & 0xFFFFFFFC))
            index += 1
        elif (word & 0x3) == 0x2:
            decoded.append(_decode_push_control(command, "call", word & 0xFFFFFFFC))
            index += 1
        elif word == 0x00020000:
            decoded.append(_decode_push_control(command, "return", None))
            index += 1
        else:
            decoded.append(
                {
                    **_decoded_push_word_base(command),
                    "decode_status": "unknown_preserved",
                    "category": "push-buffer-unknown-packet",
                    "method": "unknown_push_buffer_packet",
                    "packet_word_hex": hex32(word),
                    "hle_role": "preserved for later NV2A method recovery",
                }
            )
            index += 1
    if run["trailing_bytes"]:
        decoded.append(
            {
                "decode_status": "truncated_preserved",
                "category": "push-buffer-trailing-bytes",
                "method": "push_buffer_trailing_bytes",
                "hle_role": "preserved partial dword at end of recovered push-buffer run",
                "byte_count": len(run["trailing_bytes"]),
                "bytes_hex": run["trailing_bytes"].hex().upper(),
            }
        )
    return decoded


def _decode_method_packet(
    words: list[dict[str, Any]],
    index: int,
    *,
    packet_kind: str,
    non_increasing: bool,
) -> tuple[dict[str, Any], int]:
    command = words[index]
    word = int(command["value"])
    method_count = (word >> 18) & 0x7FF
    subchannel = (word >> 13) & 0x7
    first_method = ((word >> 2) & 0x7FF) * 4
    if method_count == 0:
        return (_decode_zero_count_method_word(command, packet_kind, subchannel, first_method), index + 1)
    data_start = index + 1
    data_end = min(data_start + method_count, len(words))
    data_words = words[data_start:data_end]
    methods = _method_data_records(
        data_words,
        first_method=first_method,
        non_increasing=non_increasing,
    )
    complete = len(data_words) == method_count
    return (
        {
            **_decoded_push_word_base(command),
            "decode_status": "known" if complete else "truncated_preserved",
            "category": "push-buffer-methods",
            "method": f"nv2a_{packet_kind}",
            "packet_kind": packet_kind,
            "subchannel": subchannel,
            "first_method": first_method,
            "first_method_hex": hex32(first_method),
            "method_count": method_count,
            "decoded_method_count": len(methods),
            "non_increasing": non_increasing,
            "methods": methods,
            "hle_role": "interpreted NV2A/D3D8 method packet",
        },
        data_end,
    )


def _decode_long_non_increasing_packet(
    words: list[dict[str, Any]],
    index: int,
) -> tuple[dict[str, Any], int]:
    command = words[index]
    word = int(command["value"])
    subchannel = (word >> 13) & 0x7
    first_method = ((word >> 2) & 0x7FF) * 4
    if index + 1 >= len(words):
        return (
            {
                **_decoded_push_word_base(command),
                "decode_status": "truncated_preserved",
                "category": "push-buffer-methods",
                "method": "nv2a_long_non_increasing_methods",
                "packet_kind": "long_non_increasing_methods",
                "subchannel": subchannel,
                "first_method": first_method,
                "first_method_hex": hex32(first_method),
                "method_count": None,
                "decoded_method_count": 0,
                "non_increasing": True,
                "methods": [],
                "hle_role": "preserved incomplete long method packet",
            },
            len(words),
        )
    count_word = words[index + 1]
    method_count = int(count_word["value"]) & 0x00FFFFFF
    if method_count == 0:
        zero_word = _decode_zero_count_method_word(
            command,
            "long_non_increasing_methods",
            subchannel,
            first_method,
        )
        zero_word["count_word_hex"] = count_word["value_hex"]
        return (zero_word, index + 2)
    data_start = index + 2
    data_end = min(data_start + method_count, len(words))
    data_words = words[data_start:data_end]
    methods = _method_data_records(
        data_words,
        first_method=first_method,
        non_increasing=True,
    )
    complete = len(data_words) == method_count
    return (
        {
            **_decoded_push_word_base(command),
            "decode_status": "known" if complete else "truncated_preserved",
            "category": "push-buffer-methods",
            "method": "nv2a_long_non_increasing_methods",
            "packet_kind": "long_non_increasing_methods",
            "subchannel": subchannel,
            "first_method": first_method,
            "first_method_hex": hex32(first_method),
            "method_count": method_count,
            "count_word_hex": count_word["value_hex"],
            "decoded_method_count": len(methods),
            "non_increasing": True,
            "methods": methods,
            "hle_role": "interpreted NV2A/D3D8 long non-increasing method packet",
        },
        data_end,
    )


def _method_data_records(
    data_words: list[dict[str, Any]],
    *,
    first_method: int,
    non_increasing: bool,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for offset, word in enumerate(data_words):
        method = first_method if non_increasing else first_method + offset * 4
        records.append(
            {
                "method": method,
                "method_hex": hex32(method),
                "name": PUSH_BUFFER_METHOD_NAMES.get(method, f"method_{method:04X}"),
                "data": int(word["value"]),
                "data_hex": word["value_hex"],
                "word_address_hex": word["address_hex"],
            }
        )
    return records


def _decode_zero_count_method_word(
    word_record: dict[str, Any],
    packet_kind: str,
    subchannel: int,
    first_method: int,
) -> dict[str, Any]:
    return {
        **_decoded_push_word_base(word_record),
        "decode_status": "known_layout_preserved",
        "category": "push-buffer-zero-count-word",
        "method": "nv2a_zero_count_method_word",
        "packet_kind": packet_kind,
        "subchannel": subchannel,
        "first_method": first_method,
        "first_method_hex": hex32(first_method),
        "method_count": 0,
        "decoded_method_count": 0,
        "methods": [],
        "hle_role": "preserved command-shaped push-buffer word with no method payload",
    }


def _decode_push_control(
    word_record: dict[str, Any],
    packet_kind: str,
    target: int | None,
) -> dict[str, Any]:
    result = {
        **_decoded_push_word_base(word_record),
        "decode_status": "known_layout_preserved",
        "category": "push-buffer-control-flow",
        "method": f"nv2a_{packet_kind}",
        "packet_kind": packet_kind,
        "hle_role": "preserved push-buffer control-flow command",
    }
    if target is not None:
        result["target"] = target
        result["target_hex"] = hex32(target)
    return result


def _decoded_push_word_base(word_record: dict[str, Any]) -> dict[str, Any]:
    return {
        "word_index": word_record.get("index"),
        "address": word_record.get("address"),
        "address_hex": word_record.get("address_hex"),
        "value": word_record.get("value"),
        "value_hex": word_record.get("value_hex"),
    }

def _decode_unknown_write(write: dict[str, Any]) -> dict[str, Any]:
    return {
        **_decoded_write_base(write),
        "decode_status": "unknown_preserved",
        "category": "unknown-write",
        "method": "unknown_render_write",
        "hle_role": "preserved without translation",
    }


def _decoded_write_base(write: dict[str, Any]) -> dict[str, Any]:
    return {
        "sequence": write.get("sequence"),
        "instruction_address_hex": write.get("instruction_address_hex"),
        "kind": write.get("kind"),
        "address": write.get("address"),
        "address_hex": write.get("address_hex"),
        "offset": write.get("offset"),
        "offset_hex": write.get("offset_hex"),
        "value": write.get("value"),
        "value_hex": write.get("value_hex"),
        "size": write.get("size", 4),
    }


def _frame_state_from_decoded_commands(commands: list[dict[str, Any]]) -> dict[str, Any]:
    state: dict[str, Any] = {
        "clear_color": None,
        "clear_depth": None,
        "clear_surface": None,
        "surface_payload": _surface_payload_from_decoded_commands(commands),
        "surface_clip": {},
        "surface_format": None,
        "surface_pitch": None,
        "texture_state": {},
        "begin_end": None,
        "state_updates": [],
    }
    for command in commands:
        if command.get("category") != "push-buffer-methods":
            continue
        for method in command.get("methods", []):
            update = _state_update_from_method(method)
            if update is None:
                continue
            state["state_updates"].append(update)
            name = update["name"]
            if name == "clear_color":
                state["clear_color"] = update["value"]
            elif name == "clear_depth":
                state["clear_depth"] = update["value"]
            elif name == "clear_surface":
                state["clear_surface"] = update["value"]
            elif name == "surface_clip_horizontal":
                state["surface_clip"]["horizontal"] = update["value"]
            elif name == "surface_clip_vertical":
                state["surface_clip"]["vertical"] = update["value"]
            elif name == "surface_format":
                state["surface_format"] = update["value"]
            elif name == "surface_pitch":
                state["surface_pitch"] = update["value"]
            elif name.startswith("texture_"):
                state["texture_state"][name] = update["value"]
            elif name == "begin_end":
                state["begin_end"] = update["value"]
    return state


def _surface_payload_from_decoded_commands(commands: list[dict[str, Any]]) -> dict[str, Any] | None:
    counts: Counter[int] = Counter()
    first_seen: dict[int, int] = {}
    samples: list[dict[str, Any]] = []
    sample_index = 0
    for command in commands:
        if command.get("category") != "push-buffer-run":
            continue
        bytes_hex = command.get("bytes_hex")
        if not isinstance(bytes_hex, str) or not bytes_hex:
            continue
        try:
            payload = bytes.fromhex(bytes_hex)
        except ValueError:
            continue
        base_address = command.get("address")
        for offset in range(0, len(payload) - (len(payload) % 4), 4):
            value = int.from_bytes(payload[offset : offset + 4], "little")
            counts[value] += 1
            first_seen.setdefault(value, sample_index)
            if len(samples) < 8:
                address = base_address + offset if isinstance(base_address, int) else None
                samples.append(
                    {
                        "address_hex": hex32(address),
                        "value_hex": hex32(value),
                    }
                )
            sample_index += 1
    if not counts:
        return None
    dominant, dominant_count = max(
        counts.items(),
        key=lambda item: (item[1], -first_seen[item[0]]),
    )
    return {
        "sample_count": sum(counts.values()),
        "unique_dword_count": len(counts),
        "dominant_dword": dominant,
        "dominant_dword_hex": hex32(dominant),
        "dominant_sample_count": dominant_count,
        "dominant_color": _argb_color(dominant),
        "samples": samples,
    }


def _state_update_from_method(method: dict[str, Any]) -> dict[str, Any] | None:
    name = str(method.get("name", ""))
    data = int(method.get("data") or 0)
    if name == "clear_color":
        value = _argb_color(data)
    elif name == "clear_depth":
        value = {
            "raw": data,
            "raw_hex": hex32(data),
            "depth24": data & 0x00FFFFFF,
            "stencil": (data >> 24) & 0xFF,
        }
    elif name == "clear_surface":
        value = _clear_surface_mask(data)
    elif name == "begin_end":
        value = {
            "raw": data,
            "raw_hex": hex32(data),
            "primitive": _primitive_from_begin_end(data),
            "active": data != 0,
        }
    elif name in {
        "surface_format",
        "texture_offset",
        "texture_format",
        "texture_address",
        "texture_control0",
        "texture_filter",
        "texture_image_rect",
        "texture_palette",
    }:
        value = {"raw": data, "raw_hex": hex32(data)}
    elif name in {"surface_clip_horizontal", "surface_clip_vertical"}:
        value = {
            "raw": data,
            "raw_hex": hex32(data),
            "origin": data & 0xFFFF,
            "extent": (data >> 16) & 0xFFFF,
        }
    elif name == "surface_pitch":
        value = {
            "raw": data,
            "raw_hex": hex32(data),
            "color_pitch": data & 0xFFFF,
            "zeta_pitch": (data >> 16) & 0xFFFF,
        }
    else:
        return None
    return {
        "name": name,
        "method_hex": method.get("method_hex"),
        "data_hex": method.get("data_hex"),
        "value": value,
    }


def _clear_surface_mask(value: int) -> dict[str, Any]:
    color_channel_mask = value & 0xF0
    return {
        "raw": value,
        "raw_hex": hex32(value),
        "clears_depth": bool(value & 0x01),
        "clears_stencil": bool(value & 0x02),
        "clears_color": bool(color_channel_mask),
        "color_channel_mask": color_channel_mask,
        "color_channel_mask_hex": hex32(color_channel_mask),
        "unknown_bits": value & ~0xF3,
        "unknown_bits_hex": hex32(value & ~0xF3),
    }


def _argb_color(value: int) -> dict[str, Any]:
    a = (value >> 24) & 0xFF
    r = (value >> 16) & 0xFF
    g = (value >> 8) & 0xFF
    b = value & 0xFF
    return {
        "raw": value,
        "raw_hex": hex32(value),
        "a8": a,
        "r8": r,
        "g8": g,
        "b8": b,
        "normalized": {
            "r": round(r / 255.0, 6),
            "g": round(g / 255.0, 6),
            "b": round(b / 255.0, 6),
            "a": round(a / 255.0, 6),
        },
    }


def replay_render_stream(
    stream: dict[str, Any],
    *,
    width: int = 640,
    height: int = 480,
) -> dict[str, Any]:
    """Build deterministic translated host work from interpreted D3D8/NV2A packets."""

    normalized = normalize_render_stream(stream)
    decoded = decode_render_stream(normalized)
    commands: list[dict[str, Any]] = []
    method_packets = 0
    interpreted_methods = 0
    control_packets = 0
    mmio_setup_writes = 0
    submission_kicks = 0
    unknown_preserved = 0
    zero_count_method_words = 0
    for decoded_command in decoded["commands"]:
        category = decoded_command.get("category")
        if category == "push-buffer-run":
            commands.append(
                {
                    "index": len(commands),
                    "translated_kind": "push_buffer_run_reassembled",
                    "address_hex": decoded_command.get("address_hex"),
                    "byte_count": decoded_command.get("byte_count"),
                    "dword_count": decoded_command.get("dword_count"),
                }
            )
        elif category == "push-buffer-methods":
            method_packets += 1
            interpreted_methods += len(decoded_command.get("methods", []))
            commands.append(
                {
                    "index": len(commands),
                    "translated_kind": "nv2a_method_packet",
                    "packet_kind": decoded_command.get("packet_kind"),
                    "subchannel": decoded_command.get("subchannel"),
                    "first_method_hex": decoded_command.get("first_method_hex"),
                    "method_count": decoded_command.get("method_count"),
                    "decoded_method_count": decoded_command.get("decoded_method_count"),
                    "methods": decoded_command.get("methods", []),
                }
            )
        elif category == "push-buffer-zero-count-word":
            zero_count_method_words += 1
            commands.append(
                {
                    "index": len(commands),
                    "translated_kind": "preserved_zero_count_method_word",
                    "packet_kind": decoded_command.get("packet_kind"),
                    "first_method_hex": decoded_command.get("first_method_hex"),
                    "value_hex": decoded_command.get("value_hex"),
                }
            )
        elif category == "push-buffer-control-flow":
            control_packets += 1
            commands.append(
                {
                    "index": len(commands),
                    "translated_kind": "push_buffer_control_flow",
                    "packet_kind": decoded_command.get("packet_kind"),
                    "target_hex": decoded_command.get("target_hex"),
                }
            )
        elif category in {"submission", "push-buffer-setup"}:
            if category == "push-buffer-setup":
                mmio_setup_writes += 1
            if decoded_command.get("method") == "pfifo_kick_or_control":
                submission_kicks += 1
            commands.append(
                {
                    "index": len(commands),
                    "translated_kind": "gpu_mmio_write",
                    "method": decoded_command.get("method"),
                    "category": category,
                    "address_hex": decoded_command.get("address_hex"),
                    "value_hex": decoded_command.get("value_hex"),
                }
            )
        elif str(decoded_command.get("decode_status", "")).startswith("unknown"):
            unknown_preserved += 1
            commands.append(
                {
                    "index": len(commands),
                    "translated_kind": "preserved_unknown_render_command",
                    "category": category,
                    "address_hex": decoded_command.get("address_hex"),
                    "value_hex": decoded_command.get("value_hex"),
                }
            )
    state_seed = _state_seed_from_decoded_commands(decoded["commands"])
    frame_state = decoded["frame_state"]
    surface_payload = frame_state.get("surface_payload")
    if frame_state.get("clear_color"):
        diagnostic_clear_color = frame_state["clear_color"]["normalized"]
        diagnostic_clear_source = "d3d8_clear_color_method"
    elif (
        isinstance(surface_payload, dict)
        and int(surface_payload.get("dominant_sample_count") or 0) >= 2
    ):
        diagnostic_clear_color = surface_payload["dominant_color"]["normalized"]
        diagnostic_clear_source = "recovered_surface_payload"
    else:
        diagnostic_clear_color = _color_from_seed(state_seed)
        diagnostic_clear_source = "state_seed"
    vulkan_state = _vulkan_state_from_frame_state(
        frame_state,
        width=width,
        height=height,
        diagnostic_clear_color=diagnostic_clear_color,
        diagnostic_clear_source=diagnostic_clear_source,
    )
    vulkan_work = _vulkan_work_from_frame_state(frame_state, vulkan_state)
    clear_work_count = sum(
        1
        for item in vulkan_work
        if item["translated_kind"]
        in {"vulkan_render_pass_begin_clear", "vulkan_cmd_clear_attachments"}
    )
    draw_work_count = sum(
        1 for item in vulkan_work if item["translated_kind"] == "vulkan_cmd_draw"
    )
    state_work_count = len(vulkan_work) - clear_work_count - draw_work_count
    return {
        "format": REPLAY_FORMAT,
        "public_safe": False,
        "renderer_backend": "vulkan",
        "width": width,
        "height": height,
        "source_write_count": normalized["write_count"],
        "captured_source_write_count": normalized["captured_write_count"],
        "decoded_command_count": decoded["decoded_command_count"],
        "push_buffer_method_packet_count": method_packets,
        "interpreted_method_count": interpreted_methods,
        "push_buffer_control_packet_count": control_packets,
        "mmio_setup_write_count": mmio_setup_writes,
        "submission_kick_count": submission_kicks,
        "unknown_preserved_count": unknown_preserved,
        "zero_count_method_word_count": zero_count_method_words,
        "translated_command_count": len(commands),
        "vulkan_work_count": len(vulkan_work),
        "vulkan_clear_work_count": clear_work_count,
        "vulkan_draw_work_count": draw_work_count,
        "vulkan_state_work_count": state_work_count,
        "translation_semantics": "d3d8-nv2a-method-push-buffer-interpretation",
        "frame_state": {
            "seed_sha256": state_seed,
            "diagnostic_clear_color": diagnostic_clear_color,
            "diagnostic_clear_source": diagnostic_clear_source,
            "method_packets": method_packets,
            "interpreted_methods": interpreted_methods,
            "state_update_count": len(frame_state["state_updates"]),
            "clear_color": frame_state.get("clear_color"),
            "clear_depth": frame_state.get("clear_depth"),
            "clear_surface": frame_state.get("clear_surface"),
            "surface_payload": surface_payload,
            "surface_clip": frame_state.get("surface_clip"),
            "surface_format": frame_state.get("surface_format"),
            "surface_pitch": frame_state.get("surface_pitch"),
            "texture_state": frame_state.get("texture_state"),
            "begin_end": frame_state.get("begin_end"),
        },
        "vulkan_state": vulkan_state,
        "vulkan_work": vulkan_work,
        "unknown_source_preservation": True,
        "commands": commands,
    }


def _vulkan_state_from_frame_state(
    frame_state: dict[str, Any],
    *,
    width: int,
    height: int,
    diagnostic_clear_color: dict[str, float],
    diagnostic_clear_source: str,
) -> dict[str, Any]:
    clip = frame_state.get("surface_clip")
    clip = clip if isinstance(clip, dict) else {}
    horizontal = clip.get("horizontal") if isinstance(clip.get("horizontal"), dict) else {}
    vertical = clip.get("vertical") if isinstance(clip.get("vertical"), dict) else {}
    render_area = {
        "x": int(horizontal.get("origin") or 0),
        "y": int(vertical.get("origin") or 0),
        "width": int(horizontal.get("extent") or width),
        "height": int(vertical.get("extent") or height),
    }
    clear_surface = frame_state.get("clear_surface")
    clear_surface = clear_surface if isinstance(clear_surface, dict) else {}
    clear_attachments: list[str] = []
    if frame_state.get("clear_color") or clear_surface.get("clears_color"):
        clear_attachments.append("color")
    if (
        frame_state.get("clear_depth")
        or clear_surface.get("clears_depth")
        or clear_surface.get("clears_stencil")
    ):
        clear_attachments.append("depth_stencil")
    return {
        "backend": "vulkan",
        "render_area": render_area,
        "diagnostic_clear": {
            "source": diagnostic_clear_source,
            "color": diagnostic_clear_color,
        },
        "clear_attachments": clear_attachments,
        "surface_format": frame_state.get("surface_format"),
        "surface_pitch": frame_state.get("surface_pitch"),
        "texture_state": frame_state.get("texture_state", {}),
        "clear_surface": frame_state.get("clear_surface"),
        "begin_end": frame_state.get("begin_end"),
    }


def _vulkan_work_from_frame_state(
    frame_state: dict[str, Any],
    vulkan_state: dict[str, Any],
) -> list[dict[str, Any]]:
    work: list[dict[str, Any]] = []

    def add(translated_kind: str, **fields: Any) -> None:
        work.append({"index": len(work), "translated_kind": translated_kind, **fields})

    if frame_state.get("surface_format") or frame_state.get("surface_pitch"):
        add(
            "vulkan_surface_state",
            surface_format=frame_state.get("surface_format"),
            surface_pitch=frame_state.get("surface_pitch"),
        )
    if frame_state.get("surface_clip"):
        add("vulkan_viewport_scissor_state", render_area=vulkan_state["render_area"])
    if frame_state.get("texture_state"):
        add("vulkan_texture_state", texture_state=frame_state.get("texture_state"))

    add(
        "vulkan_render_pass_begin_clear",
        clear_color=vulkan_state["diagnostic_clear"]["color"],
        clear_source=vulkan_state["diagnostic_clear"]["source"],
        render_area=vulkan_state["render_area"],
    )

    clear_surface = frame_state.get("clear_surface")
    if isinstance(clear_surface, dict):
        add(
            "vulkan_cmd_clear_attachments",
            clear_surface=clear_surface,
            clear_attachments=vulkan_state["clear_attachments"],
            clear_color=frame_state.get("clear_color"),
            clear_depth=frame_state.get("clear_depth"),
            render_area=vulkan_state["render_area"],
        )

    begin_end = frame_state.get("begin_end")
    if isinstance(begin_end, dict) and bool(begin_end.get("active")):
        add(
            "vulkan_cmd_draw",
            primitive=begin_end.get("primitive"),
            begin_end=begin_end,
            vertex_source="recovered_nv2a_push_buffer_state",
        )
    return work


def _primitive_from_begin_end(value: int) -> str:
    names = {
        1: "point_list",
        2: "line_list",
        3: "line_strip",
        4: "triangle_list",
        5: "triangle_strip",
        6: "triangle_fan",
    }
    return names.get(value, f"primitive_{value}")


def _state_seed_from_decoded_commands(commands: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for command in commands:
        digest.update(str(command.get("category", "")).encode("ascii", "replace"))
        digest.update(b"\0")
        digest.update(str(command.get("method", "")).encode("ascii", "replace"))
        digest.update(b"\0")
        for method in command.get("methods", []):
            digest.update(int(method.get("method") or 0).to_bytes(4, "little"))
            digest.update(int(method.get("data") or 0).to_bytes(4, "little"))
        value = command.get("value")
        if isinstance(value, int):
            digest.update(value.to_bytes(4, "little"))
    return digest.hexdigest().upper()


def _color_from_seed(seed_text: str) -> dict[str, float]:
    seed = int(seed_text[:8], 16) if seed_text else 0
    return {
        "r": round(0.18 + (((seed >> 16) & 0xFF) / 255.0) * 0.62, 6),
        "g": round(0.18 + (((seed >> 8) & 0xFF) / 255.0) * 0.62, 6),
        "b": round(0.18 + ((seed & 0xFF) / 255.0) * 0.62, 6),
        "a": 1.0,
    }


def write_json(path: Path, payload: dict[str, Any], *, pretty: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2 if pretty else None, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract, decode, and replay recovered D3D8 render streams."
    )
    parser.add_argument("--input", type=Path, required=True, help="Probe summary or stream JSON.")
    parser.add_argument("--stream-output", type=Path, help="Optional normalized stream output.")
    parser.add_argument("--decode-output", type=Path, help="Optional decoded command output.")
    parser.add_argument("--replay-output", type=Path, help="Optional translated replay output.")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    data = json.loads(args.input.read_text(encoding="utf-8"))
    stream = normalize_render_stream(data)
    decoded = decode_render_stream(stream)
    replay = replay_render_stream(stream, width=args.width, height=args.height)

    if args.stream_output is not None:
        write_json(args.stream_output, stream, pretty=args.pretty)
    if args.decode_output is not None:
        write_json(args.decode_output, decoded, pretty=args.pretty)
    if args.replay_output is not None:
        write_json(args.replay_output, replay, pretty=args.pretty)
    print(json.dumps(replay, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
