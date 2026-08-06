from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from tools.playability.replay_capsule import (
    capture_replay_capsule,
    clone_capsule_state,
    cpu_state_record,
    load_replay_capsule,
)
from tools.recomp.ia32_native_backend import (
    ARTIFACT_FORMAT,
    Ia32BackendError,
    build_ia32_slice_artifact,
    ia32_execution_contract_id,
    run_ia32_slice_artifact,
    validate_ia32_artifact_manifest,
    validate_ia32_slice,
)
from tools.recomp.x86_lifter import (
    CpuState,
    SparseMemory,
    X86ExecutionError,
    execute_lifted_function,
    lift_x86_function,
)


def _build_capsule(path: Path, *, code: bytes, base_address: int = 0x000C0000) -> Path:
    function = lift_x86_function(code, base_address=base_address, symbol="ia32_slice_test")
    state = CpuState.with_registers(eax=41, esp=0x000E0000)
    state.eip = base_address
    return capture_replay_capsule(
        path,
        state=state,
        memory=SparseMemory({0x000D0000: 0, 0x000E0000: 0}),
        functions=(function,),
        scheduler_state={},
        service_state={},
        provenance={"synthetic": True, "source": "ia32-backend-test"},
        capture_kind="synthetic",
    )


class Ia32NativeBackendTests(unittest.TestCase):
    def test_fixed_island_ignores_unrelated_decoded_partitions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            primary = lift_x86_function(
                bytes.fromhex("40C3"),
                base_address=0x000C0000,
                symbol="selected_slice",
            )
            unrelated = lift_x86_function(
                bytes.fromhex("FFD0C3"),
                base_address=0x00100000,
                symbol="unrelated_indirect_call",
            )
            state = CpuState.with_registers(eax=41, esp=0x000E0000)
            state.eip = primary.base_address
            capsule_path = capture_replay_capsule(
                root / "partitioned.b2rcap",
                state=state,
                memory=SparseMemory({0x000E0000: 0}),
                functions=(primary, unrelated),
                scheduler_state={},
                service_state={},
                provenance={"synthetic": True},
                capture_kind="synthetic",
            )

            instructions = validate_ia32_slice(
                load_replay_capsule(capsule_path),
                stop_eip=0x000C0001,
            )

        self.assertEqual(set(instructions), {0x000C0000, 0x000C0001})

    def test_external_direct_target_is_an_explicit_static_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            capsule_path = _build_capsule(
                Path(temp_dir) / "external.b2rcap",
                code=bytes.fromhex("E8FBFF0300C3"),
            )
            capsule = load_replay_capsule(capsule_path)

            with self.assertRaisesRegex(Ia32BackendError, "unverified target 0x00100000"):
                validate_ia32_slice(capsule, stop_eip=0x000C0005)

    def test_tls_state_is_an_explicit_static_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            capsule = load_replay_capsule(
                _build_capsule(
                    Path(temp_dir) / "tls.b2rcap",
                    code=bytes.fromhex("40C3"),
                )
            )
            capsule.state.fs_base = 0x1000

            with self.assertRaisesRegex(Ia32BackendError, "TLS rewrite thunk"):
                validate_ia32_slice(capsule, stop_eip=0x000C0001)

    def test_registered_return_constant_service_is_a_verified_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            function = lift_x86_function(
                bytes.fromhex("68EFBEADDEFF1500000D00C3"),
                base_address=0x000C0000,
                symbol="host_service_slice",
            )
            state = CpuState.with_registers(eax=41, esp=0x000E0800)
            state.eip = function.base_address
            capsule = load_replay_capsule(
                capture_replay_capsule(
                    root / "host-service.b2rcap",
                    state=state,
                    memory=SparseMemory(
                        {
                            0x000D0000: 0xE0000690,
                            0x000E0000: 0,
                        }
                    ),
                    functions=(function,),
                    scheduler_state={},
                    service_state={
                        "service_trace": [
                            {
                                "target": 0xE0000690,
                                "shim_name": "RtlEnterCriticalSection",
                                "kind": 1,
                                "value": 0,
                            }
                        ]
                    },
                    provenance={"synthetic": True},
                    capture_kind="synthetic",
                )
            )

            instructions = validate_ia32_slice(capsule, stop_eip=0x000C000B)

        self.assertEqual(set(instructions), {0x000C0000, 0x000C0005, 0x000C000B})

    @unittest.skipUnless(
        sys.platform == "win32" and shutil.which("clang-cl") and shutil.which("lld-link"),
        "the IA-32 PE integration test requires the supported Windows LLVM toolchain",
    )
    def test_rebuilt_pe_matches_accepted_slice_state_and_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            capsule = load_replay_capsule(
                _build_capsule(
                    root / "slice.b2rcap",
                    code=bytes.fromhex("40890500000D00C3"),
                )
            )
            stop_eip = 0x000C0007
            accepted_state, accepted_memory = clone_capsule_state(capsule)
            with self.assertRaises(X86ExecutionError) as accepted_error:
                execute_lifted_function(
                    capsule.combined_function(),
                    state=accepted_state,
                    memory=accepted_memory,
                    max_steps=2,
                    preserve_initial_eip=True,
                    record_instruction_trace=False,
                    record_trace=False,
                )

            artifact = build_ia32_slice_artifact(
                capsule,
                stop_eip=stop_eip,
                build_dir=root / "artifacts",
            )
            direct = run_ia32_slice_artifact(artifact, capsule)
            reused = build_ia32_slice_artifact(
                capsule,
                stop_eip=stop_eip,
                build_dir=root / "artifacts",
            )

            self.assertEqual(accepted_error.exception.steps, 2)
            self.assertEqual(accepted_state.eip, stop_eip)
            self.assertEqual(cpu_state_record(direct.state), cpu_state_record(accepted_state))
            self.assertEqual(direct.memory.export_pages(), accepted_memory.export_pages())
            self.assertEqual(direct.memory.read(0x000D0000, 4), (42).to_bytes(4, "little"))
            self.assertGreater(direct.elapsed_ns, 0)
            self.assertEqual(artifact.artifact_id, reused.artifact_id)
            self.assertEqual(artifact.manifest["format"], ARTIFACT_FORMAT)
            self.assertEqual(
                artifact.manifest["execution_contract_id"], ia32_execution_contract_id()
            )
            self.assertEqual(direct.execution_contract_id, ia32_execution_contract_id())
            self.assertEqual(direct.capsule_id, capsule.capsule_id)
            self.assertEqual(len(direct.toolchain_id), 64)
            self.assertFalse(artifact.manifest["runtime_compilation"])
            self.assertFalse(artifact.manifest["runtime_decoding"])
            self.assertFalse(artifact.manifest["undecoded_xbe_bytes"])
            tampered_manifest = dict(artifact.manifest)
            tampered_manifest["execution_contract_id"] = "0" * 64
            with self.assertRaisesRegex(Ia32BackendError, "execution contract"):
                validate_ia32_artifact_manifest(tampered_manifest)
            pe = artifact.executable.read_bytes()
            pe_offset = int.from_bytes(pe[0x3C:0x40], "little")
            self.assertEqual(pe[:2], b"MZ")
            self.assertEqual(pe[pe_offset : pe_offset + 4], b"PE\0\0")
            self.assertEqual(int.from_bytes(pe[pe_offset + 4 : pe_offset + 6], "little"), 0x14C)

    @unittest.skipUnless(
        sys.platform == "win32" and shutil.which("clang-cl") and shutil.which("lld-link"),
        "the IA-32 PE integration test requires the supported Windows LLVM toolchain",
    )
    def test_rebuilt_pe_executes_native_direct_call_and_return(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            capsule = load_replay_capsule(
                _build_capsule(
                    root / "call-return.b2rcap",
                    code=bytes.fromhex("E80B00000040A300300020909090909040C3"),
                )
            )
            capsule.state.set_register("esp", 0x000E0800)
            artifact = build_ia32_slice_artifact(
                capsule,
                stop_eip=0x000C000B,
                build_dir=root / "artifacts",
            )

            result = run_ia32_slice_artifact(artifact, capsule)

        self.assertEqual(result.state.eip, 0x000C000B)
        self.assertEqual(result.state.get_register("eax"), 43)
        self.assertEqual(result.memory.read_u32(0x20003000), 43)

    @unittest.skipUnless(
        sys.platform == "win32" and shutil.which("clang-cl") and shutil.which("lld-link"),
        "the IA-32 PE integration test requires the supported Windows LLVM toolchain",
    )
    def test_rebuilt_pe_executes_preverified_indirect_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            capsule = load_replay_capsule(
                _build_capsule(
                    root / "indirect-call.b2rcap",
                    code=bytes.fromhex("B810000C00FFD0909090909090909090B82A000000C3"),
                )
            )
            capsule.state.set_register("esp", 0x000E0800)
            artifact = build_ia32_slice_artifact(
                capsule,
                stop_eip=0x000C0007,
                build_dir=root / "artifacts",
            )

            result = run_ia32_slice_artifact(artifact, capsule)

        self.assertEqual(result.state.eip, 0x000C0007)
        self.assertEqual(result.state.get_register("eax"), 42)
        self.assertEqual(
            artifact.manifest["decoded_preflight"]["verified_indirect_targets"],
            {"0x000C0005": ["0x000C0010"]},
        )

    @unittest.skipUnless(
        sys.platform == "win32" and shutil.which("clang-cl") and shutil.which("lld-link"),
        "the IA-32 PE integration test requires the supported Windows LLVM toolchain",
    )
    def test_rebuilt_pe_executes_registered_host_service_thunk(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            function = lift_x86_function(
                bytes.fromhex("68EFBEADDEFF1500000D00C3"),
                base_address=0x000C0000,
                symbol="host_service_slice",
            )
            state = CpuState.with_registers(eax=41, esp=0x000E0800)
            state.eip = function.base_address
            capsule = load_replay_capsule(
                capture_replay_capsule(
                    root / "host-service.b2rcap",
                    state=state,
                    memory=SparseMemory(
                        {
                            0x000D0000: 0xE0000690,
                            0x000E0000: 0,
                        }
                    ),
                    functions=(function,),
                    scheduler_state={},
                    service_state={
                        "service_trace": [
                            {
                                "target": 0xE0000690,
                                "shim_name": "RtlEnterCriticalSection",
                                "kind": 1,
                                "value": 0,
                            }
                        ]
                    },
                    provenance={"synthetic": True},
                    capture_kind="synthetic",
                )
            )
            artifact = build_ia32_slice_artifact(
                capsule,
                stop_eip=0x000C000B,
                build_dir=root / "artifacts",
            )

            result = run_ia32_slice_artifact(artifact, capsule)

        self.assertEqual(result.state.eip, 0x000C000B)
        self.assertEqual(result.state.get_register("eax"), 0)
        self.assertEqual(result.state.get_register("esp"), 0x000E0800)
        self.assertEqual(
            artifact.manifest["decoded_preflight"]["host_service_thunks"][0]["shim_name"],
            "RtlEnterCriticalSection",
        )


if __name__ == "__main__":
    unittest.main()
