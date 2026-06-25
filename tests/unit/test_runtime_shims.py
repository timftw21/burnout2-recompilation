from __future__ import annotations

import hashlib
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
    XboxRuntimeError,
    XboxRuntimeShims,
    XboxStatus,
)


class RuntimeShimTests(unittest.TestCase):
    def test_registers_kernel_shims_with_loader_import_resolver(self) -> None:
        blob, layout = _synthetic_xbe()
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()

        runtime.register_kernel_imports(resolver, imported_ordinals=[0x41, 0x08])
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

    def test_rejects_unmodeled_kernel_imports(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()

        with self.assertRaisesRegex(XboxRuntimeError, "no behavior model"):
            runtime.register_kernel_imports(resolver, imported_ordinals=[8, 400])

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
        self.assertNotIn("stub", summary["registered_behavior_counts"])

    def test_filesystem_resolves_guest_paths_inside_extracted_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            save_root = root / "save"
            dashboard_root = root / "dashboard"
            cache_root = root / "cache"
            media = root / "MEDIA"
            media.mkdir()
            (dashboard_root / "XODash").mkdir(parents=True)
            (dashboard_root / "XODash" / "xonlinedash.xbe").write_bytes(b"dash")
            (media / "Intro.BIN").write_bytes(b"burnout" * 1024)
            runtime = XboxRuntimeShims(
                XboxRuntimeConfig(
                    extracted_disc_root=root,
                    save_data_root=save_root,
                    dashboard_data_root=dashboard_root,
                    cache_data_root=cache_root,
                )
            )

            link = runtime.nt_open_symbolic_link_object("\\??\\D:")
            self.assertEqual(link["status"], XboxStatus.SUCCESS)
            self.assertEqual(link["target"], "\\Device\\Cdrom0")
            queried_link = runtime.nt_query_symbolic_link_object(link["handle"])
            self.assertEqual(queried_link["status"], XboxStatus.SUCCESS)
            self.assertEqual(queried_link["target"], "\\Device\\Cdrom0")

            resolved = runtime.filesystem.resolve_guest_path("D:\\media\\intro.bin")
            self.assertEqual(resolved, media / "Intro.BIN")
            resolved_dash = runtime.filesystem.resolve_guest_path(
                "\\Device\\Harddisk0\\partition2\\XODash\\xonlinedash.xbe"
            )
            self.assertEqual(
                resolved_dash,
                dashboard_root / "XODash" / "xonlinedash.xbe",
            )
            missing_cache = runtime.filesystem.open_file("Y:\\xboxdash.xbe", "rb")
            self.assertEqual(missing_cache["status"], XboxStatus.NO_SUCH_FILE)
            self.assertEqual(missing_cache["root_kind"], "cache")
            opened = runtime.filesystem.open_file("\\Device\\Cdrom0\\MEDIA\\Intro.BIN")
            self.assertEqual(opened["status"], XboxStatus.SUCCESS)
            self.assertEqual(opened["root_kind"], "disc")
            read = runtime.filesystem.read_file(opened["handle"], 4)
            self.assertEqual(read["data"], b"burn")
            next_read = runtime.filesystem.read_file(opened["handle"], 3)
            self.assertEqual(next_read["data"], b"out")
            handle_info = runtime.filesystem.query_file_handle_information(opened["handle"])
            self.assertEqual(handle_info["status"], XboxStatus.SUCCESS)
            self.assertEqual(handle_info["size"], len(b"burnout" * 1024))
            self.assertEqual(handle_info["position"], 7)
            nt_handle_info = runtime.nt_query_information_file(opened["handle"])
            self.assertEqual(nt_handle_info["status"], XboxStatus.SUCCESS)
            self.assertEqual(nt_handle_info["size"], len(b"burnout" * 1024))
            set_position = runtime.nt_set_information_file(
                opened["handle"],
                {
                    "file_information_class": 14,
                    "payload": (2).to_bytes(8, "little"),
                },
            )
            self.assertEqual(set_position, XboxStatus.SUCCESS)
            positioned_read = runtime.filesystem.read_file(opened["handle"], 4)
            self.assertEqual(positioned_read["data"], b"rnou")
            file_summary = runtime.summary()["open_files"][0]
            self.assertTrue(file_summary["streaming"])
            self.assertEqual(file_summary["read_count"], 3)
            self.assertEqual(file_summary["bytes_read"], 11)

            denied = runtime.filesystem.open_file("D:\\MEDIA\\new.bin", "wb")
            self.assertEqual(denied["status"], XboxStatus.ACCESS_DENIED)
            save = runtime.filesystem.open_file(
                "\\Device\\Harddisk0\\Partition1\\UDATA\\41430019\\save.dat",
                "wb",
            )
            self.assertEqual(save["status"], XboxStatus.SUCCESS)
            written = runtime.filesystem.write_file(save["handle"], b"save")
            self.assertEqual(written["bytes_written"], 4)
            cache = runtime.filesystem.open_file("Y:\\temp\\cache.bin", "wb")
            self.assertEqual(cache["status"], XboxStatus.SUCCESS)
            self.assertEqual(cache["root_kind"], "cache")
            cache_written = runtime.filesystem.write_file(cache["handle"], b"cache")
            self.assertEqual(cache_written["bytes_written"], 5)
            save_summary = [
                item
                for item in runtime.summary()["open_files"]
                if item["guest_path"].endswith("save.dat")
            ][0]
            self.assertTrue(save_summary["save_data"])
            self.assertTrue((save_root / "UDATA" / "41430019" / "save.dat").is_file())
            cache_summary = [
                item
                for item in runtime.summary()["open_files"]
                if item["guest_path"].endswith("cache.bin")
            ][0]
            self.assertTrue(cache_summary["cache_data"])
            self.assertTrue((cache_root / "temp" / "cache.bin").is_file())

            with self.assertRaises(XboxPathError):
                runtime.filesystem.resolve_guest_path("D:\\..\\secret.bin")

    def test_unconfigured_cache_drive_is_not_mapped_to_disc_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "xboxdash.xbe").write_bytes(b"disc-dashboard")
            runtime = XboxRuntimeShims(XboxRuntimeConfig(extracted_disc_root=root))

            missing = runtime.filesystem.open_file("Y:\\xboxdash.xbe", "rb")

            self.assertEqual(missing["status"], XboxStatus.NO_SUCH_FILE)
            self.assertEqual(missing["root_kind"], "cache")

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
        self.assertIsNone(runtime.ke_initialize_timer_ex(0x5000, 1))
        timer_event = runtime.summary()["trace"][-1]
        self.assertEqual(timer_event["operation"], "initialize_timer")
        self.assertEqual(timer_event["details"]["timer_address"], 0x5000)
        self.assertEqual(timer_event["details"]["timer_type"], 1)
        self.assertFalse(runtime.ke_set_timer(0x5000, 0xB5659000, 0xFFFFFFCD, 0x5010))
        set_timer_event = runtime.summary()["trace"][-1]
        self.assertEqual(set_timer_event["operation"], "set_kernel_timer")
        self.assertEqual(set_timer_event["details"]["timer_address"], 0x5000)
        self.assertEqual(set_timer_event["details"]["dpc_address"], 0x5010)

        missing = runtime.ex_query_nonvolatile_setting(0x11, 0x6000, 0x6004, 4, 0x6008)
        self.assertEqual(missing["status"], XboxStatus.OBJECT_NAME_NOT_FOUND)
        present = runtime.ex_query_nonvolatile_setting(0x01, 0x6000, 0x6004, 4, 0x6008)
        self.assertEqual(present["status"], XboxStatus.SUCCESS)
        self.assertEqual(present["required_length"], 4)
        setting_event = runtime.summary()["trace"][-1]
        self.assertEqual(setting_event["operation"], "query_nonvolatile_setting")
        self.assertEqual(setting_event["details"]["value_address"], 0x6004)

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
        self.assertEqual(runtime.audio.submit_buffer(stream, b"\x00\x01" * 480), XboxStatus.SUCCESS)
        playback = runtime.audio.advance_playback(stream, 5)
        self.assertGreater(playback["consumed_bytes"], 0)
        summary = runtime.summary()
        self.assertTrue(summary["audio_initialized"])
        self.assertEqual(summary["input"]["ports"]["0"]["poll_count"], 1)
        self.assertEqual(summary["input"]["ports"]["0"]["last_latency_samples"], 1)
        self.assertEqual(summary["audio_streams"][0]["submitted_buffer_count"], 1)
        self.assertGreater(summary["audio_streams"][0]["played_bytes"], 0)
        self.assertEqual(summary["determinism"]["input"], "sequenced_snapshots")
        self.assertGreaterEqual(len(summary["trace"]), 4)

    def test_crypto_shims_model_stateful_rc4_and_xbox_hmac(self) -> None:
        runtime = XboxRuntimeShims()

        rc4_key = runtime.xc_rc4_key(b"Key")
        self.assertEqual(
            runtime.xc_rc4_crypt(rc4_key, b"Plain") + runtime.xc_rc4_crypt(rc4_key, b"text"),
            bytes.fromhex("BBF316E8D940AF0AD3"),
        )

        key = bytes(range(80))
        left = b"burn"
        right = b"out"
        pad1 = bytearray(64)
        pad2 = bytearray(64)
        pad1[:] = key[:64]
        pad2[:] = key[:64]
        for index in range(64):
            pad1[index] ^= 0x36
            pad2[index] ^= 0x5C
        inner = hashlib.sha1(pad1 + left + right, usedforsecurity=False).digest()
        expected = hashlib.sha1(pad2 + inner, usedforsecurity=False).digest()

        self.assertEqual(runtime.xc_hmac(key, left, right), expected)

    def test_crypto_shims_model_des_and_3des_cbc(self) -> None:
        runtime = XboxRuntimeShims()
        key = bytes.fromhex("133457799BBCDFF1")
        plaintext = bytes.fromhex("0123456789ABCDEF")
        expected_des = bytes.fromhex("85E813540F0AB405")

        des_table = runtime.xc_key_table(0, key)
        self.assertEqual(
            runtime.xc_block_crypt_cbc(des_table, b"\x00" * 8, plaintext, 1),
            expected_des,
        )
        self.assertEqual(
            runtime.xc_block_crypt_cbc(des_table, b"\x00" * 8, expected_des, 0),
            plaintext,
        )

        degenerate_3des = runtime.xc_key_table(1, key + key)
        self.assertEqual(
            runtime.xc_block_crypt_cbc(degenerate_3des, b"\x00" * 8, plaintext, 1),
            expected_des,
        )

        triple_table = runtime.xc_key_table(
            1, bytes.fromhex("0123456789ABCDEFFEDCBA9876543210")
        )
        message = b"Burnout2Burnout2"
        encrypted = runtime.xc_block_crypt_cbc(triple_table, b"\x12" * 8, message, 1)
        self.assertNotEqual(encrypted, message)
        self.assertEqual(
            runtime.xc_block_crypt_cbc(triple_table, b"\x12" * 8, encrypted, 0),
            message,
        )

    def test_crypto_shims_verify_pkcs1_sha1_signatures(self) -> None:
        runtime = XboxRuntimeShims()
        digest = hashlib.sha1(b"burnout2", usedforsecurity=False).digest()
        der_prefix = bytes.fromhex("3021300906052B0E03021A05000414")
        size = 64
        encoded = (
            b"\x00\x01"
            + b"\xFF" * (size - len(der_prefix) - len(digest) - 3)
            + b"\x00"
            + der_prefix
            + digest
        )
        public_key = {"modulus": (1 << (size * 8)) - 1, "exponent": 1, "size": size}

        self.assertTrue(runtime.xc_verify_pkcs1_signature(encoded, public_key, digest))
        self.assertFalse(runtime.xc_verify_pkcs1_signature(encoded, public_key, b"\x00" * 20))

    def test_crypto_shims_verify_xbox_rsa1_sha1_signatures(self) -> None:
        runtime = XboxRuntimeShims()
        digest = hashlib.sha1(b"burnout2-xbox", usedforsecurity=False).digest()
        prefix = bytes.fromhex("140400051A02030E2B050609302130")
        decrypted = bytearray(256)
        decrypted[:20] = digest[::-1]
        decrypted[20 : 20 + len(prefix)] = prefix
        zero_position = 20 + len(prefix)
        decrypted[zero_position] = 0
        decrypted[zero_position + 1 : 254] = b"\xFF" * (253 - zero_position)
        decrypted[254] = 1
        decrypted[255] = 0
        public_key = (
            b"RSA1"
            + (264).to_bytes(4, "little")
            + (2048).to_bytes(4, "little")
            + (255).to_bytes(4, "little")
            + (1).to_bytes(4, "little")
            + (b"\xFF" * 256)
            + (b"\x00" * 8)
        )

        self.assertTrue(runtime.xc_verify_pkcs1_signature(bytes(decrypted), public_key, digest))
        self.assertFalse(
            runtime.xc_verify_pkcs1_signature(bytes(decrypted), public_key, b"\x01" * 20)
        )

    def test_crypto_shims_model_little_endian_mod_exp(self) -> None:
        runtime = XboxRuntimeShims()

        self.assertEqual(
            runtime.xc_mod_exp(
                (5).to_bytes(4, "little"),
                (3).to_bytes(4, "little"),
                (13).to_bytes(4, "little"),
                4,
            ),
            (8).to_bytes(4, "little"),
        )


if __name__ == "__main__":
    unittest.main()
