from __future__ import annotations

import unittest

from tests.unit.test_xbe_info import _synthetic_xbe
from tools.loader.xbe_loader import (
    ImportResolver,
    XbeImportResolutionError,
    XbeLoaderError,
    XbeMemoryAccessError,
    load_xbe_bytes,
)


class XbeLoaderTests(unittest.TestCase):
    def test_maps_headers_sections_and_zero_fill_into_arena(self) -> None:
        blob, layout = _synthetic_xbe()
        loaded = load_xbe_bytes(blob, resolve_imports=False)

        self.assertEqual(loaded.arena.read(layout["base"], 4), b"XBEH")
        self.assertEqual(
            loaded.arena.read(layout["text_va"], 16),
            bytes(index % 251 for index in range(16)),
        )
        zero_fill_start = layout["data_va"] + layout["data_raw_size"]
        zero_fill_size = layout["data_virtual_size"] - layout["data_raw_size"]
        self.assertEqual(
            loaded.arena.read(zero_fill_start, zero_fill_size),
            b"\x00" * zero_fill_size,
        )
        with self.assertRaises(XbeMemoryAccessError):
            loaded.arena.read(layout["text_va"] + 0x500, 4)

    def test_models_permissions_and_bounds_for_guest_memory_access(self) -> None:
        blob, layout = _synthetic_xbe()
        loaded = load_xbe_bytes(blob, resolve_imports=False)

        with self.assertRaisesRegex(XbeMemoryAccessError, "not writable"):
            loaded.arena.write(layout["text_va"], b"\x90")

        loaded.arena.write(layout["data_va"], b"\x01\x02\x03\x04")
        self.assertEqual(loaded.arena.read(layout["data_va"], 4), b"\x01\x02\x03\x04")

        with self.assertRaisesRegex(XbeMemoryAccessError, "outside image"):
            loaded.arena.read(layout["base"] - 4, 4)

    def test_resolves_imports_with_registered_hooks_and_unresolved_stubs(self) -> None:
        blob, layout = _synthetic_xbe()
        resolver = ImportResolver()
        dbgprint_target = resolver.register_kernel(
            name="DbgPrint", target_address=0xE0000100
        )
        library_target = resolver.register_library(
            "XAPILIB.DLL", 2, target_address=0xE0000200
        )

        loaded = load_xbe_bytes(blob, resolver=resolver)

        self.assertEqual(loaded.arena.read_u32(layout["kernel_thunk_addr"]), 0xF0000000)
        self.assertEqual(
            loaded.arena.read_u32(layout["kernel_thunk_addr"] + 4), dbgprint_target
        )
        self.assertEqual(loaded.arena.read_u32(layout["data_va"] + 0x80), library_target)
        self.assertEqual(loaded.arena.read_u32(layout["data_va"] + 0x84), 0xF0000004)

        resolutions = [resolution.to_dict() for resolution in loaded.import_resolutions]
        self.assertEqual(len(resolutions), 4)
        self.assertEqual(sum(1 for item in resolutions if item["resolved"]), 2)
        self.assertEqual(sum(1 for item in resolutions if not item["resolved"]), 2)

    def test_can_fail_on_unresolved_imports(self) -> None:
        blob, _ = _synthetic_xbe()
        resolver = ImportResolver(stub_unresolved=False)

        with self.assertRaises(XbeImportResolutionError):
            load_xbe_bytes(
                blob,
                resolver=resolver,
                allow_unresolved_imports=False,
            )

    def test_can_require_matching_section_digests(self) -> None:
        blob, _ = _synthetic_xbe(corrupt_digest=True)

        with self.assertRaisesRegex(XbeLoaderError, "section digest mismatch"):
            load_xbe_bytes(blob, resolve_imports=False, require_valid_digests=True)

        loaded = load_xbe_bytes(blob, resolve_imports=False)
        digest_events = [
            event for event in loaded.trace.to_list() if event["phase"] == "digest"
        ]
        self.assertEqual(digest_events[0]["message"], "section digest mismatch detected")

    def test_trace_records_loader_initialization_order(self) -> None:
        blob, _ = _synthetic_xbe()
        loaded = load_xbe_bytes(blob)
        phases = [event["phase"] for event in loaded.trace.to_list()]

        self.assertLess(phases.index("parse"), phases.index("arena"))
        self.assertLess(phases.index("arena"), phases.index("map_headers"))
        self.assertLess(phases.index("map_headers"), phases.index("map_section"))
        self.assertLess(phases.index("map_section"), phases.index("resolve_kernel_imports"))
        self.assertLess(phases.index("resolve_kernel_imports"), phases.index("complete"))

        summary = loaded.summary()
        self.assertEqual(summary["mapped_region_count"], 3)
        self.assertEqual(summary["patched_import_count"], 4)
        self.assertEqual(summary["unresolved_import_count"], 4)


if __name__ == "__main__":
    unittest.main()
