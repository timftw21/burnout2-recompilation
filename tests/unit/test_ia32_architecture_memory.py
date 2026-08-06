from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from tools.playability.replay_capsule import capture_replay_capsule, load_replay_capsule
from tools.recomp.ia32_native_backend import (
    ARCHITECTURE_AUDIO_DSP_READY_THUNK_ADDRESS,
    ARCHITECTURE_CONTRACT_FORMAT,
    ARCHITECTURE_DENSE_RDTSC_THUNK_ADDRESS,
    ARCHITECTURE_EXCHANGE_VERSION,
    ARCHITECTURE_MCPX_FRAME_COUNTER_THUNK_BASE,
    ARCHITECTURE_MCPX_FRAME_COUNTER_THUNK_STRIDE,
    ARCHITECTURE_RDTSC_THUNK_ADDRESS,
    ARCHITECTURE_SGDT_THUNK_ADDRESS,
    ARCHITECTURE_TIMESTAMP_COUNTER_ADDRESS,
    DETERMINISTIC_TSC_STEP,
    PHASE3_ARCHITECTURE_CONTRACT_ID,
    PHASE7_AUDIO_BUFFER_COMMAND_FAMILY,
    PHASE7_AUDIO_BUFFER_COMMAND_IMMEDIATE_SITE,
    PHASE7_AUDIO_BUFFER_COMMAND_VALUE,
    PHASE7_AUDIO_BUFFER_COPY_CALL_SITES,
    PHASE7_AUDIO_BUFFER_COPY_TARGET,
    PHASE7_AUDIO_BUFFER_MMIO_END,
    PHASE7_AUDIO_BUFFER_MMIO_FAMILY,
    PHASE7_AUDIO_BUFFER_MMIO_PAGES,
    PHASE7_AUDIO_BUFFER_MMIO_START,
    PHASE7_AUDIO_DSP_CONTROL_ADDRESS,
    PHASE7_AUDIO_DSP_CONTROL_WRITE_SITES,
    PHASE7_AUDIO_DSP_MMIO_FAMILY,
    PHASE7_AUDIO_DSP_MMIO_PAGE,
    PHASE7_AUDIO_DSP_RESET_READY,
    PHASE7_AUDIO_DSP_RESET_REQUEST,
    PHASE7_AUDIO_DSP_STATUS_ADDRESS,
    PHASE7_AUDIO_SG_MMIO_ACCESS_SITES,
    PHASE7_AUDIO_SG_MMIO_FAMILY,
    PHASE7_AUDIO_SG_MMIO_PAGE,
    PHASE7_AUDIO_VOICE_COMMAND_PENDING,
    PHASE7_AUDIO_VOICE_COMMAND_WRITE_BASES,
    PHASE7_D3D_MMIO_PAGE,
    PHASE7_GPU_COMPLETION_SHADOW_READ_SITES,
    PHASE7_MCPX_FRAME_COUNTER_ADDRESS,
    PHASE7_MCPX_FRAME_COUNTER_INCREMENT,
    PHASE7_MCPX_INDEXED_MMIO_ACCESS_SITES,
    PHASE7_MCPX_INDEXED_MMIO_FAMILY,
    PHASE7_MCPX_MMIO_PAGE,
    PHASE7_PFIFO_WRITE_ONE_TO_CLEAR_SITE,
    PHASE7_PHYSICAL_ZERO_PAGE,
    PHASE7_SELF_CLEARING_MMIO_WRITE_SITES,
    Ia32ArchitectureProfile,
    Ia32BackendError,
    _FixedSlicePreflight,
    _apply_phase3_timestamp_accounting,
    _decode_phase3_fxsave,
    _encode_phase3_fxsave,
    _guarded_static_boundaries,
    _mappable_data_page,
    _phase3_architecture_plan,
    _resident_audio_buffer_command_rewrite,
    _validate_phase7_audio_buffer_mmio_family,
    _validate_phase7_audio_sg_mmio_family,
    _validate_phase7_mcpx_indexed_mmio_family,
    build_ia32_architecture_artifact,
    ia32_architecture_contract,
    ia32_architecture_contract_id,
)
from tools.recomp.ia32_proof_contract import (
    PHASE3_PROOF_SET_FORMAT,
    PHASE3_PROOF_SET_ID,
    run_asset_free_phase3_proofs,
)
from tools.recomp.x86_lifter import CpuState, SparseMemory, lift_x86_function


IA32_INTEGRATION_AVAILABLE = (
    sys.platform == "win32" and shutil.which("clang-cl") and shutil.which("lld-link")
)


def _audio_buffer_command_instructions():
    return {
        address: lift_x86_function(
            bytes.fromhex(encoded),
            base_address=address,
            symbol=f"audio_buffer_command_{address:08x}",
        ).instructions[0]
        for address, encoded in PHASE7_AUDIO_BUFFER_COMMAND_FAMILY.items()
    }


def _audio_buffer_mmio_instructions():
    return {
        address: lift_x86_function(
            bytes.fromhex(encoded),
            base_address=address,
            symbol=f"audio_buffer_mmio_{address:08x}",
        ).instructions[0]
        for address, encoded in PHASE7_AUDIO_BUFFER_MMIO_FAMILY.items()
    }


def _capsule(
    path: Path,
    *,
    code: bytes,
    state: CpuState,
    memory: SparseMemory,
    entry_eip: int = 0x000C0000,
):
    state.eip = entry_eip
    return load_replay_capsule(
        capture_replay_capsule(
            path,
            state=state,
            memory=memory,
            functions=(
                lift_x86_function(
                    code,
                    base_address=entry_eip,
                    symbol="phase3_negative_test",
                ),
            ),
            scheduler_state={},
            service_state={"service_trace": []},
            provenance={"synthetic": True, "source": "phase3-unit-test"},
            capture_kind="synthetic",
        )
    )


