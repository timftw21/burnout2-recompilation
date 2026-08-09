from __future__ import annotations

import copy
import hashlib
import struct
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tools.playability.replay_capsule import cpu_state_record
from tools.recomp.x86_lifter import SparseMemory, X86Instruction, lift_x86_function
from tools.recomp.ia32_native_backend import (
    CUTOVER_EXCHANGE_VERSION,
    CUTOVER_MEMORY_MAGIC,
    DETACHED_HELPER_IMAGE_BASE,
    NORMAL_LIVE_BOOT_KERNEL_PROBE_SIZE,
    NORMAL_LIVE_BOOT_KERNEL_PROBE_START,
    NORMAL_LIVE_BOOT_TLS_BASE,
    NORMAL_LIVE_BOOT_TLS_DATA,
    NORMAL_LIVE_BOOT_TLS_VECTOR,
    NORMAL_LIVE_TLS_INDEX_ADDRESS,
    NORMAL_LIVE_BOOT_STACK_TOP,
    NORMAL_LIVE_BOOT_RETURN_THUNK_ADDRESS,
    NORMAL_LIVE_PRIMARY_STACK_TOP,
    NORMAL_LIVE_PRIMARY_THREAD_OBJECT,
    NORMAL_LIVE_PRIMARY_TLS_BASE,
    NORMAL_LIVE_PRIMARY_TLS_DATA,
    NORMAL_LIVE_PRIMARY_TLS_VECTOR,
    NORMAL_LIVE_RESUME_THUNK_ADDRESS,
    NORMAL_LIVE_VBLANK_CALLBACK_ADDRESS,
    NORMAL_LIVE_VBLANK_CONTEXT_GLOBAL,
    NORMAL_LIVE_VBLANK_DATA_ADDRESS,
    NORMAL_LIVE_VBLANK_EXIT_THUNK_ADDRESS,
    NORMAL_LIVE_VBLANK_SCRATCH_ADDRESS,
    NORMAL_LIVE_VBLANK_STACK_TOP,
    NORMAL_LIVE_VBLANK_START_THUNK_ADDRESS,
    NORMAL_LIVE_VBLANK_TLS_BASE,
    PHASE7_D3D_MMIO_PAGE,
    PHASE7_GPU_PFIFO_CACHE1_STATUS_ADDRESS,
    PHASE7_GPU_PFIFO_IDLE_BIT,
    PHASE7_GPU_PFIFO_RUNOUT_STATUS_ADDRESS,
    PHASE7_ZEROED_DIRECT_MMIO_PAGES,
    PHASE7_CUTOVER_CONTRACT_ID,
    SCRATCH_ADDRESS,
    THUNK_REGION,
    WORKER_COMMAND_SCHEDULE,
    IA32_VBLANK_RETURN_SENTINEL,
    Ia32BackendError,
    Ia32SliceArtifact,
    _HostServiceThunk,
    _initialize_normal_live_boot_kernel_probe,
    _initialize_normal_live_boot_stack,
    _initialize_normal_live_boot_tls,
    _initialize_normal_live_device_pages,
    _cutover_command_segments,
    _allocation_region_addresses,
    _host_service_allocation_region_addresses,
    _loaded_host_service_page_addresses,
    _mixed_code_page_data_spans,
    _mappable_data_page,
    _normal_live_host_data_pages,
    _phase7_c_source,
    _phase7_full_flip_oracle,
    _phase7_full_flip_oracle_page_addresses,
    _phase7_normal_live_c_source,
    _phase7_observed_memory_ranges,
    _phase7_observed_state_record,
    _phase7_replayable_service_trace,
    _phase7_static_instruction_closure,
    _resident_scheduler_plan,
    _validate_phase7_vblank_callback_family,
    ia32_launcher_cutover_contract,
    ia32_launcher_cutover_contract_id,
    ia32_normal_live_launch_contract,
    ia32_normal_live_launch_contract_id,
    run_ia32_full_flip_cutover,
    select_ia32_launcher_backend,
    validate_ia32_normal_live_launch_artifact,
)
from tools.recomp.ia32_proof_contract import build_asset_free_phase5_fixture


def _vblank_callback_family_instructions() -> dict[int, X86Instruction]:
    instructions: dict[int, X86Instruction] = {}
    for address, payload in (
        (0x000B86C0, "8B4424048B08890DFC185500C3"),
        (
            0x000B8D3D,
            "68C0860B00889E47B90600889E44B90600889E45B90600"
            "889E46B90600E821CB1500",
        ),
        (0x00215880, "8B4424048B0DB8562200898188190000C20400"),
    ):
        function = lift_x86_function(
            bytes.fromhex(payload),
            base_address=address,
            symbol=f"vblank_callback_family_{address:08x}",
        )
        instructions.update(
            (instruction.address, instruction) for instruction in function.instructions
        )
    return instructions


