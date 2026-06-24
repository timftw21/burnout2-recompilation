from __future__ import annotations

import hashlib
import struct
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.xbe.xbe_info import XbeFormatError, parse_xbe_bytes


def _put_u16(data: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<H", data, offset, value)


def _put_u32(data: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<I", data, offset, value)


def _put_ascii(data: bytearray, offset: int, text: str) -> None:
    raw = text.encode("ascii") + b"\x00"
    data[offset : offset + len(raw)] = raw


def _put_fixed_utf16(data: bytearray, offset: int, text: str, size: int) -> None:
    raw = text.encode("utf-16-le")
    data[offset : offset + size] = b"\x00" * size
    data[offset : offset + len(raw)] = raw


def _put_wide_string(data: bytearray, offset: int, text: str) -> None:
    raw = text.encode("utf-16-le") + b"\x00\x00"
    data[offset : offset + len(raw)] = raw


def _put_library_version(
    data: bytearray,
    offset: int,
    name: str,
    major: int,
    minor: int,
    build: int,
    flags: int,
) -> None:
    encoded_name = name.encode("ascii")[:8].ljust(8, b"\x00")
    struct.pack_into("<8sHHHH", data, offset, encoded_name, major, minor, build, flags)


def _section_digest(data: bytearray, raw_address: int, raw_size: int) -> bytes:
    raw = bytes(data[raw_address : raw_address + raw_size])
    return hashlib.sha1(struct.pack("<I", raw_size) + raw).digest()


def _put_section_header(
    data: bytearray,
    offset: int,
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


def _synthetic_xbe(*, corrupt_digest: bool = False) -> tuple[bytes, dict[str, int]]:
    base = 0x00010000
    headers_size = 0x1000
    image_size = 0x5000
    text_va = base + 0x1000
    text_raw = 0x1000
    text_size = 0x400
    data_va = base + 0x2000
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
    data[0:4] = b"XBEH"
    data[text_raw : text_raw + text_size] = bytes(index % 251 for index in range(text_size))
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
    _put_u32(data, 0x0128, (text_va + 0x20) ^ 0xA8FC57AB)
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
    _put_u32(data, cert_offset + 0x00, 0x1EC)
    _put_u32(data, cert_offset + 0x04, 1_704_067_200)
    _put_u32(data, cert_offset + 0x08, 0x41430019)
    _put_fixed_utf16(data, cert_offset + 0x0C, "Burnout 2", 0x50)
    _put_u32(data, cert_offset + 0x9C, 0x10)
    _put_u32(data, cert_offset + 0xA0, 0x01)
    _put_u32(data, cert_offset + 0xA8, 1)
    _put_u32(data, cert_offset + 0xAC, 3)
    _put_u32(data, cert_offset + 0x1D0, 0x1D0)
    _put_u32(data, cert_offset + 0x1D4, 0x1234)
    _put_u32(data, cert_offset + 0x1D8, 0x02)

    _put_ascii(data, text_name_addr - base, ".text")
    _put_ascii(data, data_name_addr - base, ".data")
    _put_library_version(data, lib_versions_addr - base, "XBOXKRNL", 1, 0, 5838, 0x6001)
    _put_library_version(data, lib_versions_addr - base + 0x10, "XAPILIB", 1, 0, 5838, 0x6002)
    _put_library_version(data, lib_features_addr - base, "D3D8", 1, 0, 5838, 0x0004)
    _put_wide_string(data, import_name_addr - base, "xapilib.dll")
    _put_ascii(data, debug_path_addr - base, "D:\\build\\burnout2.exe")
    _put_ascii(data, debug_filename_addr - base, "burnout2.exe")
    _put_wide_string(data, debug_unicode_filename_addr - base, "burnout2.exe")

    _put_u32(data, text_raw + 0x100, 0x80000042)
    _put_u32(data, text_raw + 0x104, 0x80000008)
    _put_u32(data, text_raw + 0x108, 0)

    _put_u32(data, data_raw + 0x20, data_va + 0x100)
    _put_u32(data, data_raw + 0x24, data_va + 0x120)
    _put_u32(data, data_raw + 0x28, data_va + 0x40)
    _put_u32(data, data_raw + 0x2C, tls_callbacks_addr)
    _put_u32(data, data_raw + 0x30, 0x10)
    _put_u32(data, data_raw + 0x34, 0)
    _put_u32(data, data_raw + 0x60, text_va + 0x24)
    _put_u32(data, data_raw + 0x64, 0)

    _put_u32(data, import_dir_addr - base, non_kernel_thunks_addr)
    _put_u32(data, import_dir_addr - base + 0x04, import_name_addr)
    _put_u32(data, import_dir_addr - base + 0x08, 0)
    _put_u32(data, import_dir_addr - base + 0x0C, 0)
    _put_u32(data, data_raw + 0x80, 0x80000002)
    _put_u32(data, data_raw + 0x84, 0x00000003)
    _put_u32(data, data_raw + 0x88, 0)

    section_header_offset = section_headers_addr - base
    _put_section_header(
        data,
        section_header_offset,
        0x06,
        text_va,
        text_size,
        text_raw,
        text_size,
        text_name_addr,
    )
    _put_section_header(
        data,
        section_header_offset + 0x38,
        0x03,
        data_va,
        data_virtual_size,
        data_raw,
        data_raw_size,
        data_name_addr,
    )

    if corrupt_digest:
        data[text_raw + 0x10] ^= 0xFF

    return bytes(data), {
        "base": base,
        "cert_addr": cert_addr,
        "data_va": data_va,
        "data_raw_size": data_raw_size,
        "data_virtual_size": data_virtual_size,
        "kernel_thunk_addr": kernel_thunk_addr,
        "section_headers_addr": section_headers_addr,
        "text_va": text_va,
        "tls_addr": tls_addr,
    }


class XbeInfoTests(unittest.TestCase):
    def test_parses_header_certificate_sections_memory_and_digests(self) -> None:
        blob, layout = _synthetic_xbe()
        info = parse_xbe_bytes(blob)

        self.assertEqual(info["certificate"]["title_name"], "Burnout 2")
        self.assertEqual(info["certificate"]["title_id"]["hex"], "41430019")
        self.assertEqual(info["certificate"]["title_id"]["publisher_code"], "AC")
        self.assertEqual(info["certificate"]["allowed_media_names"], ["DVD_5_RO"])
        self.assertEqual(info["certificate"]["game_region_names"], ["NA"])
        self.assertEqual(info["certificate"]["extended"]["online_service_id"], 0x1234)
        self.assertEqual(info["sections"][0]["name"], ".text")
        self.assertEqual(info["sections"][1]["name"], ".data")
        self.assertEqual(info["sections"][1]["zero_fill_size"], 0x300)
        self.assertTrue(info["sections"][0]["digest_verification"]["matches"])
        self.assertTrue(info["sections"][1]["digest_verification"]["matches"])
        self.assertEqual(info["memory_map"]["zero_fill_total"], 0x300)
        data_region = next(
            region for region in info["memory_map"]["regions"] if region["name"] == ".data"
        )
        self.assertEqual(data_region["zero_fill_size"], 0x300)
        self.assertEqual(info["header"]["init_flag_names"], ["MOUNT_UTILITY_DRIVE", "LIMIT_64_MB"])
        self.assertEqual(info["header"]["entry_point"]["selected"]["key"], "retail")
        self.assertEqual(
            info["header"]["kernel_thunk_address"]["selected"]["value"],
            layout["kernel_thunk_addr"],
        )

    def test_parses_tls_libraries_library_features_and_imports(self) -> None:
        blob, layout = _synthetic_xbe()
        info = parse_xbe_bytes(blob)

        self.assertEqual(info["tls"]["virtual_address"], layout["tls_addr"])
        self.assertEqual(info["tls"]["callback_addresses"], [layout["text_va"] + 0x24])
        self.assertEqual(info["libraries"]["versions"][0]["name"], "XBOXKRNL")
        self.assertEqual(info["libraries"]["kernel"]["name"], "XBOXKRNL")
        self.assertEqual(info["libraries"]["xapi"]["name"], "XAPILIB")
        self.assertEqual(info["library_features"][0]["name"], "D3D8")
        self.assertEqual(info["kernel_imports"]["count"], 2)
        self.assertEqual(info["kernel_imports"]["imports"][0]["ordinal"], 0x42)
        self.assertEqual(info["kernel_imports"]["imports"][0]["name"], "IoCreateDevice")
        self.assertEqual(info["kernel_imports"]["imports"][1]["ordinal"], 0x08)
        self.assertEqual(info["kernel_imports"]["imports"][1]["name"], "DbgPrint")
        self.assertEqual(info["non_kernel_imports"][0]["image_name"], "xapilib.dll")
        self.assertEqual(info["non_kernel_imports"][0]["thunk_count"], 2)
        self.assertEqual(
            [thunk["ordinal"] for thunk in info["non_kernel_imports"][0]["thunks"]],
            [2, 3],
        )
        self.assertEqual(info["debug_strings"]["debug_filename"], "burnout2.exe")

    def test_reports_section_digest_mismatches(self) -> None:
        blob, _ = _synthetic_xbe(corrupt_digest=True)
        info = parse_xbe_bytes(blob)

        self.assertFalse(info["sections"][0]["digest_verification"]["matches"])
        self.assertTrue(info["sections"][1]["digest_verification"]["matches"])

    def test_rejects_zero_fill_when_file_backed_data_is_required(self) -> None:
        blob, layout = _synthetic_xbe()
        data = bytearray(blob)
        zero_fill_certificate_addr = layout["data_va"] + layout["data_raw_size"] + 0x20
        _put_u32(data, 0x0118, zero_fill_certificate_addr)

        with self.assertRaisesRegex(XbeFormatError, "zero-fill"):
            parse_xbe_bytes(bytes(data))

    def test_rejects_malformed_section_header_range(self) -> None:
        blob, layout = _synthetic_xbe()
        data = bytearray(blob)
        _put_u32(data, 0x0120, layout["base"] + 0x0FF0)

        with self.assertRaisesRegex(XbeFormatError, "not inside the XBE header"):
            parse_xbe_bytes(bytes(data))

    def test_rejects_non_xbe_magic(self) -> None:
        with self.assertRaises(XbeFormatError):
            parse_xbe_bytes(b"not an xbe")


if __name__ == "__main__":
    unittest.main()