class Ia32ArchitectureMemoryTests(unittest.TestCase):
    def test_phase3_contract_freezes_architecture_memory_and_fault_policies(self) -> None:
        contract = ia32_architecture_contract()

        self.assertEqual(contract["format"], ARCHITECTURE_CONTRACT_FORMAT)
        self.assertEqual(ia32_architecture_contract_id(), PHASE3_ARCHITECTURE_CONTRACT_ID)
        self.assertEqual(contract["exchange"]["version"], ARCHITECTURE_EXCHANGE_VERSION)
        self.assertEqual(contract["normal_runtime_python_callbacks"], 0)
        self.assertIn("x87_mmx_alias", contract["architectural_state"])
        self.assertIn("dirty_ownership", contract["memory"])
        self.assertIn("exception-code", contract["faults"]["identity"])

    def test_phase3_fxsave_round_trips_x87_and_mmx_alias_modes(self) -> None:
        x87 = CpuState()
        x87.fpu_status_word = 3 << 11
        x87.fpu_stack = [1.0, -2.5, 0.25]
        decoded_x87 = CpuState()
        _decode_phase3_fxsave(decoded_x87, _encode_phase3_fxsave(x87))
        self.assertEqual(decoded_x87.fpu_stack, x87.fpu_stack)
        self.assertFalse(any(decoded_x87.mmx_registers.values()))

        mmx = CpuState()
        mmx.set_mmx_register("mm0", 0x1122334455667788)
        mmx.set_mmx_register("mm7", 0xFEDCBA9876543210)
        decoded_mmx = CpuState()
        _decode_phase3_fxsave(decoded_mmx, _encode_phase3_fxsave(mmx))
        self.assertEqual(decoded_mmx.fpu_stack, [])
        self.assertEqual(decoded_mmx.mmx_registers, mmx.mmx_registers)

    def test_dynamic_fs_access_fails_with_address_named_rewrite_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state = CpuState.with_registers(
                ecx=0x30,
                esp=0x000E0800,
                fs_base=0x70000000,
            )
            capsule = _capsule(
                root / "dynamic-fs.b2rcap",
                code=bytes.fromhex("648B01C3"),
                state=state,
                memory=SparseMemory({0x000E0000: 0, 0x70000030: 42}),
            )

            with self.assertRaisesRegex(Ia32BackendError, r"0x000C0000.*dynamic fs"):
                build_ia32_architecture_artifact(
                    capsule,
                    stop_eip=0x000C0003,
                    build_dir=root / "artifacts",
                )

    def test_self_modifying_executable_memory_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state = CpuState.with_registers(eax=42, esp=0x000E0800)
            capsule = _capsule(
                root / "self-modifying.b2rcap",
                code=bytes.fromhex("A300000C00C3"),
                state=state,
                memory=SparseMemory({0x000C0000: bytes(4096), 0x000E0000: 0}),
            )

            with self.assertRaisesRegex(
                Ia32BackendError,
                r"0x000C0000.*self-modifying executable memory",
            ):
                build_ia32_architecture_artifact(
                    capsule,
                    stop_eip=0x000C0005,
                    build_dir=root / "artifacts",
                    profile=Ia32ArchitectureProfile(),
                )

    def test_phase3_still_rejects_wbinvd_without_resident_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            capsule = _capsule(
                root / "wbinvd.b2rcap",
                code=bytes.fromhex("0F09C3"),
                state=CpuState.with_registers(esp=0x000E0800),
                memory=SparseMemory({0x000E0000: 0}),
            )

            with self.assertRaisesRegex(
                Ia32BackendError,
                r"0x000C0000 wbinvd has no Phase-3 privileged rewrite",
            ):
                build_ia32_architecture_artifact(
                    capsule,
                    stop_eip=0x000C0002,
                    build_dir=root / "artifacts",
                )

    def test_resident_scheduler_preserves_debug_interrupts_as_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name, code_hex, stop_eip in (
                ("int3", "CCC3", 0x000C0001),
                ("int2d", "CD2DC3", 0x000C0002),
            ):
                with self.subTest(name=name):
                    capsule = _capsule(
                        root / f"resident-{name}.b2rcap",
                        code=bytes.fromhex(code_hex),
                        state=CpuState.with_registers(esp=0x000E0800),
                        memory=SparseMemory({0x000E0000: 0}),
                    )
                    instructions = {
                        instruction.address: instruction
                        for function in capsule.functions
                        for instruction in function.instructions
                    }
                    preflight = _FixedSlicePreflight(
                        instructions=instructions,
                        page_addresses=(),
                        memory_ranges=(),
                        write_ranges=(),
                        function_symbols=(capsule.functions[0].symbol,),
                        indirect_targets={},
                        host_service_thunks=(),
                        host_service_calls=(),
                        steps=0,
                    )

                    plan = _phase3_architecture_plan(
                        capsule,
                        preflight,
                        stop_eip=stop_eip,
                        profile=Ia32ArchitectureProfile(),
                        resident_privileged_boundary=True,
                    )
                    guards = _guarded_static_boundaries(plan.instructions, stop_eip)

                    self.assertEqual(plan.rewrites, ())
                    self.assertIn("fail-closed terminal trap", guards[0x000C0000])

    def test_resident_scheduler_guards_unverified_dense_rdtsc(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            capsule = _capsule(
                root / "resident-dense-rdtsc.b2rcap",
                code=bytes.fromhex("0F31") + b"\x90" * 128 + b"\xC3",
                state=CpuState.with_registers(esp=0x000E0800),
                memory=SparseMemory({0x000E0000: 0}),
            )
            instructions = {
                instruction.address: instruction
                for function in capsule.functions
                for instruction in function.instructions
            }
            preflight = _FixedSlicePreflight(
                instructions=instructions,
                page_addresses=(),
                memory_ranges=(),
                write_ranges=(),
                function_symbols=(capsule.functions[0].symbol,),
                indirect_targets={},
                host_service_thunks=(),
                host_service_calls=(),
                steps=0,
            )

            plan = _phase3_architecture_plan(
                capsule,
                preflight,
                stop_eip=0x000C0082,
                profile=Ia32ArchitectureProfile(),
                resident_privileged_boundary=True,
            )
            guards = _guarded_static_boundaries(plan.instructions, 0x000C0082)

        self.assertEqual(plan.rewrites, ())
        self.assertIn(0x000C0000, guards)

    def test_resident_scheduler_batches_observed_privileged_rewrites(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cases = (
                (
                    "interrupt-mask",
                    0x000E666A,
                    bytes.fromhex("FAC7068D04C528FBC3"),
                    0x000E6672,
                ),
                (
                    "gdtr",
                    0x000C0000,
                    bytes.fromhex("0F01442406C3"),
                    0x000C0005,
                ),
                (
                    "port-read",
                    0x0021D099,
                    bytes.fromhex("66BAC080EC8B5608C3"),
                    0x0021D0A1,
                ),
                (
                    "timestamps",
                    0x000C0000,
                    bytes.fromhex("0F31900F31C3"),
                    0x000C0005,
                ),
                (
                    "dense-timestamp",
                    0x000E24BC,
                    bytes.fromhex("8B4C24040F318901895104C3"),
                    0x000E24C7,
                ),
            )
            plans = {}
            for name, entry_eip, code, stop_eip in cases:
                capsule = _capsule(
                    root / f"resident-{name}.b2rcap",
                    code=code,
                    state=CpuState.with_registers(
                        esp=0x000E0800,
                        timestamp_counter=0x1122334455667788,
                    ),
                    memory=SparseMemory({0x000E0000: 0}),
                    entry_eip=entry_eip,
                )
                instructions = {
                    instruction.address: instruction
                    for function in capsule.functions
                    for instruction in function.instructions
                }
                preflight = _FixedSlicePreflight(
                    instructions=instructions,
                    page_addresses=(),
                    memory_ranges=(),
                    write_ranges=(),
                    function_symbols=(capsule.functions[0].symbol,),
                    indirect_targets={},
                    host_service_thunks=(),
                    host_service_calls=(),
                    steps=0,
                )
                plans[name] = _phase3_architecture_plan(
                    capsule,
                    preflight,
                    stop_eip=stop_eip,
                    profile=Ia32ArchitectureProfile(),
                    resident_privileged_boundary=True,
                )

        interrupt_plan = plans["interrupt-mask"]
        self.assertEqual(interrupt_plan.instructions[0x000E666A].bytes_hex, "90")
        self.assertEqual(interrupt_plan.instructions[0x000E6671].bytes_hex, "90")
        self.assertEqual(
            {record["status"] for record in interrupt_plan.rewrites},
            {"implemented-cooperative-scheduler-mask"},
        )

        gdtr_plan = plans["gdtr"]
        self.assertTrue(gdtr_plan.instructions[0x000C0000].bytes_hex.startswith("E8"))
        self.assertIn(
            ARCHITECTURE_SGDT_THUNK_ADDRESS,
            {address for address, _payload in gdtr_plan.extra_code_spans},
        )

        port_plan = plans["port-read"]
        self.assertEqual(port_plan.instructions[0x0021D099].bytes_hex, "9C24009D")
        self.assertEqual(port_plan.instructions[0x0021D09D].bytes_hex, "90")

        timestamp_plan = plans["timestamps"]
        self.assertEqual(len(timestamp_plan.rewrites), 2)
        self.assertEqual(
            {record["status"] for record in timestamp_plan.rewrites},
            {"implemented-deterministic-shared-counter-island"},
        )
        span_addresses = {address for address, _payload in timestamp_plan.extra_code_spans}
        self.assertIn(ARCHITECTURE_RDTSC_THUNK_ADDRESS, span_addresses)
        self.assertIn(ARCHITECTURE_TIMESTAMP_COUNTER_ADDRESS, span_addresses)

        dense_plan = plans["dense-timestamp"]
        self.assertTrue(dense_plan.instructions[0x000E24BC].bytes_hex.startswith("E8"))
        self.assertTrue(dense_plan.instructions[0x000E24C0].bytes_hex.endswith("90"))
        self.assertEqual(
            {record["status"] for record in dense_plan.rewrites},
            {"implemented-deterministic-dense-helper-thunk"},
        )
        self.assertIn(
            ARCHITECTURE_DENSE_RDTSC_THUNK_ADDRESS,
            {address for address, _payload in dense_plan.extra_code_spans},
        )

    def test_physical_zero_page_uses_an_address_named_direct_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            capsule = _capsule(
                root / "physical-zero.b2rcap",
                code=bytes.fromhex("A300000080C3"),
                state=CpuState.with_registers(eax=42, esp=0x000E0800),
                memory=SparseMemory(
                    {
                        0x000E0000: 0,
                        PHASE7_PHYSICAL_ZERO_PAGE: bytes(4096),
                    }
                ),
            )
            instructions = {
                instruction.address: instruction
                for function in capsule.functions
                for instruction in function.instructions
            }
            preflight = _FixedSlicePreflight(
                instructions=instructions,
                page_addresses=(PHASE7_PHYSICAL_ZERO_PAGE,),
                memory_ranges=(),
                write_ranges=(),
                function_symbols=(capsule.functions[0].symbol,),
                indirect_targets={},
                host_service_thunks=(),
                host_service_calls=(),
                steps=0,
            )
            plan = _phase3_architecture_plan(
                capsule,
                preflight,
                stop_eip=0x000C0005,
                profile=Ia32ArchitectureProfile(),
                resident_privileged_boundary=True,
            )

        self.assertEqual(plan.instructions[0x000C0000].bytes_hex, "A300000080")
        self.assertEqual(
            plan.page_bindings[0]["mapped_address"],
            PHASE7_PHYSICAL_ZERO_PAGE,
        )
        self.assertEqual(plan.rewrites[0]["kind"], "physical-zero-direct-page")

    def test_direct_mmio_shadow_accepts_register_derived_access(self) -> None:
        self.assertTrue(
            _mappable_data_page(PHASE7_D3D_MMIO_PAGE, detached_guest=True)
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            capsule = _capsule(
                root / "direct-mmio.b2rcap",
                code=bytes.fromhex("8B4044C3"),
                state=CpuState.with_registers(
                    eax=PHASE7_D3D_MMIO_PAGE,
                    esp=0x000E0800,
                ),
                memory=SparseMemory(
                    {
                        0x000E0000: 0,
                        PHASE7_D3D_MMIO_PAGE: bytes(0x44)
                        + bytes.fromhex("4CA81300"),
                    }
                ),
            )
            instructions = {
                instruction.address: instruction
                for function in capsule.functions
                for instruction in function.instructions
            }
            preflight = _FixedSlicePreflight(
                instructions=instructions,
                page_addresses=(PHASE7_D3D_MMIO_PAGE,),
                memory_ranges=((PHASE7_D3D_MMIO_PAGE + 0x44, 4),),
                write_ranges=(),
                function_symbols=(capsule.functions[0].symbol,),
                indirect_targets={},
                host_service_thunks=(),
                host_service_calls=(),
                steps=1,
            )

            plan = _phase3_architecture_plan(
                capsule,
                preflight,
                stop_eip=0x000C0003,
                profile=Ia32ArchitectureProfile(
                    mmio_shadow_pages={PHASE7_D3D_MMIO_PAGE: PHASE7_D3D_MMIO_PAGE}
                ),
                resident_privileged_boundary=True,
            )

        self.assertEqual(plan.rewrites, ())
        self.assertEqual(
            plan.page_bindings[0]["mapped_address"], PHASE7_D3D_MMIO_PAGE
        )
        self.assertEqual(
            plan.page_bindings[0]["logical_addresses"], [PHASE7_D3D_MMIO_PAGE]
        )

    def test_resident_self_clearing_mmio_write_is_address_named_and_discarded(self) -> None:
        self.assertEqual(
            PHASE7_SELF_CLEARING_MMIO_WRITE_SITES,
            frozenset({0x0021AE1A, 0x0021AEB5, 0x0021AF95}),
        )
        plans = []
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for site in sorted(PHASE7_SELF_CLEARING_MMIO_WRITE_SITES):
                capsule = _capsule(
                    root / f"self-clearing-mmio-{site:08x}.b2rcap",
                    code=bytes.fromhex("898810041000C3"),
                    state=CpuState.with_registers(
                        eax=0xFD000000,
                        ecx=0x00010000,
                        esp=0x000E0800,
                    ),
                    memory=SparseMemory(
                        {
                            0x000E0000: 0,
                            0xFD100000: bytes(4096),
                        }
                    ),
                    entry_eip=site,
                )
                instructions = {
                    instruction.address: instruction
                    for function in capsule.functions
                    for instruction in function.instructions
                }
                preflight = _FixedSlicePreflight(
                    instructions=instructions,
                    page_addresses=(0xFD100000,),
                    memory_ranges=((0xFD100410, 4),),
                    write_ranges=((0xFD100410, 4),),
                    function_symbols=(capsule.functions[0].symbol,),
                    indirect_targets={},
                    host_service_thunks=(),
                    host_service_calls=(),
                    steps=1,
                )
                plans.append(
                    (
                        site,
                        _phase3_architecture_plan(
                            capsule,
                            preflight,
                            stop_eip=site + 6,
                            profile=Ia32ArchitectureProfile(
                                mmio_shadow_pages={0xFD100000: 0xFD100000}
                            ),
                            resident_privileged_boundary=True,
                        ),
                    )
                )

        for site, plan in plans:
            self.assertEqual(plan.instructions[site].bytes_hex, "90" * 6)
            self.assertEqual(plan.page_bindings[0]["mapped_address"], 0xFD100000)
            self.assertEqual(
                plan.page_bindings[0]["logical_addresses"], [0xFD100000]
            )
            self.assertEqual(len(plan.rewrites), 1)
            rewrite = plan.rewrites[0]
            self.assertEqual(rewrite["address"], site)
            self.assertEqual(rewrite["kind"], "mmio-self-clearing-command-write")
            self.assertEqual(rewrite["logical_address"], 0xFD100410)
            self.assertEqual(rewrite["command_mask"], 0x00010000)
            self.assertEqual(
                rewrite["write_semantics"], "discard-self-clearing-command"
            )

    def test_resident_gpu_completion_reads_submitted_context_shadow(self) -> None:
        self.assertEqual(
            PHASE7_GPU_COMPLETION_SHADOW_READ_SITES,
            frozenset({0x0021A7D5, 0x0021A7F3}),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            capsule = _capsule(
                root / "gpu-completion-shadow.b2rcap",
                code=bytes.fromhex("8B4944C3"),
                state=CpuState.with_registers(ecx=0x000E0000, esp=0x000E0800),
                memory=SparseMemory({0x000E0000: bytes(4096)}),
                entry_eip=0x0021A7D5,
            )
            instruction = next(
                instruction
                for function in capsule.functions
                for instruction in function.instructions
                if instruction.address == 0x0021A7D5
            )
            preflight = _FixedSlicePreflight(
                instructions={0x0021A7D5: instruction},
                page_addresses=(0x000E0000,),
                memory_ranges=((0x000E0044, 4),),
                write_ranges=(),
                function_symbols=(capsule.functions[0].symbol,),
                indirect_targets={},
                host_service_thunks=(),
                host_service_calls=(),
                steps=1,
            )
            plan = _phase3_architecture_plan(
                capsule,
                preflight,
                stop_eip=0x0021A7D8,
                profile=Ia32ArchitectureProfile(),
                resident_privileged_boundary=True,
            )

        self.assertEqual(plan.instructions[0x0021A7D5].bytes_hex, "8B4940")
        rewrite = plan.rewrites[0]
        self.assertEqual(rewrite["kind"], "mmio-gpu-completion-shadow-read")
        self.assertEqual(rewrite["submitted_offset"], 0x40)
        self.assertEqual(rewrite["completed_offset"], 0x44)

    def test_resident_mcpx_counter_rewrites_complete_register_family(self) -> None:
        register_reads = (
            bytes.fromhex("A1100082FE"),
            bytes.fromhex("8B0D100082FE"),
            bytes.fromhex("8B15100082FE"),
            bytes.fromhex("8B1D100082FE"),
            bytes.fromhex("8B2D100082FE"),
            bytes.fromhex("8B35100082FE"),
            bytes.fromhex("8B3D100082FE"),
        )
        code = b"".join(register_reads) + b"\xc3"
        entry = 0x00180000
        with tempfile.TemporaryDirectory() as temp_dir:
            capsule = _capsule(
                Path(temp_dir) / "mcpx-counter-family.b2rcap",
                code=code,
                state=CpuState.with_registers(esp=0x00190000),
                memory=SparseMemory(
                    {
                        0x0018F000: bytes(4096),
                        0xFE820000: bytes(4096),
                    }
                ),
                entry_eip=entry,
            )
            instructions = {
                instruction.address: instruction
                for function in capsule.functions
                for instruction in function.instructions
            }
            read_instructions = {
                address: instruction
                for address, instruction in instructions.items()
                if any(
                    operand.absolute == PHASE7_MCPX_FRAME_COUNTER_ADDRESS
                    for operand in instruction.operands
                )
            }
            preflight = _FixedSlicePreflight(
                instructions=instructions,
                page_addresses=(0x0018F000, 0xFE820000),
                memory_ranges=((PHASE7_MCPX_FRAME_COUNTER_ADDRESS, 4),),
                write_ranges=(),
                function_symbols=(capsule.functions[0].symbol,),
                indirect_targets={},
                host_service_thunks=(),
                host_service_calls=(),
                steps=len(instructions),
            )
            plan = _phase3_architecture_plan(
                capsule,
                preflight,
                stop_eip=entry + len(code) - 1,
                profile=Ia32ArchitectureProfile(),
                resident_privileged_boundary=True,
            )

        rewrites = [
            rewrite
            for rewrite in plan.rewrites
            if rewrite["kind"] == "mmio-mcpx-frame-counter-read"
        ]
        self.assertEqual(len(read_instructions), 7)
        self.assertEqual(len(rewrites), 7)
        self.assertEqual(
            {rewrite["destination_register"] for rewrite in rewrites},
            {"eax", "ecx", "edx", "ebx", "ebp", "esi", "edi"},
        )
        self.assertTrue(all(rewrite["preserved_eflags"] for rewrite in rewrites))
        self.assertTrue(
            all(
                rewrite["increment"] == PHASE7_MCPX_FRAME_COUNTER_INCREMENT
                for rewrite in rewrites
            )
        )
        self.assertEqual(len(plan.extra_code_spans), 7)
        for address, instruction in read_instructions.items():
            replacement = bytes.fromhex(plan.instructions[address].bytes_hex)
            self.assertEqual(replacement[0], 0xE8)
            self.assertEqual(len(replacement), instruction.size)
        for thunk_address, thunk in plan.extra_code_spans:
            self.assertGreaterEqual(thunk_address, ARCHITECTURE_MCPX_FRAME_COUNTER_THUNK_BASE)
            self.assertEqual(
                (thunk_address - ARCHITECTURE_MCPX_FRAME_COUNTER_THUNK_BASE)
                % ARCHITECTURE_MCPX_FRAME_COUNTER_THUNK_STRIDE,
                0,
            )
            self.assertEqual(thunk[:2], bytes.fromhex("9C83"))
            self.assertEqual(thunk[-2:], bytes.fromhex("9DC3"))

    def test_resident_audio_dsp_control_writes_assert_reset_ready_as_one_family(
        self,
    ) -> None:
        self.assertEqual(
            PHASE7_AUDIO_DSP_CONTROL_WRITE_SITES,
            frozenset({0x00237095, 0x002370A1}),
        )
        plans = []
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for site in sorted(PHASE7_AUDIO_DSP_CONTROL_WRITE_SITES):
                capsule = _capsule(
                    root / f"audio-dsp-ready-{site:08x}.b2rcap",
                    code=bytes.fromhex("A32C01C0FEC3"),
                    state=CpuState.with_registers(
                        eax=PHASE7_AUDIO_DSP_RESET_REQUEST,
                        esp=0x000E0800,
                    ),
                    memory=SparseMemory(
                        {
                            0x000E0000: bytes(4096),
                            0xFEC00000: bytes(4096),
                        }
                    ),
                    entry_eip=site,
                )
                instructions = {
                    instruction.address: instruction
                    for function in capsule.functions
                    for instruction in function.instructions
                }
                preflight = _FixedSlicePreflight(
                    instructions=instructions,
                    page_addresses=(0x000E0000, 0xFEC00000),
                    memory_ranges=(
                        (PHASE7_AUDIO_DSP_CONTROL_ADDRESS, 4),
                        (PHASE7_AUDIO_DSP_STATUS_ADDRESS, 4),
                    ),
                    write_ranges=((PHASE7_AUDIO_DSP_CONTROL_ADDRESS, 4),),
                    function_symbols=(capsule.functions[0].symbol,),
                    indirect_targets={},
                    host_service_thunks=(),
                    host_service_calls=(),
                    steps=len(instructions),
                )
                plans.append(
                    (
                        site,
                        _phase3_architecture_plan(
                            capsule,
                            preflight,
                            stop_eip=site + 5,
                            profile=Ia32ArchitectureProfile(),
                            resident_privileged_boundary=True,
                        ),
                    )
                )

        for site, plan in plans:
            replacement = bytes.fromhex(plan.instructions[site].bytes_hex)
            self.assertEqual(replacement[0], 0xE8)
            self.assertEqual(len(replacement), 5)
            self.assertEqual(len(plan.extra_code_spans), 1)
            thunk_address, thunk = plan.extra_code_spans[0]
            self.assertEqual(thunk_address, ARCHITECTURE_AUDIO_DSP_READY_THUNK_ADDRESS)
            self.assertEqual(thunk[:2], bytes.fromhex("9CA3"))
            self.assertEqual(thunk[-2:], bytes.fromhex("9DC3"))
            rewrite = next(
                record
                for record in plan.rewrites
                if record["kind"] == "mmio-audio-dsp-reset-handshake"
            )
            self.assertEqual(rewrite["address"], site)
            self.assertEqual(
                rewrite["logical_control_address"], PHASE7_AUDIO_DSP_CONTROL_ADDRESS
            )
            self.assertEqual(
                rewrite["logical_status_address"], PHASE7_AUDIO_DSP_STATUS_ADDRESS
            )
            self.assertEqual(rewrite["reset_request_mask"], PHASE7_AUDIO_DSP_RESET_REQUEST)
            self.assertEqual(rewrite["reset_ready_mask"], PHASE7_AUDIO_DSP_RESET_READY)
            self.assertEqual(
                rewrite["mapped_status_address"],
                rewrite["mapped_control_address"] + 4,
            )
            self.assertTrue(rewrite["preserved_eflags"])

    def test_resident_audio_dsp_control_write_rejects_instruction_drift(self) -> None:
        site = min(PHASE7_AUDIO_DSP_CONTROL_WRITE_SITES)
        with tempfile.TemporaryDirectory() as temp_dir:
            capsule = _capsule(
                Path(temp_dir) / "audio-dsp-drift.b2rcap",
                code=bytes.fromhex("A33001C0FEC3"),
                state=CpuState.with_registers(eax=0, esp=0x000E0800),
                memory=SparseMemory(
                    {0x000E0000: bytes(4096), 0xFEC00000: bytes(4096)}
                ),
                entry_eip=site,
            )
            instructions = {
                instruction.address: instruction
                for function in capsule.functions
                for instruction in function.instructions
            }
            preflight = _FixedSlicePreflight(
                instructions=instructions,
                page_addresses=(0x000E0000, 0xFEC00000),
                memory_ranges=((PHASE7_AUDIO_DSP_STATUS_ADDRESS, 4),),
                write_ranges=((PHASE7_AUDIO_DSP_STATUS_ADDRESS, 4),),
                function_symbols=(capsule.functions[0].symbol,),
                indirect_targets={},
                host_service_thunks=(),
                host_service_calls=(),
                steps=len(instructions),
            )
            with self.assertRaisesRegex(
                Ia32BackendError,
                "observed audio DSP control-write family",
            ):
                _phase3_architecture_plan(
                    capsule,
                    preflight,
                    stop_eip=site + 5,
                    profile=Ia32ArchitectureProfile(),
                    resident_privileged_boundary=True,
                )

    def test_resident_audio_buffer_command_self_clears_as_one_family(self) -> None:
        instructions = _audio_buffer_command_instructions()
        original = instructions[PHASE7_AUDIO_BUFFER_COMMAND_IMMEDIATE_SITE]

        rewritten = _resident_audio_buffer_command_rewrite(
            original,
            instructions,
        )

        self.assertEqual(original.operands[0].immediate, PHASE7_AUDIO_BUFFER_COMMAND_VALUE)
        self.assertEqual(rewritten.bytes_hex, "6A00")
        self.assertEqual(len(rewritten.bytes_hex), len(original.bytes_hex))

    def test_resident_audio_buffer_command_rejects_family_drift(self) -> None:
        instructions = _audio_buffer_command_instructions()
        wrong_poll = dict(instructions)
        wrong_poll[0x00230A1A] = lift_x86_function(
            bytes.fromhex("83781001"),
            base_address=0x00230A1A,
            symbol="wrong_audio_buffer_command_poll",
        ).instructions[0]
        missing_poll = dict(instructions)
        del missing_poll[0x00230A1A]

        for candidate in (wrong_poll, missing_poll):
            with self.subTest(instruction_count=len(candidate)):
                with self.assertRaisesRegex(
                    Ia32BackendError,
                    "finite audio buffer-command self-clear family drifted",
                ):
                    _resident_audio_buffer_command_rewrite(
                        instructions[PHASE7_AUDIO_BUFFER_COMMAND_IMMEDIATE_SITE],
                        candidate,
                    )

    def test_resident_audio_buffer_aperture_is_one_bounded_family(self) -> None:
        instructions = _audio_buffer_mmio_instructions()
        _validate_phase7_audio_buffer_mmio_family(instructions)
        self.assertEqual(
            PHASE7_AUDIO_BUFFER_MMIO_END - PHASE7_AUDIO_BUFFER_MMIO_START,
            0x10000,
        )
        self.assertEqual(len(PHASE7_AUDIO_BUFFER_MMIO_PAGES), 16)
        self.assertEqual(
            PHASE7_AUDIO_BUFFER_COPY_CALL_SITES,
            {0x0022C3BD, 0x00231259, 0x00231309},
        )
        self.assertTrue(
            all(
                _mappable_data_page(page, detached_guest=True)
                for page in PHASE7_AUDIO_BUFFER_MMIO_PAGES
            )
        )

        site = 0x00230957
        with tempfile.TemporaryDirectory() as temp_dir:
            capsule = _capsule(
                Path(temp_dir) / "audio-buffer-aperture.b2rcap",
                code=bytes.fromhex(PHASE7_AUDIO_BUFFER_MMIO_FAMILY[site]) + b"\xc3",
                state=CpuState.with_registers(edi=0x1000, esp=0x000E0800),
                memory=SparseMemory(
                    {
                        0x000E0000: bytes(4096),
                        **{
                            page: bytes(4096)
                            for page in PHASE7_AUDIO_BUFFER_MMIO_PAGES
                        },
                    }
                ),
                entry_eip=site,
            )
            instruction = next(
                instruction
                for function in capsule.functions
                for instruction in function.instructions
                if instruction.address == site
            )
            pages = tuple(
                sorted({0x000E0000, *PHASE7_AUDIO_BUFFER_MMIO_PAGES})
            )
            preflight = _FixedSlicePreflight(
                instructions={site: instruction},
                page_addresses=pages,
                memory_ranges=(
                    (
                        PHASE7_AUDIO_BUFFER_MMIO_START,
                        PHASE7_AUDIO_BUFFER_MMIO_END
                        - PHASE7_AUDIO_BUFFER_MMIO_START,
                    ),
                ),
                write_ranges=(),
                function_symbols=(capsule.functions[0].symbol,),
                indirect_targets={},
                host_service_thunks=(),
                host_service_calls=(),
                steps=1,
            )
            plan = _phase3_architecture_plan(
                capsule,
                preflight,
                stop_eip=instruction.next_address,
                profile=Ia32ArchitectureProfile(
                    mmio_shadow_pages={
                        page: page for page in PHASE7_AUDIO_BUFFER_MMIO_PAGES
                    },
                    dirty_page_owners={
                        page: "audio" for page in PHASE7_AUDIO_BUFFER_MMIO_PAGES
                    },
                ),
                resident_privileged_boundary=True,
            )

        rewrite = next(
            record
            for record in plan.rewrites
            if record["kind"]
            == "mmio-audio-buffer-aperture-direct-shadow-reference"
        )
        self.assertEqual(rewrite["page_count"], 16)
        aperture_bindings = [
            record
            for record in plan.page_bindings
            if record["mapped_address"] in PHASE7_AUDIO_BUFFER_MMIO_PAGES
        ]
        self.assertEqual(len(aperture_bindings), 16)
        self.assertTrue(
            all(record["owner"] == "audio" for record in aperture_bindings)
        )

    def test_resident_audio_buffer_aperture_rejects_family_drift(self) -> None:
        instructions = _audio_buffer_mmio_instructions()
        wrong_copy = dict(instructions)
        wrong_copy[0x00230C28] = lift_x86_function(
            bytes.fromhex("F3A4"),
            base_address=0x00230C28,
            symbol="wrong_audio_buffer_copy",
        ).instructions[0]

        extra_caller = dict(instructions)
        caller_site = 0x00240000
        displacement = PHASE7_AUDIO_BUFFER_COPY_TARGET - (caller_site + 5)
        extra_caller[caller_site] = lift_x86_function(
            b"\xe8" + displacement.to_bytes(4, "little", signed=True),
            base_address=caller_site,
            symbol="extra_audio_buffer_copy_caller",
        ).instructions[0]

        extra_reference = dict(instructions)
        reference_site = 0x00240010
        extra_reference[reference_site] = lift_x86_function(
            bytes.fromhex("B8000083FE"),
            base_address=reference_site,
            symbol="extra_audio_buffer_mmio_reference",
        ).instructions[0]

        cases = (
            (wrong_copy, "finite normal-live audio buffer MMIO family"),
            (extra_caller, "audio buffer copy-caller family drifted"),
            (extra_reference, "FE83xxxx audio buffer reference family drifted"),
        )
        for candidate, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(Ia32BackendError, message):
                    _validate_phase7_audio_buffer_mmio_family(candidate)

    def test_resident_audio_voice_commands_self_clear_as_one_family(self) -> None:
        self.assertEqual(
            PHASE7_AUDIO_VOICE_COMMAND_WRITE_BASES,
            {0x00237359: "eax", 0x002377F0: "ecx"},
        )
        plans = []
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for site, register in PHASE7_AUDIO_VOICE_COMMAND_WRITE_BASES.items():
                capsule = _capsule(
                    root / f"audio-voice-command-{site:08x}.b2rcap",
                    code=bytes.fromhex(PHASE7_AUDIO_DSP_MMIO_FAMILY[site]) + b"\xc3",
                    state=CpuState.with_registers(
                        **{register: 0x10, "esp": 0x000E0800}
                    ),
                    memory=SparseMemory(
                        {
                            0x000E0000: bytes(4096),
                            PHASE7_AUDIO_DSP_MMIO_PAGE: bytes(4096),
                        }
                    ),
                    entry_eip=site,
                )
                instruction = next(
                    instruction
                    for function in capsule.functions
                    for instruction in function.instructions
                    if instruction.address == site
                )
                preflight = _FixedSlicePreflight(
                    instructions={site: instruction},
                    page_addresses=(0x000E0000, PHASE7_AUDIO_DSP_MMIO_PAGE),
                    memory_ranges=((PHASE7_AUDIO_DSP_MMIO_PAGE, 4096),),
                    write_ranges=(),
                    function_symbols=(capsule.functions[0].symbol,),
                    indirect_targets={},
                    host_service_thunks=(),
                    host_service_calls=(),
                    steps=1,
                )
                plans.append(
                    _phase3_architecture_plan(
                        capsule,
                        preflight,
                        stop_eip=instruction.next_address,
                        profile=Ia32ArchitectureProfile(
                            mmio_shadow_pages={
                                PHASE7_AUDIO_DSP_MMIO_PAGE: PHASE7_AUDIO_DSP_MMIO_PAGE
                            }
                        ),
                        resident_privileged_boundary=True,
                    )
                )

        self.assertEqual(len(plans), 2)
        for plan in plans:
            rewrite = next(
                record
                for record in plan.rewrites
                if record["kind"] == "mmio-audio-voice-command-self-clear"
            )
            replacement = bytes.fromhex(
                plan.instructions[rewrite["address"]].bytes_hex
            )
            self.assertEqual(replacement[-1], 0)
            self.assertEqual(rewrite["pending_mask"], PHASE7_AUDIO_VOICE_COMMAND_PENDING)
            self.assertTrue(rewrite["preserved_eflags"])

    def test_resident_audio_voice_command_rejects_instruction_drift(self) -> None:
        site = 0x00237359
        with tempfile.TemporaryDirectory() as temp_dir:
            capsule = _capsule(
                Path(temp_dir) / "audio-voice-command-drift.b2rcap",
                code=bytes.fromhex("C6800B01C0FE03C3"),
                state=CpuState.with_registers(eax=0x10, esp=0x000E0800),
                memory=SparseMemory(
                    {
                        0x000E0000: bytes(4096),
                        PHASE7_AUDIO_DSP_MMIO_PAGE: bytes(4096),
                    }
                ),
                entry_eip=site,
            )
            instruction = next(
                instruction
                for function in capsule.functions
                for instruction in function.instructions
                if instruction.address == site
            )
            preflight = _FixedSlicePreflight(
                instructions={site: instruction},
                page_addresses=(0x000E0000, PHASE7_AUDIO_DSP_MMIO_PAGE),
                memory_ranges=((PHASE7_AUDIO_DSP_MMIO_PAGE, 4096),),
                write_ranges=(),
                function_symbols=(capsule.functions[0].symbol,),
                indirect_targets={},
                host_service_thunks=(),
                host_service_calls=(),
                steps=1,
            )
            with self.assertRaisesRegex(
                Ia32BackendError,
                "finite audio voice-command self-clear family",
            ):
                _phase3_architecture_plan(
                    capsule,
                    preflight,
                    stop_eip=instruction.next_address,
                    profile=Ia32ArchitectureProfile(
                        mmio_shadow_pages={
                            PHASE7_AUDIO_DSP_MMIO_PAGE: PHASE7_AUDIO_DSP_MMIO_PAGE
                        }
                    ),
                    resident_privileged_boundary=True,
                )

    def test_resident_audio_sg_family_uses_one_bounded_direct_shadow(self) -> None:
        self.assertTrue(_mappable_data_page(PHASE7_AUDIO_SG_MMIO_PAGE, detached_guest=True))
        exact_instructions = {}
        for site, expected_bytes in PHASE7_AUDIO_SG_MMIO_FAMILY.items():
            exact_instructions[site] = lift_x86_function(
                bytes.fromhex(expected_bytes) + b"\xc3",
                base_address=site,
                symbol=f"audio_sg_{site:08x}",
            ).instructions[0]
        _validate_phase7_audio_sg_mmio_family(exact_instructions)

        plans = []
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for site in sorted(PHASE7_AUDIO_SG_MMIO_ACCESS_SITES):
                instruction = exact_instructions[site]
                capsule = _capsule(
                    root / f"audio-sg-{site:08x}.b2rcap",
                    code=bytes.fromhex(instruction.bytes_hex) + b"\xc3",
                    state=CpuState.with_registers(
                        eax=0x1000,
                        ecx=0x2000,
                        edx=0x3000,
                        edi=0xFE804028,
                        esp=0x000E0800,
                    ),
                    memory=SparseMemory(
                        {
                            0x000E0000: bytes(4096),
                            PHASE7_AUDIO_SG_MMIO_PAGE: bytes(4096),
                        }
                    ),
                    entry_eip=site,
                )
                lifted = next(
                    lifted
                    for function in capsule.functions
                    for lifted in function.instructions
                    if lifted.address == site
                )
                preflight = _FixedSlicePreflight(
                    instructions={site: lifted},
                    page_addresses=(0x000E0000, PHASE7_AUDIO_SG_MMIO_PAGE),
                    memory_ranges=((PHASE7_AUDIO_SG_MMIO_PAGE, 4096),),
                    write_ranges=(),
                    function_symbols=(capsule.functions[0].symbol,),
                    indirect_targets={},
                    host_service_thunks=(),
                    host_service_calls=(),
                    steps=1,
                )
                plans.append(
                    _phase3_architecture_plan(
                        capsule,
                        preflight,
                        stop_eip=lifted.next_address,
                        profile=Ia32ArchitectureProfile(
                            mmio_shadow_pages={
                                PHASE7_AUDIO_SG_MMIO_PAGE: PHASE7_AUDIO_SG_MMIO_PAGE
                            },
                            dirty_page_owners={PHASE7_AUDIO_SG_MMIO_PAGE: "audio"},
                        ),
                        resident_privileged_boundary=True,
                    )
                )

        self.assertEqual(len(plans), 4)
        for plan in plans:
            rewrite = next(
                record
                for record in plan.rewrites
                if record["kind"] == "mmio-audio-sg-direct-shadow-access"
            )
            self.assertEqual(rewrite["family_site_count"], 9)
            binding = next(
                record
                for record in plan.page_bindings
                if record["mapped_address"] == PHASE7_AUDIO_SG_MMIO_PAGE
            )
            self.assertEqual(binding["owner"], "audio")

    def test_resident_audio_sg_family_rejects_instruction_drift(self) -> None:
        instructions = {
            site: lift_x86_function(
                bytes.fromhex(expected_bytes) + b"\xc3",
                base_address=site,
                symbol=f"audio_sg_{site:08x}",
            ).instructions[0]
            for site, expected_bytes in PHASE7_AUDIO_SG_MMIO_FAMILY.items()
        }
        site = 0x00236745
        instructions[site] = lift_x86_function(
            bytes.fromhex("8947F8C3"),
            base_address=site,
            symbol="audio_sg_drift",
        ).instructions[0]
        with self.assertRaisesRegex(
            Ia32BackendError,
            "finite normal-live audio SG MMIO family",
        ):
            _validate_phase7_audio_sg_mmio_family(instructions)

    def test_resident_mcpx_indexed_family_uses_one_bounded_direct_shadow(
        self,
    ) -> None:
        self.assertTrue(_mappable_data_page(PHASE7_MCPX_MMIO_PAGE, detached_guest=True))
        exact_instructions = {
            site: lift_x86_function(
                bytes.fromhex(expected_bytes) + b"\xc3",
                base_address=site,
                symbol=f"mcpx_indexed_{site:08x}",
            ).instructions[0]
            for site, expected_bytes in PHASE7_MCPX_INDEXED_MMIO_FAMILY.items()
        }
        _validate_phase7_mcpx_indexed_mmio_family(exact_instructions)

        plans = []
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for site in sorted(PHASE7_MCPX_INDEXED_MMIO_ACCESS_SITES):
                instruction = exact_instructions[site]
                capsule = _capsule(
                    root / f"mcpx-indexed-{site:08x}.b2rcap",
                    code=bytes.fromhex(instruction.bytes_hex) + b"\xc3",
                    state=CpuState.with_registers(
                        eax=0x1F,
                        ecx=0x2000,
                        edx=0x3F,
                        esi=0x3000,
                        esp=0x000E0800,
                    ),
                    memory=SparseMemory(
                        {
                            0x000E0000: bytes(4096),
                            PHASE7_MCPX_MMIO_PAGE: bytes(4096),
                        }
                    ),
                    entry_eip=site,
                )
                lifted = next(
                    lifted
                    for function in capsule.functions
                    for lifted in function.instructions
                    if lifted.address == site
                )
                preflight = _FixedSlicePreflight(
                    instructions={site: lifted},
                    page_addresses=(0x000E0000, PHASE7_MCPX_MMIO_PAGE),
                    memory_ranges=((PHASE7_MCPX_MMIO_PAGE, 4096),),
                    write_ranges=(),
                    function_symbols=(capsule.functions[0].symbol,),
                    indirect_targets={},
                    host_service_thunks=(),
                    host_service_calls=(),
                    steps=1,
                )
                plans.append(
                    _phase3_architecture_plan(
                        capsule,
                        preflight,
                        stop_eip=lifted.next_address,
                        profile=Ia32ArchitectureProfile(
                            mmio_shadow_pages={
                                PHASE7_MCPX_MMIO_PAGE: PHASE7_MCPX_MMIO_PAGE
                            },
                            dirty_page_owners={PHASE7_MCPX_MMIO_PAGE: "audio"},
                        ),
                        resident_privileged_boundary=True,
                    )
                )

        self.assertEqual(len(plans), 3)
        for plan in plans:
            rewrite = next(
                record
                for record in plan.rewrites
                if record["kind"] == "mmio-mcpx-indexed-direct-shadow-access"
            )
            self.assertEqual(
                rewrite["family_site_count"], len(PHASE7_MCPX_INDEXED_MMIO_FAMILY)
            )
            binding = next(
                record
                for record in plan.page_bindings
                if record["mapped_address"] == PHASE7_MCPX_MMIO_PAGE
            )
            self.assertEqual(binding["owner"], "audio")

    def test_resident_mcpx_indexed_family_rejects_instruction_drift(self) -> None:
        instructions = {
            site: lift_x86_function(
                bytes.fromhex(expected_bytes) + b"\xc3",
                base_address=site,
                symbol=f"mcpx_indexed_{site:08x}",
            ).instructions[0]
            for site, expected_bytes in PHASE7_MCPX_INDEXED_MMIO_FAMILY.items()
        }
        site = 0x00231B61
        instructions[site] = lift_x86_function(
            bytes.fromhex("83E27FC3"),
            base_address=site,
            symbol="mcpx_indexed_drift",
        ).instructions[0]
        with self.assertRaisesRegex(
            Ia32BackendError,
            "finite normal-live MCPX indexed MMIO family",
        ):
            _validate_phase7_mcpx_indexed_mmio_family(instructions)

    def test_resident_mcpx_absolute_access_shares_direct_shadow(self) -> None:
        site = 0x002328B6
        with tempfile.TemporaryDirectory() as temp_dir:
            capsule = _capsule(
                Path(temp_dir) / "mcpx-absolute.b2rcap",
                code=bytes.fromhex("893D040882FEC3"),
                state=CpuState.with_registers(edi=0x1234, esp=0x000E0800),
                memory=SparseMemory(
                    {
                        0x000E0000: bytes(4096),
                        PHASE7_MCPX_MMIO_PAGE: bytes(4096),
                    }
                ),
                entry_eip=site,
            )
            instruction = capsule.functions[0].instructions[0]
            preflight = _FixedSlicePreflight(
                instructions={site: instruction},
                page_addresses=(0x000E0000, PHASE7_MCPX_MMIO_PAGE),
                memory_ranges=((PHASE7_MCPX_MMIO_PAGE, 4096),),
                write_ranges=(),
                function_symbols=(capsule.functions[0].symbol,),
                indirect_targets={},
                host_service_thunks=(),
                host_service_calls=(),
                steps=1,
            )
            plan = _phase3_architecture_plan(
                capsule,
                preflight,
                stop_eip=instruction.next_address,
                profile=Ia32ArchitectureProfile(
                    mmio_shadow_pages={PHASE7_MCPX_MMIO_PAGE: PHASE7_MCPX_MMIO_PAGE},
                    dirty_page_owners={PHASE7_MCPX_MMIO_PAGE: "audio"},
                ),
                resident_privileged_boundary=True,
            )

        rewrite = next(
            record
            for record in plan.rewrites
            if record["kind"] == "mmio-mcpx-direct-shadow-access"
        )
        self.assertEqual(rewrite["logical_address"], 0xFE820804)
        self.assertEqual(rewrite["mapped_address"], 0xFE820804)

    def test_resident_audio_dsp_mmio_family_uses_one_finite_direct_shadow(
        self,
    ) -> None:
        self.assertTrue(
            _mappable_data_page(PHASE7_AUDIO_DSP_MMIO_PAGE, detached_guest=True)
        )
        plans = []
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for site, expected_bytes in sorted(PHASE7_AUDIO_DSP_MMIO_FAMILY.items()):
                capsule = _capsule(
                    root / f"audio-dsp-family-{site:08x}.b2rcap",
                    code=bytes.fromhex(expected_bytes) + b"\xc3",
                    state=CpuState.with_registers(
                        eax=0,
                        ecx=PHASE7_AUDIO_DSP_RESET_REQUEST,
                        edx=0x13,
                        esi=PHASE7_AUDIO_DSP_RESET_READY,
                        esp=0x000E0800,
                    ),
                    memory=SparseMemory(
                        {
                            0x000E0000: bytes(4096),
                            PHASE7_AUDIO_DSP_MMIO_PAGE: bytes(4096),
                        }
                    ),
                    entry_eip=site,
                )
                instruction = next(
                    instruction
                    for function in capsule.functions
                    for instruction in function.instructions
                    if instruction.address == site
                )
                preflight = _FixedSlicePreflight(
                    instructions={site: instruction},
                    page_addresses=(0x000E0000, PHASE7_AUDIO_DSP_MMIO_PAGE),
                    memory_ranges=((PHASE7_AUDIO_DSP_MMIO_PAGE, 4096),),
                    write_ranges=(),
                    function_symbols=(capsule.functions[0].symbol,),
                    indirect_targets={},
                    host_service_thunks=(),
                    host_service_calls=(),
                    steps=1,
                )
                plans.append(
                    (
                        site,
                        _phase3_architecture_plan(
                            capsule,
                            preflight,
                            stop_eip=instruction.next_address,
                            profile=Ia32ArchitectureProfile(
                                mmio_shadow_pages={
                                    PHASE7_AUDIO_DSP_MMIO_PAGE: (
                                        PHASE7_AUDIO_DSP_MMIO_PAGE
                                    )
                                }
                            ),
                            resident_privileged_boundary=True,
                        ),
                    )
                )

        self.assertEqual(len(plans), 18)
        for site, plan in plans:
            rewrite = next(record for record in plan.rewrites if record["address"] == site)
            expected_kind = (
                "mmio-audio-dsp-reset-handshake"
                if site in PHASE7_AUDIO_DSP_CONTROL_WRITE_SITES
                else "mmio-audio-voice-command-self-clear"
                if site in PHASE7_AUDIO_VOICE_COMMAND_WRITE_BASES
                else "mmio-audio-dsp-direct-shadow-access"
            )
            self.assertEqual(rewrite["kind"], expected_kind)
            self.assertEqual(
                next(
                    binding
                    for binding in plan.page_bindings
                    if PHASE7_AUDIO_DSP_MMIO_PAGE
                    in binding["logical_addresses"]
                )["mapped_address"],
                PHASE7_AUDIO_DSP_MMIO_PAGE,
            )

    def test_resident_audio_dsp_direct_shadow_rejects_family_drift(self) -> None:
        site = 0x002370F4
        with tempfile.TemporaryDirectory() as temp_dir:
            capsule = _capsule(
                Path(temp_dir) / "audio-dsp-family-drift.b2rcap",
                code=bytes.fromhex("66890C550200C0FEC3"),
                state=CpuState.with_registers(ecx=1, edx=0x13, esp=0x000E0800),
                memory=SparseMemory(
                    {
                        0x000E0000: bytes(4096),
                        PHASE7_AUDIO_DSP_MMIO_PAGE: bytes(4096),
                    }
                ),
                entry_eip=site,
            )
            instruction = next(
                instruction
                for function in capsule.functions
                for instruction in function.instructions
                if instruction.address == site
            )
            preflight = _FixedSlicePreflight(
                instructions={site: instruction},
                page_addresses=(0x000E0000, PHASE7_AUDIO_DSP_MMIO_PAGE),
                memory_ranges=((PHASE7_AUDIO_DSP_MMIO_PAGE, 4096),),
                write_ranges=(),
                function_symbols=(capsule.functions[0].symbol,),
                indirect_targets={},
                host_service_thunks=(),
                host_service_calls=(),
                steps=1,
            )
            with self.assertRaisesRegex(
                Ia32BackendError,
                "finite normal-live audio DSP MMIO family",
            ):
                _phase3_architecture_plan(
                    capsule,
                    preflight,
                    stop_eip=instruction.next_address,
                    profile=Ia32ArchitectureProfile(
                        mmio_shadow_pages={
                            PHASE7_AUDIO_DSP_MMIO_PAGE: PHASE7_AUDIO_DSP_MMIO_PAGE
                        }
                    ),
                    resident_privileged_boundary=True,
                )

    def test_resident_pfifo_write_one_to_clear_records_zero_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            capsule = _capsule(
                root / "pfifo-write-one-to-clear.b2rcap",
                code=bytes.fromhex("C7860001400000100000C3"),
                state=CpuState.with_registers(esi=0x000E0000, esp=0x000E0800),
                memory=SparseMemory(
                    {0x000E0000: bytes(4096), 0x004E0000: bytes(4096)}
                ),
                entry_eip=PHASE7_PFIFO_WRITE_ONE_TO_CLEAR_SITE,
            )
            instruction = next(
                instruction
                for function in capsule.functions
                for instruction in function.instructions
                if instruction.address == PHASE7_PFIFO_WRITE_ONE_TO_CLEAR_SITE
            )
            preflight = _FixedSlicePreflight(
                instructions={instruction.address: instruction},
                page_addresses=(0x004E0000,),
                memory_ranges=((0x004E0100, 4),),
                write_ranges=((0x004E0100, 4),),
                function_symbols=(capsule.functions[0].symbol,),
                indirect_targets={},
                host_service_thunks=(),
                host_service_calls=(),
                steps=1,
            )
            plan = _phase3_architecture_plan(
                capsule,
                preflight,
                stop_eip=PHASE7_PFIFO_WRITE_ONE_TO_CLEAR_SITE + instruction.size,
                profile=Ia32ArchitectureProfile(),
                resident_privileged_boundary=True,
            )

        rewritten = plan.instructions[PHASE7_PFIFO_WRITE_ONE_TO_CLEAR_SITE]
        self.assertEqual(rewritten.bytes_hex, "C7860001400000000000")
        rewrite = plan.rewrites[0]
        self.assertEqual(rewrite["kind"], "mmio-pfifo-write-one-to-clear")
        self.assertEqual(rewrite["clear_mask"], 0x1000)

    def test_resident_schedule_does_not_advance_dormant_timestamp_wrapper(self) -> None:
        standalone = CpuState.with_registers(timestamp_counter=100)
        _apply_phase3_timestamp_accounting(
            standalone,
            {"phase3_timestamp_step_count": 1},
        )
        resident = CpuState.with_registers(timestamp_counter=100)
        _apply_phase3_timestamp_accounting(
            resident,
            {
                "phase3_timestamp_step_count": 1,
                "resident_scheduler": True,
            },
        )

        self.assertEqual(standalone.timestamp_counter, 100 + DETERMINISTIC_TSC_STEP)
        self.assertEqual(resident.timestamp_counter, 100)

    @unittest.skipUnless(
        IA32_INTEGRATION_AVAILABLE,
        "the IA-32 Phase-3 proof requires the supported Windows LLVM toolchain",
    )
    def test_asset_free_phase3_proofs_cover_each_semantic_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report = run_asset_free_phase3_proofs(Path(temp_dir))

        self.assertEqual(report["format"], PHASE3_PROOF_SET_FORMAT)
        self.assertEqual(report["architecture_contract_id"], ia32_architecture_contract_id())
        self.assertTrue(report["passed"])
        self.assertEqual(report["proof_set_id"], PHASE3_PROOF_SET_ID)
        self.assertEqual(len(report["proofs"]), 5)
        proofs = {proof["name"]: proof for proof in report["proofs"]}
        memory = proofs["tls-stack-alias-mmio-cross-page-dirty"]
        self.assertTrue(memory["checks"]["architectural_match"])
        self.assertTrue(memory["checks"]["cross_page_native_publication"])
        self.assertEqual(
            {record["owner"] for record in memory["dirty_publications"]},
            {"audio", "guest", "renderer"},
        )
        fault = proofs["recoverable-guest-fault"]
        self.assertTrue(fault["checks"]["fault_identity_match"])
        self.assertTrue(fault["checks"]["worker_recovered_for_second_command"])


if __name__ == "__main__":
    unittest.main()
