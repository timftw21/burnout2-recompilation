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
    def test_reports_three_xbox_cache_partitions(self) -> None:
        runtime = XboxRuntimeShims()

        self.assertEqual(runtime.hal_disk_cache_partition_count(), 3)

    def test_kernel_key_exports_use_stable_validated_key_material(self) -> None:
        signature_key = bytes(range(16))
        runtime = XboxRuntimeShims(
            XboxRuntimeConfig(xbox_signature_key=signature_key)
        )

        self.assertNotEqual(runtime.kernel_variable("XboxHDKey"), b"\x00" * 16)
        self.assertEqual(runtime.kernel_variable("XboxSignatureKey"), signature_key)

        with self.assertRaisesRegex(XboxRuntimeError, "exactly 16 bytes"):
            XboxRuntimeShims(XboxRuntimeConfig(xbox_hd_key=b"short"))

    def test_nt_status_collision_maps_to_already_exists(self) -> None:
        runtime = XboxRuntimeShims()

        self.assertEqual(
            runtime.rtl_nt_status_to_dos_error(XboxStatus.OBJECT_NAME_COLLISION),
            183,
        )
        self.assertEqual(
            runtime.rtl_nt_status_to_dos_error(XboxStatus.OBJECT_NAME_NOT_FOUND),
            2,
        )

    def test_active_worker_owns_current_thread_termination(self) -> None:
        runtime = XboxRuntimeShims()
        primary = runtime.ke_get_current_thread()
        worker = runtime.ps_create_system_thread(start_address=0x00108D50)

        previous = runtime.sync.activate_thread(worker)
        status = runtime.ps_terminate_system_thread(0x1234)
        runtime.sync.activate_thread(previous)

        snapshots = {
            thread["handle"]: thread for thread in runtime.sync.thread_snapshot()
        }
        self.assertEqual(previous, primary)
        self.assertEqual(status, 0x1234)
        self.assertTrue(snapshots[worker]["suspended"])
        self.assertFalse(snapshots[primary]["suspended"])
        self.assertEqual(runtime.ke_get_current_thread(), primary)

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

    def test_page_lock_accepts_committed_tail_of_small_contiguous_allocation(self) -> None:
        runtime = XboxRuntimeShims()
        address = runtime.mm_allocate_contiguous_memory_ex(
            0x98,
            boundary_address_multiple=0x4000,
        )

        status = runtime.mm_lock_unlock_buffer_pages(address, 0x1000, True)

        self.assertEqual(status, XboxStatus.SUCCESS)
        self.assertEqual(runtime.memory.query_statistics()["locked_range_count"], 1)
        with self.assertRaisesRegex(XboxRuntimeError, "page range"):
            runtime.mm_lock_unlock_buffer_pages(address + 0x1000, 0x1000, True)

    def test_contiguous_allocation_honors_fixed_physical_range(self) -> None:
        runtime = XboxRuntimeShims()
        size = 0x5D000
        lowest_physical = 0x02877000

        address = runtime.mm_allocate_contiguous_memory_ex(
            size,
            lowest_acceptable_address=lowest_physical,
            highest_acceptable_address=lowest_physical + size - 1,
        )

        self.assertEqual(address, 0x82877000)
        self.assertEqual(runtime.mm_get_physical_address(address), lowest_physical)

    def test_contiguous_allocation_finds_space_in_broad_physical_range(self) -> None:
        runtime = XboxRuntimeShims()

        first = runtime.mm_allocate_contiguous_memory_ex(
            0x1800,
            highest_acceptable_address=0x003FFFFF,
        )
        second = runtime.mm_allocate_contiguous_memory_ex(
            0x1000,
            highest_acceptable_address=0x003FFFFF,
        )

        self.assertEqual(first, 0x80000000)
        self.assertEqual(second, 0x80002000)

    def test_filesystem_resolves_guest_paths_inside_extracted_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            save_root = root / "save"
            dashboard_root = root / "dashboard"
            cache_root = root / "cache"
            profile_root = save_root / "UDATA" / "41430019" / "Profile1"
            profile_root.mkdir(parents=True)
            (profile_root / "progress.bin").write_bytes(b"progress")
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
                    title_id=0x41430019,
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
            resolved_disc_root = runtime.filesystem.resolve_guest_path("\\Device\\CdRom0")
            self.assertEqual(resolved_disc_root, root)
            resolved_dash = runtime.filesystem.resolve_guest_path(
                "\\Device\\Harddisk0\\partition2\\XODash\\xonlinedash.xbe"
            )
            self.assertEqual(
                resolved_dash,
                dashboard_root / "XODash" / "xonlinedash.xbe",
            )
            raw_partition_path = "\\Device\\Harddisk0\\partition0"
            self.assertEqual(
                runtime.filesystem.resolve_guest_path(raw_partition_path),
                cache_root / "partition0.bin",
            )
            raw_partition = runtime.filesystem.open_file(
                raw_partition_path,
                "ab",
                create_disposition=1,
            )
            self.assertEqual(raw_partition["status"], XboxStatus.SUCCESS)
            self.assertEqual(raw_partition["root_kind"], "cache")
            self.assertEqual((cache_root / "partition0.bin").stat().st_size, 0xA00)
            self.assertEqual(
                runtime.filesystem.resolve_guest_path("U:\\"),
                save_root / "UDATA" / "41430019",
            )
            self.assertEqual(
                runtime.filesystem.resolve_guest_path("T:\\settings.bin"),
                save_root / "TDATA" / "41430019" / "settings.bin",
            )
            created_profile = runtime.filesystem.open_directory(
                "U:\\Profile2",
                create_disposition=3,
            )
            self.assertEqual(created_profile["status"], XboxStatus.SUCCESS)
            self.assertTrue(created_profile["created"])
            self.assertEqual(created_profile["io_information"], 2)
            self.assertTrue((save_root / "UDATA" / "41430019" / "Profile2").is_dir())
            reopened_profile = runtime.filesystem.open_directory(
                "U:\\Profile2",
                create_disposition=3,
            )
            self.assertEqual(reopened_profile["status"], XboxStatus.SUCCESS)
            self.assertFalse(reopened_profile["created"])
            self.assertEqual(reopened_profile["io_information"], 1)
            collision = runtime.filesystem.open_directory(
                "U:\\Profile2",
                create_disposition=2,
            )
            self.assertEqual(
                collision["status"],
                XboxStatus.OBJECT_NAME_COLLISION,
            )
            user_root = runtime.filesystem.open_file("U:\\", "rb")
            self.assertEqual(user_root["status"], XboxStatus.SUCCESS)
            self.assertEqual(user_root["root_kind"], "save")
            directory_entry = runtime.filesystem.query_directory_entry(
                user_root["handle"], pattern="*.*"
            )
            self.assertEqual(directory_entry["status"], XboxStatus.SUCCESS)
            self.assertEqual(directory_entry["name"], "Profile1")
            self.assertTrue(directory_entry["is_directory"])
            second_entry = runtime.filesystem.query_directory_entry(
                user_root["handle"]
            )
            self.assertEqual(second_entry["status"], XboxStatus.SUCCESS)
            self.assertEqual(second_entry["name"], "Profile2")
            self.assertEqual(
                runtime.filesystem.query_directory_entry(user_root["handle"])[
                    "status"
                ],
                XboxStatus.NO_MORE_FILES,
            )
            restarted_entry = runtime.filesystem.query_directory_entry(
                user_root["handle"], restart_scan=True
            )
            self.assertEqual(restarted_entry["name"], "Profile1")
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
            file_summary = next(
                item
                for item in runtime.summary()["open_files"]
                if item["guest_path"].endswith("Intro.BIN")
            )
            self.assertTrue(file_summary["streaming"])
            self.assertEqual(file_summary["read_count"], 3)
            self.assertEqual(file_summary["bytes_read"], 11)

            disc_root = runtime.filesystem.open_file("\\Device\\CdRom0", "rb")
            self.assertEqual(disc_root["status"], XboxStatus.SUCCESS)
            self.assertEqual(disc_root["root_kind"], "disc")
            self.assertTrue(disc_root["is_directory"])
            disc_root_info = runtime.filesystem.query_file_handle_information(
                disc_root["handle"]
            )
            self.assertEqual(disc_root_info["status"], XboxStatus.SUCCESS)
            self.assertTrue(disc_root_info["is_directory"])
            self.assertEqual(disc_root_info["size"], 0)
            disc_volume = runtime.filesystem.query_volume_handle_information(
                disc_root["handle"]
            )
            self.assertEqual(disc_volume["status"], XboxStatus.SUCCESS)
            self.assertEqual(disc_volume["root_kind"], "disc")
            nt_disc_volume = runtime.nt_query_volume_information_file(
                disc_root["handle"]
            )
            self.assertEqual(nt_disc_volume["status"], XboxStatus.SUCCESS)
            self.assertEqual(nt_disc_volume["root_kind"], "disc")
            nt_disc_directory = runtime.nt_query_directory_file(
                disc_root["handle"]
            )
            self.assertEqual(nt_disc_directory["status"], XboxStatus.SUCCESS)
            self.assertIn("MEDIA", nt_disc_directory["entries"])
            self.assertEqual(
                runtime.nt_query_directory_file(0xDEADBEEF)["status"],
                XboxStatus.INVALID_HANDLE,
            )
            directory_read = runtime.filesystem.read_file(disc_root["handle"], 4)
            self.assertEqual(directory_read["status"], XboxStatus.ACCESS_DENIED)
            disc_root_summary = [
                item
                for item in runtime.summary()["open_files"]
                if item["guest_path"] == "\\Device\\CdRom0"
            ][0]
            self.assertTrue(disc_root_summary["is_directory"])
            self.assertFalse(disc_root_summary["streaming"])

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
            runtime.nt_wait_for_single_object_ex(semaphore, 1, 0, 0),
            XboxStatus.WAIT_TIMEOUT,
        )
        self.assertEqual(
            runtime.ke_wait_for_single_object(0x0022704C, 6, 1, 0, 0),
            XboxStatus.WAIT_0,
        )
        kernel_wait_event = runtime.summary()["trace"][-1]
        self.assertEqual(kernel_wait_event["operation"], "wait_kernel_single")
        self.assertEqual(
            kernel_wait_event["details"]["object_address"],
            0x0022704C,
        )
        self.assertEqual(
            runtime.nt_release_semaphore(semaphore, 2)["previous_count"],
            0,
        )
        worker = runtime.ps_create_system_thread(
            start_address=0x00108D50,
            parameter=0x1234,
        )
        self.assertEqual(runtime.nt_close(worker), XboxStatus.SUCCESS)
        self.assertIn(
            worker,
            {thread["handle"] for thread in runtime.sync.thread_snapshot()},
        )
        self.assertNotIn(worker, runtime.handles._objects)
        invalid_release = runtime.nt_release_semaphore(0, 1)
        self.assertEqual(invalid_release["status"], XboxStatus.INVALID_HANDLE)
        self.assertEqual(invalid_release["previous_count"], 0)
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

        class RecordingAudioOutput:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self.closed = False

            def submit_pcm(self, payload: bytes, **format_fields: object) -> bool:
                self.calls.append({"payload": payload, **format_fields})
                return True

            def close(self) -> None:
                self.closed = True

        output = RecordingAudioOutput()
        runtime.audio.set_output_backend(output)

        state = ControllerState(connected=True, buttons=0x1000, left_trigger=64)
        runtime.input.set_controller_state(0, state)
        self.assertEqual(runtime.input.poll_controller(0), state)

        self.assertEqual(
            runtime.av_set_display_mode(
                0xFD000000,
                0,
                0x040F0D0F,
                0x12,
                1280 * 4,
                0x21D8D000,
            ),
            XboxStatus.SUCCESS,
        )
        self.assertEqual(runtime.graphics.display_mode.width, 1280)
        gpu_address = runtime.mm_claim_gpu_instance_memory(0x2000)
        self.assertEqual(runtime.memory.query(gpu_address)["kind"], "gpu")

        stream = runtime.audio.create_stream("pcm16")
        self.assertEqual(runtime.audio.submit_buffer(stream, b"\x00\x01" * 480), XboxStatus.SUCCESS)
        playback = runtime.audio.advance_playback(stream, 5)
        self.assertGreater(playback["consumed_bytes"], 0)
        summary = runtime.summary()
        self.assertTrue(summary["audio_initialized"])
        runtime.audio.close_output_backend()
        self.assertTrue(output.closed)
        self.assertEqual(summary["input"]["ports"]["0"]["poll_count"], 1)
        self.assertEqual(summary["input"]["ports"]["0"]["last_latency_samples"], 1)
        self.assertEqual(summary["audio_streams"][0]["submitted_buffer_count"], 1)
        self.assertEqual(summary["audio_streams"][0]["host_submitted_buffer_count"], 1)
        self.assertEqual(len(output.calls), 1)
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
