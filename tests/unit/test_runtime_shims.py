from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.unit.test_xbe_info import _synthetic_xbe
from tools.loader.xbe_loader import ImportResolver, load_xbe_bytes
from tools.runtime.runtime_smoke import build_runtime_smoke_summary
from runtime.xbox.shims import (
    ControllerState,
    XboxPathError,
    XboxRuntimeConfig,
    XboxRuntimeShims,
    XboxStatus,
)


class RuntimeShimTests(unittest.TestCase):
    def test_registers_kernel_shims_with_loader_import_resolver(self) -> None:
        blob, layout = _synthetic_xbe()
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()

        runtime.register_kernel_imports(resolver, imported_ordinals=[0x42, 0x08])
        loaded = load_xbe_bytes(blob, resolver=resolver)

        self.assertEqual(
            loaded.arena.read_u32(layout["kernel_thunk_addr"]),
            runtime.registered_shims[1].target_address,
        )
        self.assertEqual(
            loaded.arena.read_u32(layout["kernel_thunk_addr"] + 4),
            runtime.registered_shims[0].target_address,
        )
        kernel_resolutions = [
            resolution for resolution in loaded.import_resolutions if resolution.namespace == "kernel"
        ]
        self.assertTrue(all(resolution.resolved for resolution in kernel_resolutions))
        self.assertEqual(runtime.summary()["registered_kernel_shim_count"], 2)

    def test_registers_placeholder_shims_for_unknown_imported_ordinals(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()

        shims = runtime.register_kernel_imports(
            resolver, imported_ordinals=[8, 400], include_placeholders=True
        )

        by_ordinal = {shim.ordinal: shim for shim in shims}
        self.assertEqual(by_ordinal[8].behavior, "implemented")
        self.assertEqual(by_ordinal[400].behavior, "stub")
        self.assertEqual(by_ordinal[400].name, "ordinal_0400")

    def test_runtime_smoke_summary_uses_registered_shims(self) -> None:
        blob, _ = _synthetic_xbe()
        with tempfile.TemporaryDirectory() as temp_dir:
            xbe_path = Path(temp_dir) / "default.xbe"
            xbe_path.write_bytes(blob)

            summary = build_runtime_smoke_summary(xbe_path)

        self.assertEqual(summary["format"], "b2-recomp-runtime-smoke")
        self.assertEqual(summary["source"]["kernel_import_count"], 2)
        self.assertEqual(summary["registered_kernel_shim_count"], 2)
        self.assertEqual(summary["unresolved_import_count"], 2)

    def test_filesystem_resolves_guest_paths_inside_extracted_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            media = root / "MEDIA"
            media.mkdir()
            (media / "Intro.BIN").write_bytes(b"burnout")
            runtime = XboxRuntimeShims(XboxRuntimeConfig(extracted_disc_root=root))

            resolved = runtime.filesystem.resolve_guest_path("D:\\media\\intro.bin")
            self.assertEqual(resolved, media / "Intro.BIN")
            opened = runtime.filesystem.open_file("\\Device\\Cdrom0\\MEDIA\\Intro.BIN")
            self.assertEqual(opened["status"], XboxStatus.SUCCESS)
            read = runtime.filesystem.read_file(opened["handle"], 4)
            self.assertEqual(read["data"], b"burn")

            with self.assertRaises(XboxPathError):
                runtime.filesystem.resolve_guest_path("D:\\..\\secret.bin")

    def test_memory_allocations_are_deterministic_and_bounds_checked(self) -> None:
        runtime = XboxRuntimeShims()

        address = runtime.memory.allocate_pool(16, tag=0x54455354)
        runtime.memory.write(address, b"abcd")
        self.assertEqual(runtime.memory.read(address, 4), b"abcd")
        runtime.rtl_zero_memory(address + 1, 2)
        self.assertEqual(runtime.memory.read(address, 4), b"a\x00\x00d")
        query = runtime.memory.query(address)
        self.assertEqual(query["status"], XboxStatus.SUCCESS)
        self.assertEqual(query["size"], 16)
        self.assertEqual(runtime.memory.free(address), XboxStatus.SUCCESS)
        self.assertEqual(runtime.memory.query(address)["status"], XboxStatus.INVALID_PARAMETER)

    def test_clock_and_synchronization_are_deterministic(self) -> None:
        runtime = XboxRuntimeShims()

        start_counter = runtime.clock.query_performance_counter()
        runtime.ke_delay_execution_thread(-10_000)
        self.assertGreater(runtime.clock.query_performance_counter(), start_counter)

        event = runtime.nt_create_event(manual_reset=False, initial_state=False)
        self.assertEqual(
            runtime.nt_wait_for_single_object(event, timeout_100ns=0),
            XboxStatus.WAIT_TIMEOUT,
        )
        self.assertEqual(runtime.ke_set_event(event), XboxStatus.SUCCESS)
        self.assertEqual(runtime.nt_wait_for_single_object(event), XboxStatus.WAIT_0)
        self.assertEqual(
            runtime.nt_wait_for_single_object(event, timeout_100ns=0),
            XboxStatus.WAIT_TIMEOUT,
        )

        semaphore = runtime.nt_create_semaphore(initial_count=1, limit=2)
        self.assertEqual(runtime.nt_wait_for_single_object(semaphore), XboxStatus.WAIT_0)
        self.assertEqual(
            runtime.nt_wait_for_single_object(semaphore, timeout_100ns=0),
            XboxStatus.WAIT_TIMEOUT,
        )
        self.assertEqual(
            runtime.nt_release_semaphore(semaphore, 2)["previous_count"],
            0,
        )

    def test_input_graphics_and_audio_shims_record_state(self) -> None:
        runtime = XboxRuntimeShims()

        state = ControllerState(connected=True, buttons=0x1000, left_trigger=64)
        runtime.input.set_controller_state(0, state)
        self.assertEqual(runtime.input.poll_controller(0), state)

        self.assertEqual(runtime.av_set_display_mode(1280, 720, 32, 60, 0), XboxStatus.SUCCESS)
        self.assertEqual(runtime.graphics.display_mode.width, 1280)
        gpu_address = runtime.mm_claim_gpu_instance_memory(0x2000)
        self.assertEqual(runtime.memory.query(gpu_address)["kind"], "gpu")

        stream = runtime.audio.create_stream("pcm16")
        self.assertEqual(runtime.audio.submit_buffer(stream, b"\x00\x01"), XboxStatus.SUCCESS)
        summary = runtime.summary()
        self.assertTrue(summary["audio_initialized"])
        self.assertGreaterEqual(len(summary["trace"]), 4)


if __name__ == "__main__":
    unittest.main()