class Ia32LauncherCutoverTests(unittest.TestCase):
    def test_normal_live_control_thunks_do_not_overlap_service_bodies(self) -> None:
        addresses = (
            NORMAL_LIVE_BOOT_RETURN_THUNK_ADDRESS,
            NORMAL_LIVE_RESUME_THUNK_ADDRESS,
            NORMAL_LIVE_VBLANK_START_THUNK_ADDRESS,
            NORMAL_LIVE_VBLANK_EXIT_THUNK_ADDRESS,
        )

        self.assertEqual(len(set(addresses)), len(addresses))
        for address in addresses:
            self.assertGreaterEqual(address, THUNK_REGION + 0x2100)
            self.assertLess(address, THUNK_REGION + 0x3000)

    def test_normal_live_vblank_callback_family_is_finite(self) -> None:
        instructions = _vblank_callback_family_instructions()

        _validate_phase7_vblank_callback_family(instructions)

        drifted = dict(instructions)
        drifted[0x000B86C6] = replace(
            drifted[0x000B86C6],
            bytes_hex="890DF8185500",
        )
        extra_installer = dict(instructions)
        installer = lift_x86_function(
            b"\x68" + struct.pack("<I", NORMAL_LIVE_VBLANK_CALLBACK_ADDRESS),
            base_address=0x00300000,
            symbol="unexpected_vblank_installer",
        ).instructions[0]
        extra_installer[installer.address] = installer
        extra_caller = dict(instructions)
        caller_address = 0x00300100
        caller = lift_x86_function(
            b"\xE8"
            + struct.pack(
                "<i",
                NORMAL_LIVE_VBLANK_CALLBACK_ADDRESS - (caller_address + 5),
            ),
            base_address=caller_address,
            symbol="unexpected_vblank_direct_caller",
        ).instructions[0]
        extra_caller[caller.address] = caller
        cases = (
            (drifted, "vblank callback family drifted"),
            (extra_installer, "callback-installer family drifted"),
            (extra_caller, "unexpected direct caller"),
        )
        for candidate, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                Ia32BackendError,
                message,
            ):
                _validate_phase7_vblank_callback_family(candidate)

    def test_normal_live_materializes_only_allowlisted_device_pages(self) -> None:
        evidence = SparseMemory()
        evidence.write(0xFD100000, b"\x11" * 4096)
        evidence.write(0xFD800000, b"\x22" * 4096)
        evidence.write(PHASE7_D3D_MMIO_PAGE, b"\x33" * 4096)
        evidence.write(0xFEC00000, b"\x44" * 4096)
        memory = SparseMemory()

        _initialize_normal_live_device_pages(memory, evidence)

        self.assertEqual(memory.read(0xFD100000, 4), b"\x11" * 4)
        self.assertEqual(memory.read(0xFD800000, 4), b"\x22" * 4)
        self.assertEqual(memory.read(PHASE7_D3D_MMIO_PAGE, 4), b"\x33" * 4)
        for page in PHASE7_ZEROED_DIRECT_MMIO_PAGES:
            self.assertEqual(memory.read(page, 4), bytes(4))
        self.assertEqual(
            memory.read_u32(PHASE7_GPU_PFIFO_RUNOUT_STATUS_ADDRESS),
            PHASE7_GPU_PFIFO_IDLE_BIT,
        )
        self.assertEqual(
            memory.read_u32(PHASE7_GPU_PFIFO_CACHE1_STATUS_ADDRESS),
            PHASE7_GPU_PFIFO_IDLE_BIT,
        )
        self.assertEqual(memory.read_u32(0xFD003220), 0)
        self.assertTrue(
            set(range(0xFD700000, 0xFD705000, 0x1000)).issubset(
                PHASE7_ZEROED_DIRECT_MMIO_PAGES
            )
        )
        self.assertFalse(memory.has_allocated_page(0xFD926000))
        self.assertFalse(memory.has_allocated_page(0xFEC00000))

    def test_normal_live_materializes_kernel_data_exports_with_service_thunks(self) -> None:
        memory = SparseMemory()
        memory.write(0x0022D000, b"\x90" * 4096)
        memory.write_u32(0xE00000A0, 3)
        memory.write(0xE0000750, b"\xAA\xBB\xCC\xDD")
        services = (
            _HostServiceThunk(0x0022D896, "DirectSoundBufferPlay", 4, 0, 16),
            _HostServiceThunk(0xE00004F0, "NtOpenFile", 1, 0, 24),
            _HostServiceThunk(0xE0000560, "NtReadFile", 1, 0, 32),
        )

        pages = _normal_live_host_data_pages(memory, services)

        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0][0], 0xE0000000)
        self.assertEqual(struct.unpack_from("<I", pages[0][1], 0xA0)[0], 3)
        self.assertEqual(pages[0][1][0x750:0x754], b"\xAA\xBB\xCC\xDD")

    def test_detached_guest_service_regions_reuse_the_loaded_guest_image(self) -> None:
        services = (
            _HostServiceThunk(0x0022D896, "DirectSoundBufferPlay", 4, 0, 16),
            _HostServiceThunk(0x0028D180, "XInputGetState", 4, 0, 8),
            _HostServiceThunk(0x31F10200, "TitleAssetStreamClose", 4, 0, 0),
            _HostServiceThunk(0xE0000560, "NtReadFile", 4, 0, 32),
        )

        regions = _host_service_allocation_region_addresses(
            services,
            guest_virtual_size=0x5C5000,
            detached_guest=True,
        )

        self.assertEqual(regions, (0x31F10000, 0xE0000000))

    def test_loaded_guest_service_pages_are_writable_during_thunk_install(self) -> None:
        services = (
            _HostServiceThunk(0x0022D896, "DirectSoundBufferPlay", 4, 0, 16),
            _HostServiceThunk(0x0028D180, "XInputGetState", 4, 0, 8),
            _HostServiceThunk(0x31F10200, "TitleAssetStreamClose", 4, 0, 0),
            _HostServiceThunk(0xE0000560, "NtReadFile", 4, 0, 32),
        )

        pages = _loaded_host_service_page_addresses(
            services,
            guest_virtual_size=0x5C5000,
            detached_guest=True,
            audited_host_services=True,
        )

        self.assertEqual(pages, (0x0022D000, 0x0028D000))

    def test_detached_normal_live_restores_the_xbe_header_page(self) -> None:
        self.assertTrue(
            _mappable_data_page(DETACHED_HELPER_IMAGE_BASE, detached_guest=True)
        )
        self.assertFalse(
            _mappable_data_page(DETACHED_HELPER_IMAGE_BASE + 0x1000, detached_guest=True)
        )

    def test_normal_live_boot_stack_contract_uses_a_descending_stack(self) -> None:
        memory = SparseMemory()
        stack_pointer = _initialize_normal_live_boot_stack(
            memory,
            stack_size=0x30000,
        )

        pages = {page * 0x1000 for page, _payload in memory.export_pages()}
        self.assertEqual(stack_pointer, NORMAL_LIVE_BOOT_STACK_TOP)
        self.assertIn(NORMAL_LIVE_BOOT_STACK_TOP - 0x30000, pages)
        self.assertIn(NORMAL_LIVE_BOOT_STACK_TOP - 0x1000, pages)
        self.assertIn(NORMAL_LIVE_BOOT_STACK_TOP, pages)
        self.assertEqual(
            memory.read_u32(NORMAL_LIVE_BOOT_STACK_TOP),
            NORMAL_LIVE_BOOT_RETURN_THUNK_ADDRESS,
        )

    def test_normal_live_boot_kernel_probe_matches_sparse_zero_memory(self) -> None:
        memory = SparseMemory()

        _initialize_normal_live_boot_kernel_probe(memory)

        pages = {page * 0x1000 for page, _payload in memory.export_pages()}
        self.assertEqual(NORMAL_LIVE_BOOT_KERNEL_PROBE_SIZE, 0x2000)
        self.assertEqual(
            pages,
            {
                NORMAL_LIVE_BOOT_KERNEL_PROBE_START,
                NORMAL_LIVE_BOOT_KERNEL_PROBE_START + 0x1000,
            },
        )
        self.assertEqual(memory.read_u32(0x8001003C), 0)
        self.assertEqual(memory.read_u32(0x8000FFF0), 0)

    def test_normal_live_boot_tls_owns_slot_zero(self) -> None:
        memory = SparseMemory()
        tls_index_address = NORMAL_LIVE_TLS_INDEX_ADDRESS
        memory.write_u32(tls_index_address, 0xFFFFFFFB)

        _initialize_normal_live_boot_tls(
            memory,
            tls_index_address=tls_index_address,
            raw_payload=b"\x11\x22\x33\x44",
            zero_fill_size=12,
        )

        self.assertEqual(memory.read_u32(tls_index_address), 0)
        self.assertEqual(
            memory.read_u32(NORMAL_LIVE_BOOT_TLS_BASE + 0x04),
            NORMAL_LIVE_BOOT_TLS_VECTOR,
        )
        self.assertEqual(
            memory.read_u32(NORMAL_LIVE_BOOT_TLS_VECTOR),
            NORMAL_LIVE_BOOT_TLS_DATA,
        )
        self.assertEqual(memory.read(NORMAL_LIVE_BOOT_TLS_DATA, 16), b"\x11\x22\x33\x44" + bytes(12))

    def test_normal_live_primary_thread_layout_is_disjoint_and_resident(self) -> None:
        self.assertEqual(NORMAL_LIVE_PRIMARY_STACK_TOP, 0x71000000)
        self.assertEqual(NORMAL_LIVE_PRIMARY_TLS_BASE, 0x72010000)
        self.assertGreaterEqual(
            NORMAL_LIVE_PRIMARY_THREAD_OBJECT,
            NORMAL_LIVE_PRIMARY_TLS_BASE + 0x1000,
        )
        self.assertGreaterEqual(
            NORMAL_LIVE_PRIMARY_TLS_DATA,
            NORMAL_LIVE_PRIMARY_THREAD_OBJECT + 0x1000,
        )
        self.assertLess(NORMAL_LIVE_PRIMARY_TLS_DATA, NORMAL_LIVE_PRIMARY_TLS_BASE + 0x10000)
        self.assertGreaterEqual(
            NORMAL_LIVE_PRIMARY_TLS_VECTOR,
            NORMAL_LIVE_PRIMARY_TLS_DATA + 0x1000,
        )
        self.assertLess(
            NORMAL_LIVE_PRIMARY_TLS_VECTOR,
            NORMAL_LIVE_PRIMARY_TLS_BASE + 0x10000,
        )

    def test_phase7_static_closure_keeps_both_conditional_successors(self) -> None:
        function = lift_x86_function(
            b"\x74\x02\xC3",
            base_address=0x1000,
            symbol="phase7_conditional_closure",
        )
        target = lift_x86_function(
            b"\xC3",
            base_address=0x1004,
            symbol="phase7_conditional_target",
        )

        instructions, symbols = _phase7_static_instruction_closure(
            (function, target),
            (0x1000,),
            {},
        )

        self.assertEqual(set(instructions), {0x1000, 0x1002, 0x1004})
        self.assertEqual(
            symbols,
            {"phase7_conditional_closure", "phase7_conditional_target"},
        )

    def test_service_region_is_not_reserved_as_a_guest_allocation(self) -> None:
        service = _HostServiceThunk(
            0x31F10300,
            "SyntheticTitleService",
            1,
            0,
            0,
        )
        region = service.target & ~0xFFFF

        regions = _allocation_region_addresses(
            (region,),
            guest_virtual_size=0x1000,
            detached_guest=True,
            host_service_thunks=(service,),
        )

        self.assertNotIn(region, regions)

    def test_worker_runtime_regions_are_not_reserved_twice(self) -> None:
        regions = _allocation_region_addresses(
            (THUNK_REGION, SCRATCH_ADDRESS),
            guest_virtual_size=0x1000,
            detached_guest=True,
            host_service_thunks=(),
        )

        self.assertNotIn(THUNK_REGION, regions)
        self.assertNotIn(SCRATCH_ADDRESS, regions)

    def test_mixed_code_page_allows_declared_static_rewrite_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = build_asset_free_phase5_fixture(Path(temporary))
        memory = SparseMemory()
        memory.write(0x1000, b"\x74\x0D\xA5\x5A")
        capsule = replace(fixture.capsule, memory=memory)

        spans = _mixed_code_page_data_spans(
            capsule,
            ((0x1000, 4),),
            ((0x1000, b"\xCC\x0D"),),
            verified_code_spans=((0x1000, b"\x74\x0D"),),
        )

        self.assertEqual(spans, ((0x1002, b"\xA5\x5A"),))

    def test_contract_identity_is_frozen(self) -> None:
        self.assertEqual(ia32_launcher_cutover_contract_id(), PHASE7_CUTOVER_CONTRACT_ID)
        contract = ia32_launcher_cutover_contract()
        self.assertEqual(contract["launcher"]["default_backend"], "same-isa-ia32")
        self.assertFalse(contract["launcher"]["automatic_live_fallback"])
        self.assertEqual(
            contract["memory"]["command_output"],
            "native-dirty-pages-only",
        )

    def test_missing_normal_artifact_fails_closed_but_oracle_is_explicit(self) -> None:
        with self.assertRaisesRegex(Ia32BackendError, "requires a verified"):
            select_ia32_launcher_backend(None)
        self.assertEqual(
            select_ia32_launcher_backend(None, requested="diagnostic-oracle"),
            "fusion-only-64-bit-diagnostic",
        )

    def test_normal_live_contract_requires_boot_dynamic_scheduler_and_native_io(self) -> None:
        contract = ia32_normal_live_launch_contract()

        self.assertEqual(len(ia32_normal_live_launch_contract_id()), 64)
        self.assertEqual(contract["workload"]["entry_state"], "verified-xbe-entry")
        self.assertEqual(
            contract["workload"]["scheduler"],
            "dynamic-native-resident",
        )
        self.assertFalse(contract["workload"]["fixed_replay_plan"])
        self.assertEqual(contract["live_host"]["command_span_publication"], "native")
        self.assertEqual(contract["live_host"]["audio_decode_mix_submission"], "native")
        self.assertEqual(
            contract["workload"]["vblank_lane"],
            {
                "owner": "native-control-watcher-thread",
                "frequency_hz": 60,
                "callback_address": NORMAL_LIVE_VBLANK_CALLBACK_ADDRESS,
                "callback_data_address": NORMAL_LIVE_VBLANK_DATA_ADDRESS,
                "stack_top": NORMAL_LIVE_VBLANK_STACK_TOP,
                "tls_base": NORMAL_LIVE_VBLANK_TLS_BASE,
                "return_sentinel": IA32_VBLANK_RETURN_SENTINEL,
                "runtime_target_policy": "exact-registered-decoded-callback",
            },
        )
        self.assertEqual(contract["normal_validation"]["python_callbacks"], 0)

    def test_fixed_phase7_artifact_is_not_a_normal_live_artifact(self) -> None:
        artifact = Ia32SliceArtifact(
            root=Path("artifact"),
            executable=Path("artifact/b2r-ia32-slice.exe"),
            manifest_path=Path("artifact/manifest.json"),
            manifest={
                "inputs": {},
                "normal_launcher_cutover": True,
                "resident_scheduler_step_count": 2,
            },
        )

        with patch(
            "tools.recomp.ia32_native_backend.select_ia32_launcher_backend",
            return_value="same-isa-ia32",
        ):
            with self.assertRaisesRegex(Ia32BackendError, "fixed replay-plan"):
                validate_ia32_normal_live_launch_artifact(artifact)

    def test_normal_live_artifact_contract_is_accepted_after_cutover_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "b2r-ia32-live.exe"
            executable.write_bytes(b"verified native live fixture")
            contract = ia32_normal_live_launch_contract()
            contract_id = ia32_normal_live_launch_contract_id()
            artifact = Ia32SliceArtifact(
                root=root,
                executable=executable,
                manifest_path=root / "manifest.json",
                manifest={
                    "artifact_id": "normal-live-artifact",
                    "inputs": {"normal_live_launch_contract_id": contract_id},
                    "normal_live_launch": True,
                    "normal_live_launch_contract_id": contract_id,
                    "normal_live_launch_contract": contract,
                    "executable_sha256": hashlib.sha256(
                        executable.read_bytes()
                    ).hexdigest(),
                },
            )

            with patch(
                "tools.recomp.ia32_native_backend.select_ia32_launcher_backend",
                return_value="same-isa-ia32",
            ):
                validation = validate_ia32_normal_live_launch_artifact(artifact)

        self.assertEqual(validation["status"], "valid")
        self.assertEqual(validation["workload"], "boot-frontend-lesson-one")
        self.assertEqual(validation["normal_runtime_python_callbacks"], 0)

    def test_full_flip_validation_rejects_a_capsule_without_a_flip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = build_asset_free_phase5_fixture(Path(temporary))
        scheduler_state = copy.deepcopy(fixture.capsule.scheduler_state)
        for step in scheduler_state["resident_scheduler"]["steps"]:
            if step["exit"] == "flip":
                step["exit"] = "yield"
        capsule = replace(fixture.capsule, scheduler_state=scheduler_state)
        artifact = Ia32SliceArtifact(
            root=Path("artifact"),
            executable=Path("artifact/worker.exe"),
            manifest_path=Path("artifact/manifest.json"),
            manifest={"inputs": {"stop_eip": fixture.stop_eip}},
        )

        with self.assertRaisesRegex(Ia32BackendError, "at least one completed flip"):
            run_ia32_full_flip_cutover(artifact, capsule)

    def test_observed_full_flip_oracle_restores_terminal_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = build_asset_free_phase5_fixture(Path(temporary))
        initial = fixture.capsule.memory
        terminal = type(initial).from_pages(initial.export_pages())
        changed_page = next(iter(initial.export_pages()))[0]
        terminal.write(changed_page * 4096, b"\xA5")
        terminal_pages = dict(terminal.export_pages())
        scheduler_state = copy.deepcopy(fixture.capsule.scheduler_state)
        scheduler = scheduler_state["resident_scheduler"]
        scheduler_tls_pages = {
            int(scheduler["active_tls_base"]) // 4096,
            *(int(lane["tls_base"]) // 4096 for lane in scheduler["lanes"]),
        }
        digest = hashlib.sha256()
        for page, payload in sorted(terminal_pages.items()):
            if page in scheduler_tls_pages:
                continue
            digest.update(struct.pack("<I", page))
            digest.update(payload)
        changed_payload = terminal_pages[changed_page]
        resource = (
            struct.pack("<4sII", b"B2F7", 1, 1)
            + struct.pack("<I", changed_page)
            + changed_payload
        )
        terminal_state = copy.deepcopy(fixture.capsule.state)
        terminal_state.eip = fixture.stop_eip
        scheduler["capture_mode"] = "observed-full-flip"
        scheduler["full_flip_oracle"] = {
            "format": "b2-recomp-phase7-full-flip-oracle",
            "version": 1,
            "actual_stop_eip": fixture.stop_eip,
            "completed_flips": 1,
            "terminal_cpu_state": cpu_state_record(terminal_state),
            "terminal_memory_resource": "phase7/full-flip-pages.bin",
            "terminal_memory_sha256": digest.hexdigest(),
            "changed_page_count": 1,
            "service_trace": [],
            "service_trace_overflow_count": 0,
        }
        capsule = replace(
            fixture.capsule,
            scheduler_state=scheduler_state,
            resources={"phase7/full-flip-pages.bin": resource},
        )

        oracle, restored_state, restored_memory = _phase7_full_flip_oracle(
            capsule,
            stop_eip=fixture.stop_eip,
        )

        self.assertEqual(oracle["completed_flips"], 1)
        self.assertEqual(restored_state.eip, fixture.stop_eip)
        self.assertEqual(restored_memory.read(changed_page * 4096, 1), b"\xA5")
        self.assertEqual(
            _phase7_full_flip_oracle_page_addresses(capsule, oracle),
            (changed_page * 4096,),
        )

    def test_full_flip_scope_uses_declared_products_tls_and_live_stack(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = build_asset_free_phase5_fixture(Path(temporary))
        plan = _resident_scheduler_plan(fixture.capsule, stop_eip=fixture.stop_eip)
        terminal = copy.deepcopy(fixture.capsule.state)
        terminal.set_register("esp", 0x70FFFF80)

        ranges = _phase7_observed_memory_ranges(plan, terminal)

        self.assertIn(("render-product", plan.render_address, plan.render_size), ranges)
        self.assertIn(("audio-product", plan.audio_address, plan.audio_size), ranges)
        self.assertIn(("primary-live-stack", 0x70FFFF80, 0x80), ranges)
        self.assertIn(("active-tls", plan.active_tls_base, 4096), ranges)
        self.assertEqual(
            {name for name, _address, _size in ranges if name.endswith("-tls")},
            {"active-tls", "primary-tls", "worker-tls", "vblank-tls"},
        )

    def test_full_flip_state_excludes_physical_alias_and_sticky_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = build_asset_free_phase5_fixture(Path(temporary))
        accepted = copy.deepcopy(fixture.capsule.state)
        native = copy.deepcopy(fixture.capsule.state)
        accepted.mmx_registers["mm0"] = 0x1234
        native.mmx_registers["mm0"] = 0
        accepted.mxcsr &= ~0x3F
        native.mxcsr |= 0x21
        accepted.fpu_status_word = 0x100
        native.fpu_status_word = 0x161

        self.assertEqual(
            _phase7_observed_state_record(accepted, fs_base=0x73030000),
            _phase7_observed_state_record(native, fs_base=0x73030000),
        )

    def test_full_flip_service_projection_excludes_global_worker_traffic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = build_asset_free_phase5_fixture(Path(temporary))
        service_state = copy.deepcopy(fixture.capsule.service_state)
        service_state["service_registry"] = [
            {
                "target": 0xE0000690,
                "kind": 1,
                "execution": "native32",
            }
        ]
        capsule = replace(fixture.capsule, service_state=service_state)
        oracle = {
            "service_trace": [
                {"target": 0xE0000690, "result": 0},
                {"target": 0xE00005C0, "result": 0x102},
                {"target": 0x0022D916, "result": 0},
            ]
        }

        self.assertEqual(
            _phase7_replayable_service_trace(capsule, oracle),
            ((0xE0000690, 0),),
        )

    def test_generated_worker_seeds_once_and_uses_publications_afterward(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = build_asset_free_phase5_fixture(Path(temporary))
            scheduler_plan = _resident_scheduler_plan(
                fixture.capsule,
                stop_eip=fixture.stop_eip,
            )
        pages = tuple(sorted(scheduler_plan.required_pages))
        source = _phase7_c_source(
            page_addresses=pages,
            mapped_page_addresses=pages,
            guest_virtual_size=0x10000,
            code_spans=((fixture.capsule.state.eip, b"\xc3"),),
            stop_eip=fixture.stop_eip,
            scheduler_plan=scheduler_plan,
            service_state=fixture.capsule.service_state,
            detached_guest=True,
        )
        self.assertIn("initialize_resident_memory();", source)
        self.assertIn("kDetachedGuestCodePages[]", source)
        self.assertIn("publish_changed_page", source)
        self.assertIn("publish_page_without_code", source)
        self.assertIn("publish_changed_bytes", source)
        self.assertIn("? publish_page_without_code(", source)
        self.assertIn(": publish_changed_page(published, resident)", source)
        self.assertNotIn("g_canonical_publication_page[4096]", source)
        self.assertIn("for (index = 0; index < 1024u; index += 8u)", source)
        self.assertNotIn("byte_index < 4096u", source)
        self.assertEqual(source.count("apply_host_publications();"), 2)
        self.assertIn("states[index] = 1u;", source)
        self.assertNotIn(
            "copy_bytes(exchange->pages + index * 4096u, (void*)kPages[index], 4096u);",
            source,
        )

    def test_normal_live_frame_manifest_commits_only_new_ring_spans(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = build_asset_free_phase5_fixture(Path(temporary))
            scheduler_plan = _resident_scheduler_plan(
                fixture.capsule,
                stop_eip=fixture.stop_eip,
            )
        pages = tuple(sorted(scheduler_plan.required_pages))
        source = _phase7_c_source(
            page_addresses=pages,
            mapped_page_addresses=pages,
            guest_virtual_size=0x10000,
            code_spans=((fixture.capsule.state.eip, b"\xc3"),),
            stop_eip=fixture.stop_eip,
            scheduler_plan=scheduler_plan,
            service_state=fixture.capsule.service_state,
            detached_guest=True,
        )
        source = _phase7_normal_live_c_source(
            source,
            page_count=len(pages),
            boot_image_size=0x10000,
            stop_instruction=X86Instruction(
                address=fixture.stop_eip,
                size=5,
                mnemonic="nop",
                bytes_hex="9090909090",
            ),
        )

        self.assertIn("static U64 g_live_command_write_count;", source)
        self.assertIn("static U32 g_live_push_ring_cursor;", source)
        self.assertIn("static U32 __stdcall live_control_watcher", source)
        self.assertIn(
            "while (live_read_u32(g_live_control, 32u) == 0u) {",
            source,
        )
        self.assertIn(
            "__atomic_exchange_n(&g_live_summary_claimed, 1u, 5)",
            source,
        )
        self.assertIn(
            "live_write_fault_summary(ExceptionRecord* record, void* context)",
            source,
        )
        self.assertIn("context ? ((U32*)context)[40] : 0u", source)
        self.assertIn(',\\\"eip\\\":\\\"', source)
        self.assertIn(
            "g_live_control_thread = CreateThread(\n"
            "            0, 0u, live_control_watcher, 0, 0u, 0);",
            source,
        )
        self.assertLess(
            source.index("if (!live_open_transports()) ExitProcess(20);"),
            source.index("g_live_control_thread = CreateThread("),
        )
        vblank = source[
            source.index("static U32 __stdcall live_vblank_lane") :
            source.index("static BOOL live_start_vblank_lane")
        ]
        self.assertIn("interval = (frequency + 30u) / 60u;", vblank)
        self.assertIn(f"0x{NORMAL_LIVE_VBLANK_CONTEXT_GLOBAL:08X}u", vblank)
        self.assertIn(f"0x{NORMAL_LIVE_VBLANK_CALLBACK_ADDRESS:08X}u", vblank)
        self.assertIn(f"0x{NORMAL_LIVE_VBLANK_DATA_ADDRESS:08X}u", vblank)
        self.assertIn(f"0x{NORMAL_LIVE_VBLANK_STACK_TOP:08X}u", vblank)
        self.assertIn(f"0x{IA32_VBLANK_RETURN_SENTINEL:08X}u", vblank)
        self.assertIn(
            f"0x{NORMAL_LIVE_VBLANK_START_THUNK_ADDRESS:08X}u)();",
            vblank,
        )
        self.assertIn(f"0x{NORMAL_LIVE_VBLANK_SCRATCH_ADDRESS:08X}u", vblank)
        self.assertNotIn("0x005518FCu", vblank)
        self.assertIn(
            f"(void*)0x{NORMAL_LIVE_VBLANK_TLS_BASE:08X}u, 0x10000u",
            source,
        )
        self.assertIn("if (!live_start_vblank_lane()) {", source)
        self.assertIn("if (g_live_vblank_lane_ready)", source)
        self.assertIn("live_vblank_lane(0);", source)
        self.assertIn(
            "if (live_read_u32(g_live_control, 32u) != 0u) {\n"
            "                Sleep(1u);\n"
            "                continue;\n"
            "            }",
            source,
        )
        self.assertIn(
            "if (!live_publish_frame()) {\n"
            "                if (live_read_u32(g_live_control, 32u) != 0u) {\n"
            "                    Sleep(1u);\n"
            "                    continue;\n"
            "                }\n"
            "                live_write_summary();\n"
            "                ExitProcess(70);",
            source,
        )
        self.assertIn("\\\"publication_failure_code\\\"", source)
        self.assertIn("\\\"retained_vertex_ranges\\\"", source)
        self.assertIn(
            f"(void*)0x{NORMAL_LIVE_VBLANK_EXIT_THUNK_ADDRESS:08X}u",
            source,
        )
        self.assertIn(
            f"(void*)0x{IA32_VBLANK_RETURN_SENTINEL:08X}u",
            source,
        )
        self.assertIn("g_live_vblank_completion_count", source)
        self.assertIn(
            'live_join_name("b2_recomp_presented_", g_live_run_id', source
        )
        self.assertIn(
            'live_join_name("b2_recomp_published_", g_live_run_id', source
        )
        self.assertIn(
            "live_manifest_string(payload, size, 30u,\n"
            "                                g_live_presentation_event_name)",
            source,
        )
        self.assertIn(
            "live_manifest_string(payload, size, 31u,\n"
            "                                g_live_publication_event_name)",
            source,
        )
        self.assertIn("if (!SetEvent(g_live_publication_event)) return 0;", source)
        self.assertIn(
            "live_manifest_u64(payload, size, 10u,\n"
            "                             g_live_command_write_count)",
            source,
        )
        self.assertIn(
            "live_manifest_u64(payload, size, 25u,\n"
            "                             g_live_command_cursor)",
            source,
        )
        self.assertIn("live_manifest_u64(payload, size, 28u, 1u)", source)
        frame = source[
            source.index("static BOOL live_publish_frame(void)") :
            source.index("static BOOL live_start_primary_worker")
        ]
        ring_commit = frame.index("live_publish_command_span")
        resource_commit = frame.index("live_publish_resources()")
        manifest_commit = frame.index("live_publish_manifest()")
        self.assertLess(ring_commit, resource_commit)
        self.assertLess(resource_commit, manifest_commit)
        self.assertIn("ring_cursor = *(volatile U32*)context;", frame)
        self.assertIn("g_live_push_ring_cursor, ring_cursor,\n                !initialized", frame)
        self.assertIn("if (published_span && !initialized) ++g_live_flip_count", frame)
        self.assertIn("if (initialized) return 1;", frame)
        self.assertNotIn("put_words", frame)
        span = source[
            source.index("static BOOL live_publish_command_span(") :
            source.index("static BOOL live_publish_frame(void)")
        ]
        self.assertIn("0x80000000u + span_start - ring_start", span)
        self.assertIn("live_scan_resource_span(", span)
        self.assertIn("g_live_command_write_count += write_count", span)
        resources = source[
            source.index("#define LIVE_RESOURCE_BINDING_CAPACITY") :
            source.index("static BOOL live_publish_manifest(void)")
        ]
        self.assertIn("g_live_texture_resources);", resources)
        self.assertIn("allocation->contiguous", resources)
        self.assertIn("g_live_texture_binding_cache", resources)
        self.assertIn("g_live_resource_dedupe_stamps", resources)
        self.assertIn("g_live_resource_last_allocation", resources)
        self.assertIn(
            "live_sha256_finish(&content, resource->hash)", resources
        )
        self.assertIn("generation == g_live_resource_generation", resources)
        self.assertIn(
            "copy_bytes(resource->hash, header + 24u, sizeof(resource->hash))",
            resources,
        )
        self.assertIn(
            "(const void*)resource->source, resource->byte_count))", resources
        )
        self.assertIn("words_a[7] != words_b[7]", resources)
        self.assertIn("else {\n                live_hash_resource(resource);", resources)
        self.assertIn("retained_binding_count", resources)
        self.assertIn("retained_binding_evict_cursor", resources)
        self.assertIn("vertex_array_offsets[16]", resources)
        self.assertIn("vertex_array_formats[16]", resources)
        self.assertIn("live_retain_active_vertex_ranges()", resources)
        self.assertIn(
            "state->retained_vertex_range_count >= LIVE_VERTEX_RANGE_CAPACITY",
            resources,
        )
        self.assertIn(
            "candidate_end - candidate_start > 16u * 1024u * 1024u",
            resources,
        )
        self.assertIn(
            "start = candidate_start;\n        end = candidate_end;",
            resources,
        )
        self.assertNotIn(
            "if (end - start > 16u * 1024u * 1024u) return 0;",
            resources,
        )
        self.assertIn(
            "live_resource_source(\n"
            "                    retained_start, retained_end - retained_start, &source)",
            resources,
        )
        self.assertIn("method == 0x1800u || method == 0x1808u", resources)
        self.assertIn("method == 0x1810u", resources)
        self.assertIn('candidate.format = "VERTEX_BUFFER"', resources)
        self.assertIn("live_collect_resources(", resources)
        self.assertNotIn("g_live_resource_scan.frame_binding_count = 0u", resources)
        self.assertIn(
            "if (!live_prepare_resource_snapshot(\n"
            "            g_live_texture_resources, resource_count)) return 1;\n"
            "    ++g_live_resource_generation;",
            resources,
        )
        self.assertNotIn("++g_live_resource_generation", frame)
        self.assertIn('copy_bytes(output, "B2TEX001", 8u)', resources)
        self.assertIn("g_live_resource_payload_size = payload_size", resources)
        self.assertNotIn("live_write_u32(output, 8u, 0u)", resources)
        self.assertIn(
            "live_manifest_u64(payload, size, 33u,\n"
            "                             g_live_resource_payload_size)",
            source,
        )
        self.assertIn(
            "for (index = 0u; index < g_live_file_count; ++index) {\n"
            "        if (!g_live_files[index].active) {",
            source,
        )
        self.assertIn(
            "if ((file->flags & 2u) && transferred < requested &&\n"
            "                        (offset >= file->size ||\n"
            "                         (U64)transferred == file->size - offset))",
            source,
        )
        self.assertIn(
            "Report zero-filled completion without\n"
            "                       advancing the persistent cursor past consumed bytes.",
            source,
        )
        self.assertIn(
            "for (index = transferred; index < requested; ++index)\n"
            "                            *(U8*)(buffer + index) = 0u;",
            source,
        )
        self.assertIn(
            "transfer_end = offset + (U64)transferred;\n"
            "            position_end = offset + (U64)consumed;\n"
            "            if (!arguments[7]) file->position = position_end;",
            source,
        )
        self.assertIn(
            "if (value == 22u && transfer_end > file->size)\n"
            "                file->size = transfer_end;",
            source,
        )
        self.assertIn("static void live_refresh_controller(void)", source)
        self.assertIn("sequence = live_read_u32(g_live_control, 64u);", source)
        self.assertIn("g_live_controller.buttons & analog_masks[index]", source)
        self.assertIn(
            "static LiveControllerState g_live_controller = {\n"
            "    0u, 0u, 0u, 0, 0, 0, 0, 1u, 0u};",
            source,
        )
        self.assertIn("g_live_controller_service_counts[service->runtime_kind]", source)
        self.assertIn("\\\"device_queries\\\"", source)
        self.assertIn("\\\"state_calls\\\"", source)
        self.assertIn("\\\"close_calls\\\"", source)
        self.assertIn("\\\"set_state_calls\\\"", source)
        self.assertIn("g_live_controller_open_mask", source)
        self.assertIn("*(U32*)output = 0u;", source)
        self.assertIn("*(U8*)(output + 0x41u) = (U8)(port + 2u);", source)
        self.assertIn(
            "g_live_controller_last_left_motor = *(U16*)(output + 0x42u);",
            source,
        )
        self.assertIn(
            "g_live_controller_last_right_motor = *(U16*)(output + 0x44u);",
            source,
        )
        self.assertIn("static U32 live_dispatch_audio_service(", source)
        self.assertIn(
            "static U32 live_audio_effect_image(\n"
            "    U32 this_pointer, U32 image, U32 image_size, U32 workspace_output",
            source,
        )
        self.assertIn(
            "(image_size != 0x54FCu && image_size != 0x5800u)", source
        )
        self.assertIn("table_offset != 0x542Cull", source)
        self.assertIn("if (count != 5u) goto invalid;", source)
        self.assertIn("copy_bytes((void*)target0", source)
        self.assertIn("copy_bytes((void*)target8", source)
        self.assertIn("*(U32*)(this_pointer + 0x20u) = workspace_address;", source)
        self.assertIn(
            "return live_dispatch_audio_service(service, this_pointer, arguments);",
            source,
        )
        self.assertIn("effect_image_failures", source)
        self.assertIn("g_live_audio_service_value_counts[value]", source)
        self.assertIn("g_live_audio_service_value_counts[31]", source)
        self.assertIn("*(U32*)base = 0x002C9474u;", source)
        self.assertIn("*(U32*)(base + 4u) = 1u;", source)
        self.assertIn("U32 key = base;", source)
        self.assertIn("*(U32*)base = 0x002C948Cu;", source)
        self.assertIn("*(U32*)(base + 4u) = 0x002C9480u;", source)
        self.assertIn("if (value == 29u) {", source)
        self.assertIn("live_audio_stop_buffer(key);", source)
        self.assertIn("if (value == 30u) {", source)
        self.assertIn("g_live_audio_packets[index].stream == key", source)
        self.assertIn("\\\"buffer_release_calls\\\"", source)
        self.assertIn("\\\"stream_release_calls\\\"", source)
        self.assertIn("if (value == 28u) {", source)
        self.assertIn("*(U32*)(buffer->descriptor + 0xC0u) = start;", source)
        self.assertIn("if (value == 22u) {", source)
        self.assertIn("*(U32*)(buffer->descriptor + 0xC8u) = start;", source)
        self.assertIn(
            "value == 25u || value == 26u || value == 27u", source
        )
        self.assertIn("U64 factor = 0xFFB497A2ull;", source)
        self.assertIn(
            "gain = (gain * factor + 0x80000000ull) >> 32u;", source
        )
        self.assertIn("if (value == 21u) {", source)
        self.assertIn("if (mixbin == 0u) buffer->left_mixbin_volume", source)
        self.assertIn("else if (mixbin == 1u) buffer->right_mixbin_volume", source)
        self.assertIn("live_audio_refresh_playback_gain(", source)
        self.assertIn("\\\"buffer_create_calls\\\"", source)
        self.assertIn("\\\"stream_process_calls\\\"", source)
        self.assertNotIn("live_audio_frontend_special_create", source)
        self.assertIn(
            "live_audio_set_music_mode(this_pointer, arguments[0])", source
        )
        self.assertIn("if (mode == 3u)", source)
        self.assertIn("else if (mode == 3u && has_gameplay_track)", source)
        self.assertIn("live_audio_stop_music();", source)
        self.assertNotIn("queued->sequence < sequence", source)
        self.assertIn("U32 packet_index = stream->packet_head;", source)
        self.assertIn("stream->packet_head = packet->next;", source)
        self.assertIn("stream->packet_tail = packet_index;", source)
        self.assertIn("slot->sequence = ++g_live_audio_packet_sequence", source)
        self.assertIn("*(U32*)status = 0x8000000Au;", source)
        self.assertIn("live_audio_complete_packet(packet, 0u);", source)
        self.assertIn("0x8000000Bu", source)
        self.assertIn("SetEvent((HANDLE)packet->completion_event)", source)
        self.assertIn("\\\"packet_completions\\\"", source)
        self.assertIn("\\\"packet_flushes\\\"", source)
        self.assertIn("\\\"lane_start_failures\\\"", source)
        self.assertIn(
            "0, 64u * 1024u, live_audio_lane, 0, 0x00010000u, 0", source
        )
        self.assertIn("g_live_audio_lane_start_error = GetLastError();", source)
        self.assertIn("if (!live_start_audio_lane()) ExitProcess(20);", source)
        self.assertIn("\\\"active_mixes\\\"", source)
        self.assertIn("copy_bytes(g_live_control + 8224u, samples, byte_count);", source)
        self.assertIn("live_write_u32(g_live_control, 8192u, published);", source)
        self.assertIn("service->runtime_kind == 4u ||", source)
        self.assertIn("service->runtime_kind == 7u ||", source)
        self.assertIn("service->runtime_kind == 16u ||", source)
        self.assertIn("service->runtime_kind == 17u)", source)
        self.assertIn("if (service->runtime_kind == 13u)", source)
        self.assertIn("native_input", source)
        self.assertIn("native_audio", source)
        self.assertIn("g_workload_last_allocation", source)
        self.assertIn("g_workload_semaphore_cache[64]", source)
        self.assertIn("g_live_file_cache[256]", source)
        self.assertIn("g_live_audio_playback_cache[64]", source)
        self.assertIn("g_live_audio_active_playback_masks[2]", source)
        self.assertIn("g_live_audio_active_stream_mask", source)
        self.assertIn("g_live_audio_packet_free_hint", source)
        self.assertIn("live_audio_reserve_playback()", source)
        self.assertIn("__builtin_ctz(available)", source)
        self.assertIn("playback = live_audio_playback(arguments[0]);", source)
        self.assertEqual(source.count("install_host_service_thunks();"), 2)
        reinstall = source[
            source.index("static void install_host_service_thunks") :
            source.index("static const char* mapping_name")
        ]
        self.assertNotIn("kNormalLiveHostDataPages", reinstall)

    def test_warm_command_segments_exclude_page_payloads_and_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = build_asset_free_phase5_fixture(Path(temporary))
        pages = tuple(0x00100000 + index * 4096 for index in range(16))
        artifact = Ia32SliceArtifact(
            root=Path("artifact"),
            executable=Path("artifact/worker.exe"),
            manifest_path=Path("artifact/manifest.json"),
            manifest={
                "normal_launcher_cutover": True,
                "page_addresses": list(pages),
                "resident_scheduler_step_count": len(
                    fixture.capsule.scheduler_state["resident_scheduler"]["steps"]
                ),
            },
        )
        header, tail_offset, tail = _cutover_command_segments(
            fixture.capsule,
            artifact,
            worker_command=WORKER_COMMAND_SCHEDULE,
        )
        self.assertEqual(int.from_bytes(header[4:8], "little"), CUTOVER_EXCHANGE_VERSION)
        self.assertLess(len(header) + len(tail), len(pages) * 4096)
        self.assertGreater(tail_offset, len(header) + len(pages) * 4096)
        self.assertNotIn(CUTOVER_MEMORY_MAGIC.to_bytes(4, "little"), tail)


if __name__ == "__main__":
    unittest.main()
