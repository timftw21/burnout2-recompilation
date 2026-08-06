"""Build a deterministic asset-free XBE fixture for offline validation."""

from __future__ import annotations

import hashlib
import struct
from typing import Any


def _put_u32(data: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<I", data, offset, value)


def _put_ascii(data: bytearray, offset: int, value: str) -> None:
    payload = value.encode("ascii") + b"\0"
    data[offset : offset + len(payload)] = payload


def _put_utf16(data: bytearray, offset: int, value: str, size: int | None = None) -> None:
    payload = value.encode("utf-16-le") + b"\0\0"
    if size is not None:
        data[offset : offset + size] = b"\0" * size
        payload = payload[:size]
    data[offset : offset + len(payload)] = payload


def _put_library_version(
    data: bytearray,
    offset: int,
    name: str,
    flags: int,
) -> None:
    struct.pack_into(
        "<8sHHHH",
        data,
        offset,
        name.encode("ascii")[:8].ljust(8, b"\0"),
        1,
        0,
        5838,
        flags,
    )


def _section_digest(data: bytearray, raw_address: int, raw_size: int) -> bytes:
    payload = bytes(data[raw_address : raw_address + raw_size])
    return hashlib.sha1(struct.pack("<I", raw_size) + payload).digest()


def _put_section(
    data: bytearray,
    offset: int,
    *,
    flags: int,
    virtual_address: int,
    virtual_size: int,
    raw_address: int,
    raw_size: int,
    name_address: int,
) -> None:
    struct.pack_into(
        "<IIIIIIIII20s",
        data,
        offset,
        flags,
        virtual_address,
        virtual_size,
        raw_address,
        raw_size,
        name_address,
        1,
        0,
        0,
        _section_digest(data, raw_address, raw_size),
    )


def build_synthetic_xbe(
    *,
    text_payload: bytes,
    text_payload_offset: int = 0x20,
    text_virtual_address: int = 0x000C0000,
    corrupt_digest: bool = False,
) -> tuple[bytes, dict[str, Any]]:
    """Return a valid two-section XBE with caller-provided title code."""

    if text_payload_offset < 0 or text_payload_offset + len(text_payload) > 0x100:
        raise ValueError("synthetic XBE title code must fit before its import table")
    base = 0x00010000
    headers_size = 0x1000
    text_va = int(text_virtual_address)
    data_va = text_va + 0x10000
    image_size = data_va - base + 0x5000
    text_raw = 0x1000
    text_size = 0x400
    data_raw = 0x2000
    data_raw_size = 0x200
    data_virtual_size = 0x500
    section_headers_addr = base + 0x0400
    text_name_addr = base + 0x0500
    data_name_addr = base + 0x0510
    cert_addr = base + 0x0200
    lib_versions_addr = base + 0x0600
    lib_features_addr = base + 0x0650
    import_dir_addr = base + 0x0680
    import_name_addr = base + 0x06A0
    debug_path_addr = base + 0x0720
    debug_filename_addr = base + 0x0760
    debug_unicode_filename_addr = base + 0x0780
    kernel_thunk_addr = text_va + 0x100
    tls_addr = data_va + 0x20
    tls_callbacks_addr = data_va + 0x60
    non_kernel_thunks_addr = data_va + 0x80

    data = bytearray(0x2800)
    data[:4] = b"XBEH"
    data[text_raw : text_raw + text_size] = b"\x90" * text_size
    data[text_raw + text_payload_offset : text_raw + text_payload_offset + len(text_payload)] = (
        text_payload
    )
    data[data_raw : data_raw + data_raw_size] = b"\xCC" * data_raw_size

    _put_u32(data, 0x0104, base)
    _put_u32(data, 0x0108, headers_size)
    _put_u32(data, 0x010C, image_size)
    _put_u32(data, 0x0110, 0x180)
    _put_u32(data, 0x0114, 1_704_067_200)
    _put_u32(data, 0x0118, cert_addr)
    _put_u32(data, 0x011C, 2)
    _put_u32(data, 0x0120, section_headers_addr)
    _put_u32(data, 0x0124, 0x05)
    _put_u32(data, 0x0128, (text_va + text_payload_offset) ^ 0xA8FC57AB)
    _put_u32(data, 0x012C, tls_addr)
    _put_u32(data, 0x0130, 0x30000)
    _put_u32(data, 0x0134, 0x100000)
    _put_u32(data, 0x0138, 0x1000)
    _put_u32(data, 0x013C, base)
    _put_u32(data, 0x0140, image_size)
    _put_u32(data, 0x0148, 1_704_067_100)
    _put_u32(data, 0x014C, debug_path_addr)
    _put_u32(data, 0x0150, debug_filename_addr)
    _put_u32(data, 0x0154, debug_unicode_filename_addr)
    _put_u32(data, 0x0158, kernel_thunk_addr ^ 0x5B6D40B6)
    _put_u32(data, 0x015C, import_dir_addr)
    _put_u32(data, 0x0160, 2)
    _put_u32(data, 0x0164, lib_versions_addr)
    _put_u32(data, 0x0168, lib_versions_addr)
    _put_u32(data, 0x016C, lib_versions_addr + 0x10)
    _put_u32(data, 0x0178, lib_features_addr)
    _put_u32(data, 0x017C, 1)

    cert_offset = cert_addr - base
    _put_u32(data, cert_offset, 0x1EC)
    _put_u32(data, cert_offset + 0x04, 1_704_067_200)
    _put_u32(data, cert_offset + 0x08, 0x41430019)
    _put_utf16(data, cert_offset + 0x0C, "Phase 2 Fixture", 0x50)
    _put_u32(data, cert_offset + 0x9C, 0x10)
    _put_u32(data, cert_offset + 0xA0, 0x01)
    _put_u32(data, cert_offset + 0xA8, 1)
    _put_u32(data, cert_offset + 0xAC, 3)
    _put_u32(data, cert_offset + 0x1D0, 0x1D0)
    _put_u32(data, cert_offset + 0x1D4, 0x1234)
    _put_u32(data, cert_offset + 0x1D8, 0x02)

    _put_ascii(data, text_name_addr - base, ".text")
    _put_ascii(data, data_name_addr - base, ".data")
    _put_library_version(data, lib_versions_addr - base, "XBOXKRNL", 0x6001)
    _put_library_version(data, lib_versions_addr - base + 0x10, "XAPILIB", 0x6002)
    _put_library_version(data, lib_features_addr - base, "D3D8", 0x0004)
    _put_utf16(data, import_name_addr - base, "xapilib.dll")
    _put_ascii(data, debug_path_addr - base, "D:\\build\\phase2.exe")
    _put_ascii(data, debug_filename_addr - base, "phase2.exe")
    _put_utf16(data, debug_unicode_filename_addr - base, "phase2.exe")

    _put_u32(data, text_raw + 0x100, 0x80000041)
    _put_u32(data, text_raw + 0x104, 0x80000008)
    _put_u32(data, text_raw + 0x108, 0)
    _put_u32(data, data_raw + 0x20, data_va + 0x100)
    _put_u32(data, data_raw + 0x24, data_va + 0x120)
    _put_u32(data, data_raw + 0x28, data_va + 0x40)
    _put_u32(data, data_raw + 0x2C, tls_callbacks_addr)
    _put_u32(data, data_raw + 0x30, 0x10)
    _put_u32(data, data_raw + 0x34, 0)
    _put_u32(data, data_raw + 0x60, text_va + text_payload_offset + 4)
    _put_u32(data, data_raw + 0x64, 0)
    _put_u32(data, import_dir_addr - base, non_kernel_thunks_addr)
    _put_u32(data, import_dir_addr - base + 0x04, import_name_addr)
    _put_u32(data, data_raw + 0x80, 0x80000002)
    _put_u32(data, data_raw + 0x84, 0x00000003)
    _put_u32(data, data_raw + 0x88, 0)

    section_offset = section_headers_addr - base
    _put_section(
        data,
        section_offset,
        flags=0x06,
        virtual_address=text_va,
        virtual_size=text_size,
        raw_address=text_raw,
        raw_size=text_size,
        name_address=text_name_addr,
    )
    _put_section(
        data,
        section_offset + 0x38,
        flags=0x03,
        virtual_address=data_va,
        virtual_size=data_virtual_size,
        raw_address=data_raw,
        raw_size=data_raw_size,
        name_address=data_name_addr,
    )
    if corrupt_digest:
        data[text_raw + text_payload_offset] ^= 0xFF
    return bytes(data), {
        "base": base,
        "entry_eip": text_va + text_payload_offset,
        "text_va": text_va,
        "text_payload_offset": text_payload_offset,
        "data_va": data_va,
        "section_headers_addr": section_headers_addr,
    }
