from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.xbe.xbe_info import XbeFormatError, parse_xbe_bytes


def _put_u32(data: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<I", data, offset, value)


def _synthetic_xbe() -> bytes:
    base = 0x00010000
    headers_size = 0x1000
    image_size = 0x4000
    entry = base + 0x1200
    kernel_thunk = base + 0x2100
    cert_addr = base + 0x0200
    section_headers_addr = base + 0x0400
    section_name_addr = base + 0x0500
    raw_addr = 0x1000

    data = bytearray(0x2000)
    data[0:4] = b"XBEH"
    _put_u32(data, 0x0104, base)
    _put_u32(data, 0x0108, headers_size)
    _put_u32(data, 0x010C, image_size)
    _put_u32(data, 0x0110, 0x178)
    _put_u32(data, 0x0114, 1_704_067_200)
    _put_u32(data, 0x0118, cert_addr)
    _put_u32(data, 0x011C, 1)
    _put_u32(data, 0x0120, section_headers_addr)
    _put_u32(data, 0x0128, entry ^ 0xA8FC57AB)
    _put_u32(data, 0x0158, kernel_thunk ^ 0x5B6D40B6)

    cert_offset = cert_addr - base
    _put_u32(data, cert_offset + 0x00, 0x1D0)
    _put_u32(data, cert_offset + 0x04, 1_704_067_200)
    _put_u32(data, cert_offset + 0x08, 0x45410017)
    title = "Burnout 2".encode("utf-16-le")
    data[cert_offset + 0x0C : cert_offset + 0x0C + len(title)] = title
    _put_u32(data, cert_offset + 0x9C, 0x10)
    _put_u32(data, cert_offset + 0xA0, 0x01)
    _put_u32(data, cert_offset + 0xA8, 1)
    _put_u32(data, cert_offset + 0xAC, 3)

    section_header_offset = section_headers_addr - base
    struct.pack_into(
        "<IIIIIIIII20s",
        data,
        section_header_offset,
        0x06,
        base + 0x1000,
        0x1000,
        raw_addr,
        0x1000,
        section_name_addr,
        1,
        0,
        0,
        bytes.fromhex("11" * 20),
    )
    data[section_name_addr - base : section_name_addr - base + 6] = b".text\0"
    return bytes(data)


class XbeInfoTests(unittest.TestCase):
    def test_parses_header_certificate_and_section_metadata(self) -> None:
        info = parse_xbe_bytes(_synthetic_xbe())

        self.assertEqual(info["certificate"]["title_name"], "Burnout 2")
        self.assertEqual(info["certificate"]["title_id"]["hex"], "45410017")
        self.assertEqual(info["certificate"]["title_id"]["publisher_code"], "EA")
        self.assertEqual(info["certificate"]["allowed_media_names"], ["DVD_5_RO"])
        self.assertEqual(info["certificate"]["game_region_names"], ["NA"])
        self.assertEqual(info["sections"][0]["name"], ".text")
        self.assertEqual(info["sections"][0]["flag_names"], ["PRELOAD", "EXECUTABLE"])
        self.assertEqual(info["header"]["entry_point"]["selected"]["key"], "retail")
        self.assertEqual(
            info["header"]["kernel_thunk_address"]["selected"]["key"], "retail"
        )

    def test_rejects_non_xbe_magic(self) -> None:
        with self.assertRaises(XbeFormatError):
            parse_xbe_bytes(b"not an xbe")


if __name__ == "__main__":
    unittest.main()
