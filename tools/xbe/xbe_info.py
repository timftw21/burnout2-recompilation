#!/usr/bin/env python3
"""Minimal Xbox XBE metadata parser for local analysis reports."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import struct
from pathlib import Path
from typing import Any


class XbeFormatError(ValueError):
    """Raised when an input file is not a parseable XBE."""


MAGIC = b"XBEH"
HEADER_MIN_SIZE = 0x178
SECTION_HEADER_SIZE = 0x38
CERTIFICATE_TITLE_NAME_OFFSET = 0x0C
CERTIFICATE_TITLE_NAME_SIZE = 0x50

ENTRY_KEYS = {
    "beta": 0xE682F45B,
    "debug": 0x94859D4B,
    "retail": 0xA8FC57AB,
}

KERNEL_THUNK_KEYS = {
    "beta": 0x46437DCD,
    "debug": 0xEFB1F152,
    "retail": 0x5B6D40B6,
}

MEDIA_FLAGS = {
    0x00000001: "HARD_DISK",
    0x00000002: "DVD_X2",
    0x00000004: "DVD_CD",
    0x00000008: "CD",
    0x00000010: "DVD_5_RO",
    0x00000020: "DVD_9_RO",
    0x00000040: "DVD_5_RW",
    0x00000080: "DVD_9_RW",
    0x00000100: "DONGLE",
    0x00000200: "MEDIA_BOARD",
    0x40000000: "NONSECURE_HARD_DISK",
    0x80000000: "NONSECURE_MODE",
}

REGION_FLAGS = {
    0x00000001: "NA",
    0x00000002: "JAPAN",
    0x00000004: "REST_OF_WORLD",
    0x80000000: "MANUFACTURING",
}

SECTION_FLAGS = {
    0x00000001: "WRITABLE",
    0x00000002: "PRELOAD",
    0x00000004: "EXECUTABLE",
    0x00000008: "INSERTED_FILE",
    0x00000010: "HEAD_PAGE_READ_ONLY",
    0x00000020: "TAIL_PAGE_READ_ONLY",
}


def _u32(data: bytes, offset: int) -> int:
    _require_range(data, offset, 4, "u32")
    return struct.unpack_from("<I", data, offset)[0]


def _require_range(data: bytes, offset: int, size: int, label: str) -> None:
    if offset < 0 or size < 0 or offset + size > len(data):
        raise XbeFormatError(
            f"{label} range 0x{offset:X}..0x{offset + size:X} is outside file"
        )


def _timestamp_to_iso8601(value: int) -> str | None:
    if value == 0:
        return None
    try:
        return dt.datetime.fromtimestamp(value, tz=dt.UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _decode_fixed_utf16le(raw: bytes) -> str:
    text = raw.decode("utf-16-le", errors="replace")
    return text.split("\x00", 1)[0]


def _read_c_string(data: bytes, offset: int) -> str:
    _require_range(data, offset, 1, "string")
    end = data.find(b"\x00", offset)
    if end == -1:
        end = len(data)
    return data[offset:end].decode("ascii", errors="replace")


def _flags(value: int, names: dict[int, str]) -> list[str]:
    return [name for bit, name in names.items() if value & bit]


def _unknown_flag_bits(value: int, names: dict[int, str]) -> int:
    known = 0
    for bit in names:
        known |= bit
    return value & ~known


def _hex32(value: int) -> str:
    return f"0x{value:08X}"


def _title_id_details(title_id: int) -> dict[str, Any]:
    hex_value = f"{title_id:08X}"
    publisher_code: str | None = None
    try:
        publisher_bytes = bytes.fromhex(hex_value[:4])
        if all(32 <= byte <= 126 for byte in publisher_bytes):
            publisher_code = publisher_bytes.decode("ascii")
    except ValueError:
        publisher_code = None

    return {
        "hex": hex_value,
        "publisher_code": publisher_code,
        "publisher_title_number": int(hex_value[4:], 16),
    }


def _header_vaddr_to_file_offset(address: int, header: dict[str, Any]) -> int:
    base_address = header["base_address"]
    headers_size = header["headers_size"]
    if base_address <= address < base_address + headers_size:
        return address - base_address
    raise XbeFormatError(
        f"virtual address 0x{address:08X} is not inside the XBE header range"
    )


def _vaddr_to_file_offset(
    address: int, header: dict[str, Any], sections: list[dict[str, Any]]
) -> int:
    if address == 0:
        raise XbeFormatError("null virtual address cannot be mapped")

    base_address = header["base_address"]
    headers_size = header["headers_size"]
    if base_address <= address < base_address + headers_size:
        return address - base_address

    for section in sections:
        virtual_address = section["virtual_address"]
        virtual_size = max(section["virtual_size"], section["raw_size"])
        if virtual_address <= address < virtual_address + virtual_size:
            return section["raw_address"] + (address - virtual_address)

    raise XbeFormatError(f"virtual address 0x{address:08X} could not be mapped")


def _decode_keyed_address(
    encoded: int, keys: dict[str, int], header: dict[str, Any]
) -> dict[str, Any]:
    base_address = header["base_address"]
    image_end = base_address + header["image_size"]
    candidates = []
    chosen = None

    for name, key in keys.items():
        decoded = encoded ^ key
        valid = base_address <= decoded < image_end
        candidate = {
            "key": name,
            "address": _hex32(decoded),
            "valid_for_image": valid,
        }
        candidates.append(candidate)
        if valid and chosen is None:
            chosen = candidate

    return {
        "encoded": _hex32(encoded),
        "selected": chosen,
        "candidates": candidates,
    }


def parse_xbe_bytes(data: bytes) -> dict[str, Any]:
    """Parse a subset of XBE metadata without exposing executable contents."""

    if len(data) < HEADER_MIN_SIZE:
        raise XbeFormatError(
            f"file is too small for an XBE header: {len(data)} bytes"
        )
    if data[:4] != MAGIC:
        raise XbeFormatError("missing XBEH magic")

    header = {
        "base_address": _u32(data, 0x0104),
        "headers_size": _u32(data, 0x0108),
        "image_size": _u32(data, 0x010C),
        "image_header_size": _u32(data, 0x0110),
        "timestamp": _u32(data, 0x0114),
        "timestamp_utc": _timestamp_to_iso8601(_u32(data, 0x0114)),
        "certificate_address": _u32(data, 0x0118),
        "section_count": _u32(data, 0x011C),
        "section_headers_address": _u32(data, 0x0120),
        "init_flags": _u32(data, 0x0124),
        "entry_point": _decode_keyed_address(
            _u32(data, 0x0128),
            ENTRY_KEYS,
            {
                "base_address": _u32(data, 0x0104),
                "image_size": _u32(data, 0x010C),
            },
        ),
        "tls_address": _u32(data, 0x012C),
        "stack_size": _u32(data, 0x0130),
        "pe_heap_reserve": _u32(data, 0x0134),
        "pe_heap_commit": _u32(data, 0x0138),
        "pe_base_address": _u32(data, 0x013C),
        "pe_image_size": _u32(data, 0x0140),
        "pe_checksum": _u32(data, 0x0144),
        "pe_timestamp": _u32(data, 0x0148),
        "pe_timestamp_utc": _timestamp_to_iso8601(_u32(data, 0x0148)),
        "kernel_thunk_address": _decode_keyed_address(
            _u32(data, 0x0158),
            KERNEL_THUNK_KEYS,
            {
                "base_address": _u32(data, 0x0104),
                "image_size": _u32(data, 0x010C),
            },
        ),
        "non_kernel_import_directory_address": _u32(data, 0x015C),
        "library_versions_count": _u32(data, 0x0160),
        "library_versions_address": _u32(data, 0x0164),
        "kernel_library_version_address": _u32(data, 0x0168),
        "xapi_library_version_address": _u32(data, 0x016C),
        "logo_bitmap_address": _u32(data, 0x0170),
        "logo_bitmap_size": _u32(data, 0x0174),
    }

    if header["image_header_size"] < HEADER_MIN_SIZE:
        raise XbeFormatError(
            f"unexpected image header size: 0x{header['image_header_size']:X}"
        )
    if header["headers_size"] > len(data):
        raise XbeFormatError(
            f"headers_size 0x{header['headers_size']:X} is larger than file"
        )

    section_headers_offset = _header_vaddr_to_file_offset(
        header["section_headers_address"], header
    )

    sections = []
    for index in range(header["section_count"]):
        offset = section_headers_offset + index * SECTION_HEADER_SIZE
        _require_range(data, offset, SECTION_HEADER_SIZE, "section header")
        (
            flags,
            virtual_address,
            virtual_size,
            raw_address,
            raw_size,
            section_name_address,
            section_name_ref_count,
            head_shared_page_ref_count_address,
            tail_shared_page_ref_count_address,
        ) = struct.unpack_from("<IIIIIIIII", data, offset)
        digest = data[offset + 0x24 : offset + SECTION_HEADER_SIZE].hex().upper()

        sections.append(
            {
                "index": index,
                "name": None,
                "flags": flags,
                "flag_names": _flags(flags, SECTION_FLAGS),
                "unknown_flag_bits": _unknown_flag_bits(flags, SECTION_FLAGS),
                "virtual_address": virtual_address,
                "virtual_size": virtual_size,
                "raw_address": raw_address,
                "raw_size": raw_size,
                "section_name_address": section_name_address,
                "section_name_ref_count": section_name_ref_count,
                "head_shared_page_ref_count_address": head_shared_page_ref_count_address,
                "tail_shared_page_ref_count_address": tail_shared_page_ref_count_address,
                "digest_sha1": digest,
            }
        )

    for section in sections:
        try:
            name_offset = _vaddr_to_file_offset(
                section["section_name_address"], header, sections
            )
            section["name"] = _read_c_string(data, name_offset)
        except XbeFormatError:
            section["name"] = None

    certificate_offset = _vaddr_to_file_offset(
        header["certificate_address"], header, sections
    )
    _require_range(data, certificate_offset, 0x1D0, "certificate")

    title_id = _u32(data, certificate_offset + 0x08)
    title_raw = data[
        certificate_offset
        + CERTIFICATE_TITLE_NAME_OFFSET : certificate_offset
        + CERTIFICATE_TITLE_NAME_OFFSET
        + CERTIFICATE_TITLE_NAME_SIZE
    ]
    alternate_title_ids = [
        _u32(data, certificate_offset + 0x5C + index * 4) for index in range(16)
    ]
    allowed_media = _u32(data, certificate_offset + 0x9C)
    game_region = _u32(data, certificate_offset + 0xA0)

    certificate = {
        "offset": certificate_offset,
        "size": _u32(data, certificate_offset),
        "timestamp": _u32(data, certificate_offset + 0x04),
        "timestamp_utc": _timestamp_to_iso8601(_u32(data, certificate_offset + 0x04)),
        "title_id": _title_id_details(title_id),
        "title_name": _decode_fixed_utf16le(title_raw),
        "alternate_title_ids": [
            _title_id_details(value) for value in alternate_title_ids if value != 0
        ],
        "allowed_media": allowed_media,
        "allowed_media_names": _flags(allowed_media, MEDIA_FLAGS),
        "allowed_media_unknown_bits": _unknown_flag_bits(allowed_media, MEDIA_FLAGS),
        "game_region": game_region,
        "game_region_names": _flags(game_region, REGION_FLAGS),
        "game_region_unknown_bits": _unknown_flag_bits(game_region, REGION_FLAGS),
        "ratings": _u32(data, certificate_offset + 0xA4),
        "disc_number": _u32(data, certificate_offset + 0xA8),
        "version": _u32(data, certificate_offset + 0xAC),
    }

    return {
        "format": "xbe",
        "header": header,
        "certificate": certificate,
        "sections": sections,
    }


def parse_xbe_file(path: Path) -> dict[str, Any]:
    return parse_xbe_bytes(path.read_bytes())


def main() -> int:
    parser = argparse.ArgumentParser(description="Emit metadata for an Xbox XBE file.")
    parser.add_argument("xbe", type=Path, help="Path to the XBE file to inspect.")
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    args = parser.parse_args()

    info = parse_xbe_file(args.xbe)
    print(json.dumps(info, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
