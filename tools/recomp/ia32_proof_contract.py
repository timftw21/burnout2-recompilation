#!/usr/bin/env python3
"""Freeze and validate the Phase-0 same-ISA IA-32 proof contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import struct
import sys
import zlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.native_toolchain import sha256_file
from tools.playability.differential_replay import (
    ReplayObservation,
    diagnose_observation_divergence,
)
from tools.playability.replay_capsule import (
    ReplayCapsule,
    ReplayCapsuleError,
    capture_replay_capsule,
    cpu_state_from_record,
    cpu_state_record,
    load_replay_capsule,
)
from tools.recomp.ia32_native_backend import (
    MXCSR_STICKY_STATUS_MASK,
    NATIVE_HOST_SERVICE_CALLBACK,
    NATIVE_HOST_SERVICE_RETURN_CONSTANT,
    NATIVE_HOST_SERVICE_WRITE_U32,
    WORKER_COMMAND_ENTER,
    WORKER_COMMAND_RESUME,
    WORKER_COMMAND_SERVICE_RETURN,
    WORKER_COMMAND_SCHEDULE,
    Ia32BackendError,
    Ia32GuestFault,
    Ia32ArchitectureProfile,
    Ia32CoverageProfile,
    Ia32PersistentWorker,
    Ia32SliceArtifact,
    build_ia32_decoded_store_artifact,
    build_ia32_decoded_store_architecture_artifact,
    build_ia32_architecture_artifact,
    build_ia32_host_abi_artifact,
    build_ia32_resident_scheduler_artifact,
    build_ia32_coverage_growth_artifact,
    build_ia32_launcher_cutover_artifact,
    build_ia32_persistent_slice_artifact,
    build_ia32_slice_artifact,
    execute_ia32_slice_reference,
    execute_ia32_scheduler_reference,
    ia32_execution_contract,
    ia32_execution_contract_id,
    ia32_decoded_store_artifact_contract,
    ia32_decoded_store_artifact_contract_id,
    ia32_persistent_worker_contract,
    ia32_persistent_worker_contract_id,
    ia32_architecture_contract,
    ia32_architecture_contract_id,
    ia32_host_abi_contract,
    ia32_host_abi_contract_id,
    ia32_resident_scheduler_contract,
    ia32_resident_scheduler_contract_id,
    ia32_coverage_growth_contract,
    ia32_coverage_growth_contract_id,
    ia32_launcher_cutover_contract,
    ia32_launcher_cutover_contract_id,
    inspect_ia32_decoded_store,
    load_ia32_slice_artifact,
    normalize_ia32_phase0_state,
    normalize_ia32_phase3_state,
    plan_ia32_coverage_growth,
    run_ia32_slice_artifact,
    select_ia32_launcher_backend,
    validate_ia32_decoded_store_artifact_sources,
)
from tools.recomp.x86_lifter import CpuState, LiftedFunction, SparseMemory, lift_x86_function
from tools.xbe.synthetic_fixture import build_synthetic_xbe


PROOF_SET_FORMAT = "b2-recomp-ia32-phase0-proof-set"
PROOF_SET_VERSION = 1
PHASE1_PROOF_SET_FORMAT = "b2-recomp-ia32-phase1-proof-set"
PHASE1_PROOF_SET_VERSION = 1
PHASE2_PROOF_SET_FORMAT = "b2-recomp-ia32-phase2-proof-set"
PHASE2_PROOF_SET_VERSION = 1
PHASE3_PROOF_SET_FORMAT = "b2-recomp-ia32-phase3-proof-set"
PHASE3_PROOF_SET_VERSION = 2
PHASE3_PROOF_SET_ID = "de4175d8991c9030063570b4927ca482ed38668726d59049e60ebab4ec3eca45"
PHASE4_PROOF_SET_FORMAT = "b2-recomp-ia32-phase4-proof-set"
PHASE4_PROOF_SET_VERSION = 4
PHASE4_PROOF_SET_ID = "73bbca58fa82fe81d18e9aaa783eef4b6786c853787934244dbab067a95233b8"
PHASE5_PROOF_SET_FORMAT = "b2-recomp-ia32-phase5-proof-set"
PHASE5_PROOF_SET_VERSION = 4
PHASE5_PROOF_SET_ID = "6041247e49b60c22041010f2e5eba6bdb4cf12604e3655a5db1a542e20841c1c"
PHASE6_PROOF_SET_FORMAT = "b2-recomp-ia32-phase6-proof-set"
PHASE6_PROOF_SET_VERSION = 4
PHASE6_PROOF_SET_ID = "9a614ef70b5e5cd4ccc8eeffd662c2df15af504afba2d3bd0f36017b23e6727a"
PHASE7_PROOF_SET_FORMAT = "b2-recomp-ia32-phase7-proof-set"
PHASE7_PROOF_SET_VERSION = 3
PHASE7_PROOF_SET_ID = "a7e5a5a845c50c5aacf49501ba9aadcf6cee632df3f03b96916795c14b29b412"
COMPUTE_ENTRY_EIP = 0x000C0000
COMPUTE_STOP_EIP = 0x000C0007
SERVICE_ENTRY_EIP = 0x000C1000
SERVICE_STOP_EIP = 0x000C100B
SYNTHETIC_SERVICE_TARGET = 0xE0000690
HOST_ABI_ENTRY_EIP = 0x000C4000
HOST_ABI_STOP_EIP = 0x000C4048
HOST_ABI_CALLBACK_EIP = 0x000C5000
HOST_ABI_SERVICE_TARGETS = (0xE0001000, 0xE0001010, 0xE0001020, 0xE0001030)
SCHEDULER_STOP_EIP = 0x000C60F0
SCHEDULER_TLS_HELPER_EIP = 0x000C6100
SCHEDULER_SERVICE_TARGET = 0xE0001100
SCHEDULER_SERVICE_POINTER = 0x000D0000
SCHEDULER_RENDER_ADDRESS = 0x000D2000
SCHEDULER_AUDIO_ADDRESS = 0x000D3000
SCHEDULER_ACTIVE_TLS = 0x70000000
SCHEDULER_LANE_TLS = (0x71000000, 0x71001000, 0x71002000)
SCHEDULER_STEP_EIPS = (
    0x000C6000,
    0x000C6020,
    0x000C6040,
    0x000C6060,
    0x000C6080,
    0x000C60A0,
    0x000C60C0,
)
ASSET_FREE_COMPUTE_CAPSULE_ID = "72a63d2e7afeeb6b108ab3a3e1ba153a288f5f570658e6260cc00ccc3b8d6afd"
ASSET_FREE_SERVICE_CAPSULE_ID = "62fd475b7f928c211daea121982d54f145f55e9726fba622f7b8ac9b6908d584"


@dataclass(frozen=True)
class Phase0ProofFixture:
    name: str
    kind: str
    capsule: ReplayCapsule
    stop_eip: int


@dataclass(frozen=True)
class Phase2ProofFixture:
    capsule: ReplayCapsule
    stop_eip: int
    xbe_path: Path
    decoded_block_store_path: Path


@dataclass(frozen=True)
class Phase3ProofFixture:
    name: str
    capsule: ReplayCapsule
    stop_eip: int
    profile: Ia32ArchitectureProfile
    expected_dirty_owners: tuple[str, ...] = ()
    recoverable_fault: bool = False
    xbe_path: Path | None = None
    decoded_block_store_path: Path | None = None


@dataclass(frozen=True)
class Phase4ProofFixture:
    capsule: ReplayCapsule
    stop_eip: int
    expected_service_order: tuple[str, ...]


@dataclass(frozen=True)
class Phase5ProofFixture:
    capsule: ReplayCapsule
    stop_eip: int
    expected_lane_order: tuple[str, ...]
    expected_service_order: tuple[str, ...]
    contained_fault: bool = False


@dataclass(frozen=True)
class Phase6ProofFixture:
    scheduler: Phase5ProofFixture
    xbe_path: Path
    decoded_block_store_path: Path
    coverage_profile: Ia32CoverageProfile


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _memory_identity(memory: SparseMemory) -> str:
    page_hashes = [
        {"page_index": index, "sha256": _sha256(payload)}
        for index, payload in memory.export_pages()
    ]
    return _sha256(_canonical_json(page_hashes))


def _state_identity(state: CpuState) -> str:
    return _sha256(_canonical_json(cpu_state_record(state)))


def _artifact_stop_eip(artifact: Ia32SliceArtifact) -> int:
    inputs = artifact.manifest.get("inputs")
    if not isinstance(inputs, dict):
        raise Ia32BackendError("IA-32 artifact omits its input record")
    try:
        return int(inputs["stop_eip"])
    except (KeyError, TypeError, ValueError) as exc:
        raise Ia32BackendError("IA-32 artifact omits its stop EIP") from exc


def _host_service_records(artifact: Ia32SliceArtifact) -> list[dict[str, Any]]:
    preflight = artifact.manifest.get("decoded_preflight")
    if not isinstance(preflight, dict):
        raise Ia32BackendError("IA-32 artifact omits its decoded preflight")
    records = preflight.get("host_service_thunks", [])
    if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
        raise Ia32BackendError("IA-32 artifact host-service thunk records are malformed")
    return [dict(record) for record in records]


def _role_errors(kind: str, artifact: Ia32SliceArtifact) -> list[str]:
    services = _host_service_records(artifact)
    if kind == "compute":
        return [] if not services else ["compute proof unexpectedly invokes a host service"]
    if kind != "host-service":
        return [f"unknown Phase-0 proof kind {kind!r}"]
    matching = [
        record
        for record in services
        if record.get("shim_name") == "RtlEnterCriticalSection"
        and int(record.get("target", -1)) == SYNTHETIC_SERVICE_TARGET
        and int(record.get("stack_cleanup_bytes", -1)) == 4
    ]
    if len(services) != 1 or len(matching) != 1:
        return [
            "host-service proof must contain exactly one RtlEnterCriticalSection "
            "return-constant thunk with four-byte cleanup"
        ]
    return []


def _evaluate_proof(
    fixture: Phase0ProofFixture,
    artifact: Ia32SliceArtifact,
    *,
    identity_peer: Ia32SliceArtifact | None = None,
    expected_capsule_id: str | None = None,
    expected_artifact_id: str | None = None,
    normalize_historical_mmx_alias: bool = False,
) -> dict[str, Any]:
    inputs = artifact.manifest.get("inputs")
    if not isinstance(inputs, dict) or inputs.get("capsule_id") != fixture.capsule.capsule_id:
        raise Ia32BackendError(f"{fixture.name} artifact was built for a different capsule")
    if _artifact_stop_eip(artifact) != fixture.stop_eip:
        raise Ia32BackendError(f"{fixture.name} artifact has the wrong stop EIP")

    reference = execute_ia32_slice_reference(fixture.capsule, stop_eip=fixture.stop_eip)
    native = run_ia32_slice_artifact(artifact, fixture.capsule)
    accepted_state = normalize_ia32_phase0_state(reference.state)
    native_state = normalize_ia32_phase0_state(native.state)
    if normalize_historical_mmx_alias:
        for name in accepted_state.mmx_registers:
            accepted_state.mmx_registers[name] = 0
            native_state.mmx_registers[name] = 0
    normalization = {
        "execution_contract_id": ia32_execution_contract_id(),
        "reserved_x87_control_bits_removed": True,
        "mxcsr_sticky_status": {
            "accepted": reference.state.mxcsr & MXCSR_STICKY_STATUS_MASK,
            "native": native.state.mxcsr & MXCSR_STICKY_STATUS_MASK,
            "accepted_hex": f"0x{reference.state.mxcsr & MXCSR_STICKY_STATUS_MASK:02X}",
            "native_hex": f"0x{native.state.mxcsr & MXCSR_STICKY_STATUS_MASK:02X}",
            "compared": False,
        },
        "historical_mmx_x87_alias_excluded": normalize_historical_mmx_alias,
    }
    accepted = ReplayObservation(
        backend="decoded-reference",
        prefix_steps=reference.steps,
        steps_executed=reference.steps,
        exit_reason="fixed-slice-stop",
        state=accepted_state,
        memory=reference.memory,
        events=fixture.capsule.events,
    )
    experimental = ReplayObservation(
        backend="same-isa-ia32",
        prefix_steps=reference.steps,
        steps_executed=reference.steps,
        exit_reason="fixed-slice-stop",
        state=native_state,
        memory=native.memory,
        events=fixture.capsule.events,
    )
    role_errors = _role_errors(fixture.kind, artifact)
    identity_errors = []
    if expected_capsule_id is not None and fixture.capsule.capsule_id != expected_capsule_id:
        identity_errors.append("capsule identity differs from the retained proof")
    if expected_artifact_id is not None and artifact.artifact_id != expected_artifact_id:
        identity_errors.append("artifact identity differs from the retained proof")
    architectural_match = accepted.architectural_hash() == experimental.architectural_hash()
    peer_manifest_sha256 = (
        sha256_file(identity_peer.manifest_path) if identity_peer is not None else None
    )
    artifact_reproducible = (
        None
        if identity_peer is None
        else (
            artifact.artifact_id == identity_peer.artifact_id
            and sha256_file(artifact.manifest_path) == peer_manifest_sha256
            and sha256_file(artifact.executable) == sha256_file(identity_peer.executable)
        )
    )
    if artifact_reproducible is False:
        identity_errors.append("independent artifact rebuild changed its identity")

    manifest_contract_id = str(
        artifact.manifest.get("execution_contract_id", "historical-prototype")
    )
    stable_identity = {
        "name": fixture.name,
        "kind": fixture.kind,
        "capsule_id": fixture.capsule.capsule_id,
        "capsule_file_sha256": sha256_file(fixture.capsule.path),
        "artifact_id": artifact.artifact_id,
        "artifact_manifest_sha256": sha256_file(artifact.manifest_path),
        "artifact_executable_sha256": sha256_file(artifact.executable),
        "artifact_execution_contract_id": manifest_contract_id,
        "toolchain_id": native.toolchain_id,
        "stop_eip": fixture.stop_eip,
        "accepted_architectural_hash": accepted.architectural_hash(),
        "native_architectural_hash": experimental.architectural_hash(),
        "normalization": normalization,
    }
    passed = architectural_match and not role_errors and not identity_errors
    report: dict[str, Any] = {
        **stable_identity,
        "proof_id": _sha256(_canonical_json(stable_identity)),
        "passed": passed,
        "artifact_reproducible": artifact_reproducible,
        "artifact_identity_status": (
            "sealed-existing"
            if identity_peer is None
            else "independent-rebuild-match"
            if artifact_reproducible
            else "independent-rebuild-mismatch"
        ),
        "role_errors": role_errors,
        "identity_errors": identity_errors,
        "capsule": {
            "path": str(fixture.capsule.path),
            "entry_eip": fixture.capsule.state.eip,
            "entry_eip_hex": f"0x{fixture.capsule.state.eip:08X}",
            "memory_page_count": len(fixture.capsule.memory.export_pages()),
        },
        "artifact": {
            "path": str(artifact.root),
            "manifest": str(artifact.manifest_path),
            "executable": str(artifact.executable),
            "peer_manifest_sha256": peer_manifest_sha256,
            "host_service_thunks": _host_service_records(artifact),
        },
        "reference": {
            "steps": reference.steps,
            "state_id": _state_identity(accepted_state),
            "memory_id": _memory_identity(reference.memory),
            "architectural_hash": accepted.architectural_hash(),
        },
        "native": {
            "state_id": _state_identity(native_state),
            "memory_id": _memory_identity(native.memory),
            "architectural_hash": experimental.architectural_hash(),
        },
        "timing": {
            "protocol_id": ia32_execution_contract()["timing_protocol_id"],
            "elapsed_ns": native.elapsed_ns,
            "process_elapsed_ns": native.process_elapsed_ns,
        },
    }
    if not architectural_match:
        report["divergence"] = diagnose_observation_divergence(
            accepted,
            experimental,
            compact=True,
        )
    return report


def build_asset_free_phase0_capsules(root: Path) -> tuple[Phase0ProofFixture, ...]:
    root.mkdir(parents=True, exist_ok=True)
    compute_function = lift_x86_function(
        bytes.fromhex("40890500000D00C3"),
        base_address=COMPUTE_ENTRY_EIP,
        symbol="phase0_compute_increment_store",
    )
    compute_state = CpuState.with_registers(eax=41, esp=0x000E0800)
    compute_state.eip = COMPUTE_ENTRY_EIP
    compute_state.fpu_control_word = 0x033F
    compute_path = capture_replay_capsule(
        root / "compute.b2rcap",
        state=compute_state,
        memory=SparseMemory({0x000D0000: 0, 0x000E0000: 0}),
        functions=(compute_function,),
        scheduler_state={"lane": "primary", "slice": 0},
        service_state={"service_trace": []},
        provenance={
            "synthetic": True,
            "manual_capture": False,
            "source": "ia32-phase0-contract",
            "proof": "compute",
        },
        capture_kind="synthetic",
    )

    service_function = lift_x86_function(
        bytes.fromhex("68EFBEADDEFF1500000D00C3"),
        base_address=SERVICE_ENTRY_EIP,
        symbol="phase0_rtl_enter_critical_section",
    )
    service_state = CpuState.with_registers(eax=41, esp=0x000E0800)
    service_state.eip = SERVICE_ENTRY_EIP
    service_state.fpu_control_word = 0x033F
    service_path = capture_replay_capsule(
        root / "host-service.b2rcap",
        state=service_state,
        memory=SparseMemory({0x000D0000: SYNTHETIC_SERVICE_TARGET, 0x000E0000: 0}),
        functions=(service_function,),
        scheduler_state={"lane": "primary", "slice": 0},
        service_state={
            "service_trace": [
                {
                    "target": SYNTHETIC_SERVICE_TARGET,
                    "shim_name": "RtlEnterCriticalSection",
                    "kind": 1,
                    "value": 0,
                }
            ]
        },
        provenance={
            "synthetic": True,
            "manual_capture": False,
            "source": "ia32-phase0-contract",
            "proof": "host-service",
        },
        capture_kind="synthetic",
    )
    fixtures = (
        Phase0ProofFixture(
            "asset-free-compute",
            "compute",
            load_replay_capsule(compute_path),
            COMPUTE_STOP_EIP,
        ),
        Phase0ProofFixture(
            "asset-free-host-service",
            "host-service",
            load_replay_capsule(service_path),
            SERVICE_STOP_EIP,
        ),
    )
    expected_ids = {
        "asset-free-compute": ASSET_FREE_COMPUTE_CAPSULE_ID,
        "asset-free-host-service": ASSET_FREE_SERVICE_CAPSULE_ID,
    }
    for fixture in fixtures:
        if fixture.capsule.capsule_id != expected_ids[fixture.name]:
            raise Ia32BackendError(
                f"the frozen {fixture.name} capsule changed without a proof-contract version bump"
            )
    return fixtures


def _write_phase2_decoded_store(
    path: Path,
    *,
    xbe_sha256: str,
    functions: Sequence[LiftedFunction],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            DROP TABLE IF EXISTS cache_metadata;
            DROP TABLE IF EXISTS decoded_blocks;
            CREATE TABLE cache_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE decoded_blocks (
                cache_key TEXT PRIMARY KEY,
                image_sha256 TEXT NOT NULL,
                target INTEGER NOT NULL,
                entry_bytes INTEGER NOT NULL,
                max_block_instructions INTEGER NOT NULL,
                payload BLOB NOT NULL,
                payload_bytes INTEGER NOT NULL,
                last_used_ns INTEGER NOT NULL,
                access_count INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        connection.executemany(
            "INSERT INTO cache_metadata(key, value) VALUES(?, ?)",
            (("version", "3"), ("payload_format", "zlib-json-blob")),
        )
        for function in functions:
            record = function.to_dict(include_bytes=False)
            payload = zlib.compress(
                json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8"),
                level=6,
            )
            cache_key = ":".join(
                (
                    "3",
                    xbe_sha256.upper(),
                    f"0x{function.base_address:08X}",
                    "512",
                    "224",
                )
            )
            connection.execute(
                """
                INSERT INTO decoded_blocks(
                    cache_key, image_sha256, target, entry_bytes,
                    max_block_instructions, payload, payload_bytes,
                    last_used_ns, access_count
                ) VALUES(?, ?, ?, ?, ?, ?, ?, 0, 0)
                """,
                (
                    cache_key,
                    xbe_sha256.upper(),
                    function.base_address,
                    512,
                    224,
                    payload,
                    len(payload),
                ),
            )
        connection.commit()
    finally:
        connection.close()


def build_asset_free_phase2_fixture(
    root: Path,
    *,
    code: bytes | None = None,
    stop_offset: int = 0x0B,
) -> Phase2ProofFixture:
    root.mkdir(parents=True, exist_ok=True)
    default_program = code is None
    if default_program:
        active_code = (
            bytes.fromhex("E81B000000B950000C00FFD1A30030002090C3")
            + b"\x90" * 13
            + bytes.fromhex("40C3")
            + b"\x90" * 14
            + bytes.fromhex("40C3")
        )
        stop_offset = 0x11
    else:
        assert code is not None
        active_code = code
    xbe_payload, layout = build_synthetic_xbe(text_payload=active_code)
    xbe_path = root / "phase2-fixture.xbe"
    xbe_path.write_bytes(xbe_payload)
    entry_eip = int(layout["entry_eip"])
    if default_program:
        functions: tuple[LiftedFunction, ...] = (
            lift_x86_function(
                active_code[:19],
                base_address=entry_eip,
                symbol="phase2_direct_and_indirect_entry",
            ),
            lift_x86_function(
                active_code[0x20:0x22],
                base_address=entry_eip + 0x20,
                symbol="phase2_direct_target",
            ),
            lift_x86_function(
                active_code[0x30:0x32],
                base_address=entry_eip + 0x30,
                symbol="phase2_indirect_target",
            ),
        )
    else:
        functions = (
            lift_x86_function(
                active_code,
                base_address=entry_eip,
                symbol="phase2_custom_store",
            ),
        )
    state = CpuState.with_registers(eax=41, esp=0x000E0800)
    state.eip = entry_eip
    state.fpu_control_word = 0x033F
    capsule_path = capture_replay_capsule(
        root / "phase2-direct-call.b2rcap",
        state=state,
        memory=SparseMemory({0x000E0000: 0, 0x20003000: 0}),
        functions=functions,
        scheduler_state={"lane": "primary", "slice": 0},
        service_state={"service_trace": []},
        provenance={
            "synthetic": True,
            "manual_capture": False,
            "source": "ia32-phase2-contract",
        },
        capture_kind="synthetic",
    )
    store_path = root / "decoded-blocks.sqlite3"
    _write_phase2_decoded_store(
        store_path,
        xbe_sha256=_sha256(xbe_payload),
        functions=functions,
    )
    return Phase2ProofFixture(
        capsule=load_replay_capsule(capsule_path),
        stop_eip=entry_eip + stop_offset,
        xbe_path=xbe_path,
        decoded_block_store_path=store_path,
    )


def _capture_phase3_fixture(
    root: Path,
    *,
    name: str,
    code: bytes,
    stop_offset: int,
    state: CpuState,
    memory: SparseMemory,
    profile: Ia32ArchitectureProfile | None = None,
    expected_dirty_owners: tuple[str, ...] = (),
    recoverable_fault: bool = False,
) -> Phase3ProofFixture:
    base_address = 0x000C0000
    state.eip = base_address
    capsule_path = capture_replay_capsule(
        root / f"{name}.b2rcap",
        state=state,
        memory=memory,
        functions=(
            lift_x86_function(
                code,
                base_address=base_address,
                symbol=f"phase3_{name.replace('-', '_')}",
            ),
        ),
        scheduler_state={"lane": "primary", "slice": 0},
        service_state={"service_trace": []},
        provenance={
            "synthetic": True,
            "manual_capture": False,
            "source": "ia32-phase3-contract",
            "proof": name,
        },
        capture_kind="synthetic",
    )
    return Phase3ProofFixture(
        name=name,
        capsule=load_replay_capsule(capsule_path),
        stop_eip=base_address + stop_offset,
        profile=profile or Ia32ArchitectureProfile(),
        expected_dirty_owners=expected_dirty_owners,
        recoverable_fault=recoverable_fault,
    )


def build_asset_free_phase3_fixtures(root: Path) -> tuple[Phase3ProofFixture, ...]:
    """Create deterministic capsules for every Phase-3 semantic boundary."""

    root.mkdir(parents=True, exist_ok=True)

    def stack_memory() -> SparseMemory:
        return SparseMemory({0x000E0000: bytes(4096)})

    x87_state = CpuState.with_registers(esp=0x000E0800)
    x87_state.fpu_stack = [2.5]
    x87 = _capture_phase3_fixture(
        root,
        name="x87-stack",
        code=bytes.fromhex("D9E8C3"),
        stop_offset=2,
        state=x87_state,
        memory=stack_memory(),
    )

    simd_state = CpuState.with_registers(esp=0x000E0800)
    simd_state.set_mmx_register("mm0", 0x1122334455667788)
    simd_state.set_xmm_register("xmm0", (1.0, 2.0, 3.0, 4.0))
    simd_state.set_xmm_register("xmm1", (0.5, 1.5, 2.5, 3.5))
    simd = _capture_phase3_fixture(
        root,
        name="mmx-sse-alias",
        code=bytes.fromhex("0F6FC80F58C1C3"),
        stop_offset=6,
        state=simd_state,
        memory=stack_memory(),
    )

    memory_code = (
        bytes.fromhex(
            "64A13000000040"
            "64A334000000"
            "A1040000A040A3080000A0"
            "A10000D0FD40A30400D0FD"
            "C705FF0F0D0078563412"
            "E80E000000"
        )
        + b"\x90" * 14
        + bytes.fromhex("40C3")
    )
    memory_state = CpuState.with_registers(esp=0x000E0800, fs_base=0x70000000)
    memory = SparseMemory(
        {
            0x000D0000: bytes(4096),
            0x000D1000: bytes(4096),
            0x000E0000: bytes(4096),
            0x70000030: 41,
            0xA0000004: 10,
            0xFDD00000: 20,
        }
    )
    memory_semantics = _capture_phase3_fixture(
        root,
        name="tls-stack-alias-mmio-cross-page-dirty",
        code=memory_code,
        stop_offset=50,
        state=memory_state,
        memory=memory,
        profile=Ia32ArchitectureProfile(
            dirty_page_owners={
                0x000D0000: "audio",
                0x000D1000: "audio",
            }
        ),
        expected_dirty_owners=("audio", "guest", "renderer"),
    )

    timestamp_store = build_asset_free_phase2_fixture(
        root / "privileged-rdtsc-store",
        code=bytes.fromhex("0F31C3"),
        stop_offset=2,
    )
    timestamp = Phase3ProofFixture(
        name="privileged-rdtsc-decoded-store",
        capsule=timestamp_store.capsule,
        stop_eip=timestamp_store.stop_eip,
        profile=Ia32ArchitectureProfile(),
        xbe_path=timestamp_store.xbe_path,
        decoded_block_store_path=timestamp_store.decoded_block_store_path,
    )

    fault_state = CpuState.with_registers(esp=0x000E0800)
    fault = _capture_phase3_fixture(
        root,
        name="recoverable-guest-fault",
        code=bytes.fromhex("A100000D00C3"),
        stop_offset=5,
        state=fault_state,
        memory=SparseMemory(
            {
                0x000D0000: 42,
                0x000E0000: bytes(4096),
            }
        ),
        profile=Ia32ArchitectureProfile(recoverable_fault_pages=(0x000D0000,)),
        recoverable_fault=True,
    )
    return (x87, simd, memory_semantics, timestamp, fault)


def _proof_set_report(proofs: Sequence[dict[str, Any]], *, kind: str) -> dict[str, Any]:
    contract = ia32_execution_contract()
    stable_identity = {
        "format": PROOF_SET_FORMAT,
        "version": PROOF_SET_VERSION,
        "kind": kind,
        "execution_contract_id": ia32_execution_contract_id(),
        "proof_ids": [str(proof["proof_id"]) for proof in proofs],
    }
    return {
        **stable_identity,
        "proof_set_id": _sha256(_canonical_json(stable_identity)),
        "passed": bool(proofs) and all(bool(proof["passed"]) for proof in proofs),
        "execution_contract": contract,
        "proofs": list(proofs),
    }


def run_asset_free_phase0_proofs(build_dir: Path) -> dict[str, Any]:
    first = build_asset_free_phase0_capsules(build_dir / "capsules-a")
    second = build_asset_free_phase0_capsules(build_dir / "capsules-b")
    second_by_name = {fixture.name: fixture for fixture in second}
    proofs = []
    for fixture in first:
        peer_fixture = second_by_name[fixture.name]
        if fixture.capsule.capsule_id != peer_fixture.capsule.capsule_id:
            raise Ia32BackendError(f"{fixture.name} capsule rebuild changed its identity")
        artifact = build_ia32_slice_artifact(
            fixture.capsule,
            stop_eip=fixture.stop_eip,
            build_dir=build_dir / "artifacts-a",
        )
        peer_artifact = build_ia32_slice_artifact(
            peer_fixture.capsule,
            stop_eip=peer_fixture.stop_eip,
            build_dir=build_dir / "artifacts-b",
        )
        proofs.append(
            _evaluate_proof(
                fixture,
                artifact,
                identity_peer=peer_artifact,
            )
        )
    return _proof_set_report(proofs, kind="asset-free")


def _evaluate_persistent_proof(
    fixture: Phase0ProofFixture,
    artifact: Ia32SliceArtifact,
    identity_peer: Ia32SliceArtifact,
) -> dict[str, Any]:
    reference = execute_ia32_slice_reference(fixture.capsule, stop_eip=fixture.stop_eip)
    accepted_state = normalize_ia32_phase0_state(reference.state)
    accepted = ReplayObservation(
        backend="decoded-reference",
        prefix_steps=reference.steps,
        steps_executed=reference.steps,
        exit_reason="fixed-slice-stop",
        state=accepted_state,
        memory=reference.memory,
        events=fixture.capsule.events,
    )
    commands = (
        (WORKER_COMMAND_ENTER, WORKER_COMMAND_RESUME, WORKER_COMMAND_ENTER)
        if fixture.kind == "compute"
        else (
            WORKER_COMMAND_ENTER,
            WORKER_COMMAND_SERVICE_RETURN,
            WORKER_COMMAND_RESUME,
        )
    )
    command_names = {
        WORKER_COMMAND_ENTER: "enter",
        WORKER_COMMAND_RESUME: "resume",
        WORKER_COMMAND_SERVICE_RETURN: "service-return",
    }
    dispatches = []
    divergences = []
    with Ia32PersistentWorker.start(artifact, fixture.capsule) as worker:
        process_id = worker.process_id
        startup_ns = worker.startup_ns
        for command in commands:
            native = worker.dispatch(fixture.capsule, command=command)
            native_state = normalize_ia32_phase0_state(native.state)
            experimental = ReplayObservation(
                backend="same-isa-ia32-resident",
                prefix_steps=reference.steps,
                steps_executed=reference.steps,
                exit_reason="fixed-slice-stop",
                state=native_state,
                memory=native.memory,
                events=fixture.capsule.events,
            )
            architectural_match = accepted.architectural_hash() == experimental.architectural_hash()
            if not architectural_match:
                divergences.append(
                    diagnose_observation_divergence(accepted, experimental, compact=True)
                )
            dispatches.append(
                {
                    "index": native.worker_dispatch_index,
                    "command": command_names[command],
                    "process_id": native.worker_process_id,
                    "state_id": _state_identity(native_state),
                    "memory_id": _memory_identity(native.memory),
                    "architectural_hash": experimental.architectural_hash(),
                    "architectural_match": architectural_match,
                    "elapsed_ns": native.elapsed_ns,
                    "resident_dispatch_ns": native.resident_dispatch_ns,
                }
            )
    artifact_reproducible = (
        artifact.artifact_id == identity_peer.artifact_id
        and sha256_file(artifact.manifest_path) == sha256_file(identity_peer.manifest_path)
        and sha256_file(artifact.executable) == sha256_file(identity_peer.executable)
    )
    same_process = all(item["process_id"] == process_id for item in dispatches)
    reset_isolated = (
        len({(str(item["state_id"]), str(item["memory_id"])) for item in dispatches}) == 1
    )
    stable_identity = {
        "name": fixture.name,
        "kind": fixture.kind,
        "capsule_id": fixture.capsule.capsule_id,
        "artifact_id": artifact.artifact_id,
        "persistent_worker_contract_id": ia32_persistent_worker_contract_id(),
        "toolchain_id": artifact.manifest["toolchain"]["toolchain_id"],
        "stop_eip": fixture.stop_eip,
        "commands": [command_names[command] for command in commands],
        "accepted_architectural_hash": accepted.architectural_hash(),
        "dispatch_architectural_hashes": [item["architectural_hash"] for item in dispatches],
        "normal_runtime_python_callbacks": 0,
    }
    passed = (
        bool(dispatches)
        and all(bool(item["architectural_match"]) for item in dispatches)
        and same_process
        and reset_isolated
        and artifact_reproducible
        and not _role_errors(fixture.kind, artifact)
    )
    report: dict[str, Any] = {
        **stable_identity,
        "proof_id": _sha256(_canonical_json(stable_identity)),
        "passed": passed,
        "artifact_reproducible": artifact_reproducible,
        "resident_process_count": 1
        if same_process
        else len({item["process_id"] for item in dispatches}),
        "same_process": same_process,
        "reset_isolated": reset_isolated,
        "worker_process_id": process_id,
        "worker_startup_ns": startup_ns,
        "dispatches": dispatches,
    }
    if divergences:
        report["divergences"] = divergences
    return report


def run_asset_free_phase1_proofs(build_dir: Path) -> dict[str, Any]:
    first = build_asset_free_phase0_capsules(build_dir / "capsules-a")
    second = build_asset_free_phase0_capsules(build_dir / "capsules-b")
    second_by_name = {fixture.name: fixture for fixture in second}
    proofs = []
    for fixture in first:
        peer_fixture = second_by_name[fixture.name]
        artifact = build_ia32_persistent_slice_artifact(
            fixture.capsule,
            stop_eip=fixture.stop_eip,
            build_dir=build_dir / "artifacts-a",
        )
        peer_artifact = build_ia32_persistent_slice_artifact(
            peer_fixture.capsule,
            stop_eip=peer_fixture.stop_eip,
            build_dir=build_dir / "artifacts-b",
        )
        proofs.append(_evaluate_persistent_proof(fixture, artifact, peer_artifact))
    stable_identity = {
        "format": PHASE1_PROOF_SET_FORMAT,
        "version": PHASE1_PROOF_SET_VERSION,
        "persistent_worker_contract_id": ia32_persistent_worker_contract_id(),
        "proof_ids": [str(proof["proof_id"]) for proof in proofs],
    }
    return {
        **stable_identity,
        "proof_set_id": _sha256(_canonical_json(stable_identity)),
        "passed": bool(proofs) and all(bool(proof["passed"]) for proof in proofs),
        "persistent_worker_contract": ia32_persistent_worker_contract(),
        "proofs": proofs,
    }


def run_asset_free_phase2_proofs(build_dir: Path) -> dict[str, Any]:
    first = build_asset_free_phase2_fixture(build_dir / "fixture-a")
    second = build_asset_free_phase2_fixture(build_dir / "fixture-b")
    connection = sqlite3.connect(second.decoded_block_store_path)
    try:
        connection.execute("UPDATE decoded_blocks SET last_used_ns=123456789, access_count=17")
        connection.commit()
    finally:
        connection.close()
    first_artifact = build_ia32_decoded_store_artifact(
        first.capsule,
        xbe_path=first.xbe_path,
        decoded_block_store_path=first.decoded_block_store_path,
        stop_eip=first.stop_eip,
        build_dir=build_dir / "artifacts-a",
    )
    second_artifact = build_ia32_decoded_store_artifact(
        second.capsule,
        xbe_path=second.xbe_path,
        decoded_block_store_path=second.decoded_block_store_path,
        stop_eip=second.stop_eip,
        build_dir=build_dir / "artifacts-b",
    )
    validate_ia32_decoded_store_artifact_sources(
        first_artifact,
        xbe_path=first.xbe_path,
        decoded_block_store_path=first.decoded_block_store_path,
    )
    resident = _evaluate_persistent_proof(
        Phase0ProofFixture(
            "asset-free-decoded-store",
            "compute",
            first.capsule,
            first.stop_eip,
        ),
        first_artifact,
        second_artifact,
    )
    coverage = json.loads((first_artifact.root / "coverage-map.json").read_text(encoding="utf-8"))
    relocations = json.loads(
        (first_artifact.root / "direct-edge-relocations.json").read_text(encoding="utf-8")
    )
    rewrites = json.loads(
        (first_artifact.root / "rewrite-manifest.json").read_text(encoding="utf-8")
    )
    indirect_targets = json.loads(
        (first_artifact.root / "indirect-target-table.json").read_text(encoding="utf-8")
    )
    worker_source = (first_artifact.root / "runner.c").read_text(encoding="utf-8")
    runtime_stop_patch = f"copy_bytes((void*)0x{first.stop_eip:08X}u, kStopPatch" in worker_source
    inputs = first_artifact.manifest["inputs"]["decoded_store_artifact"]
    output_hashes = {
        name: record["sha256"]
        for name, record in sorted(first_artifact.manifest["phase2_outputs"].items())
    }
    phase2_checks = {
        "coverage_complete": coverage["complete"] is True,
        "decoded_block_count": coverage["block_count"] == 3,
        "decoded_instruction_count": coverage["instruction_count"] == 10,
        "known_direct_edges_preserved": bool(relocations["edges"])
        and all(edge["relocation"] == "preserved-fixed-va" for edge in relocations["edges"]),
        "indirect_target_set_verified": len(indirect_targets["sites"]) == 1
        and indirect_targets["sites"][0]["status"] == "verified-fixed-target-set"
        and len(indirect_targets["sites"][0]["targets"]) == 1,
        "unsupported_site_count": len(coverage["unsupported_sites"]) == 0,
        "rewrite_gap_count": len(rewrites["unsupported_sites"]) == 0,
        "normal_execution_eligible": (first_artifact.manifest["normal_execution_eligible"] is True),
        "runtime_decoding": first_artifact.manifest["runtime_decoding"],
        "runtime_compilation": first_artifact.manifest["runtime_compilation"],
        "runtime_code_patching": first_artifact.manifest["runtime_code_patching"],
        "runtime_stop_patch_present": runtime_stop_patch,
        "native_promotion": first_artifact.manifest["native_promotion"],
        "raw_xbe_execution": first_artifact.manifest["raw_xbe_execution"],
    }
    stable_identity = {
        "name": "asset-free-decoded-store-artifact",
        "contract_id": ia32_decoded_store_artifact_contract_id(),
        "xbe_sha256": inputs["xbe_sha256"],
        "store_snapshot_id": inputs["store_snapshot_id"],
        "artifact_id": first_artifact.artifact_id,
        "peer_artifact_id": second_artifact.artifact_id,
        "output_hashes": output_hashes,
        "resident_proof_id": resident["proof_id"],
        "phase2_checks": phase2_checks,
    }
    proof = {
        **stable_identity,
        "proof_id": _sha256(_canonical_json(stable_identity)),
        "passed": (
            resident["passed"]
            and first_artifact.artifact_id == second_artifact.artifact_id
            and all(
                value is False
                for key, value in phase2_checks.items()
                if key.startswith("runtime_") or key in {"native_promotion", "raw_xbe_execution"}
            )
            and all(
                bool(value)
                for key, value in phase2_checks.items()
                if not key.startswith("runtime_")
                and key not in {"native_promotion", "raw_xbe_execution"}
            )
        ),
        "artifact_reproducible_across_mutable_store_metadata": (
            first_artifact.artifact_id == second_artifact.artifact_id
        ),
        "resident_execution": resident,
        "artifact": {
            "path": str(first_artifact.root),
            "manifest": str(first_artifact.manifest_path),
            "phase2_outputs": first_artifact.manifest["phase2_outputs"],
        },
    }
    proof_set_identity = {
        "format": PHASE2_PROOF_SET_FORMAT,
        "version": PHASE2_PROOF_SET_VERSION,
        "decoded_store_artifact_contract_id": (ia32_decoded_store_artifact_contract_id()),
        "proof_ids": [proof["proof_id"]],
    }
    return {
        **proof_set_identity,
        "proof_set_id": _sha256(_canonical_json(proof_set_identity)),
        "passed": proof["passed"],
        "decoded_store_artifact_contract": ia32_decoded_store_artifact_contract(),
        "proofs": [proof],
    }


def _evaluate_phase3_differential(
    fixture: Phase3ProofFixture,
    artifact: Ia32SliceArtifact,
    peer_artifact: Ia32SliceArtifact,
) -> dict[str, Any]:
    reference = execute_ia32_slice_reference(
        fixture.capsule,
        stop_eip=fixture.stop_eip,
    )
    native = run_ia32_slice_artifact(artifact, fixture.capsule)
    accepted_state = normalize_ia32_phase3_state(reference.state)
    native_state = normalize_ia32_phase3_state(native.state)
    accepted = ReplayObservation(
        backend="decoded-reference",
        prefix_steps=reference.steps,
        steps_executed=reference.steps,
        exit_reason="phase3-fixed-slice-stop",
        state=accepted_state,
        memory=reference.memory,
        events=fixture.capsule.events,
    )
    experimental = ReplayObservation(
        backend="same-isa-ia32-phase3",
        prefix_steps=reference.steps,
        steps_executed=reference.steps,
        exit_reason="phase3-fixed-slice-stop",
        state=native_state,
        memory=native.memory,
        events=fixture.capsule.events,
    )
    architectural_match = accepted.architectural_hash() == experimental.architectural_hash()
    artifact_reproducible = (
        artifact.artifact_id == peer_artifact.artifact_id
        and sha256_file(artifact.executable) == sha256_file(peer_artifact.executable)
        and sha256_file(artifact.manifest_path) == sha256_file(peer_artifact.manifest_path)
    )
    dirty_owners = tuple(sorted({str(record["owner"]) for record in native.dirty_publications}))
    dirty_match = dirty_owners == fixture.expected_dirty_owners
    architecture_map = json.loads(
        (artifact.root / "architecture-map.json").read_text(encoding="utf-8")
    )
    checks = {
        "architectural_match": architectural_match,
        "artifact_reproducible": artifact_reproducible,
        "dirty_ownership_match": dirty_match,
        "cross_page_native_publication": (
            fixture.name != "tls-stack-alias-mmio-cross-page-dirty"
            or sum(1 for record in native.dirty_publications if record["owner"] == "audio") == 2
        ),
        "normal_runtime_python_callbacks_zero": (
            artifact.manifest["normal_runtime_python_callbacks"] == 0
        ),
        "runtime_decoding": artifact.manifest["runtime_decoding"],
        "runtime_compilation": artifact.manifest["runtime_compilation"],
        "runtime_code_patching": artifact.manifest["runtime_code_patching"],
        "native_promotion": artifact.manifest["native_promotion"],
        "raw_xbe_execution": artifact.manifest["raw_xbe_execution"],
    }
    stable_identity = {
        "name": fixture.name,
        "contract_id": ia32_architecture_contract_id(),
        "capsule_id": fixture.capsule.capsule_id,
        "artifact_id": artifact.artifact_id,
        "peer_artifact_id": peer_artifact.artifact_id,
        "architecture_map_sha256": sha256_file(artifact.root / "architecture-map.json"),
        "accepted_architectural_hash": accepted.architectural_hash(),
        "native_architectural_hash": experimental.architectural_hash(),
        "dirty_publications": list(native.dirty_publications),
        "checks": checks,
    }
    proof: dict[str, Any] = {
        **stable_identity,
        "proof_id": _sha256(_canonical_json(stable_identity)),
        "passed": (
            all(
                bool(value)
                for key, value in checks.items()
                if key
                not in {
                    "runtime_decoding",
                    "runtime_compilation",
                    "runtime_code_patching",
                    "native_promotion",
                    "raw_xbe_execution",
                }
            )
            and all(
                value is False
                for key, value in checks.items()
                if key
                in {
                    "runtime_decoding",
                    "runtime_compilation",
                    "runtime_code_patching",
                    "native_promotion",
                    "raw_xbe_execution",
                }
            )
        ),
        "architecture_map": architecture_map,
        "reference": {
            "state_id": _state_identity(accepted_state),
            "memory_id": _memory_identity(reference.memory),
        },
        "native": {
            "state_id": _state_identity(native_state),
            "memory_id": _memory_identity(native.memory),
            "resident_dispatch_ns": native.resident_dispatch_ns,
        },
    }
    if not architectural_match:
        proof["divergence"] = diagnose_observation_divergence(
            accepted,
            experimental,
            compact=True,
        )
    return proof


def _evaluate_phase3_fault(
    fixture: Phase3ProofFixture,
    artifact: Ia32SliceArtifact,
    peer_artifact: Ia32SliceArtifact,
) -> dict[str, Any]:
    faults: list[dict[str, Any]] = []
    with Ia32PersistentWorker.start(artifact, fixture.capsule) as worker:
        process_id = worker.process_id
        for _index in range(2):
            try:
                worker.dispatch(fixture.capsule)
            except Ia32GuestFault as exc:
                faults.append(
                    {
                        "code": exc.code,
                        "guest_eip": exc.guest_eip,
                        "access_address": exc.access_address,
                        "access_kind": exc.access_kind,
                        "worker_process_id": worker.process_id,
                    }
                )
            else:
                raise Ia32BackendError("Phase-3 fault capsule completed without its fault")
    expected = {
        "code": 0xC0000005,
        "guest_eip": fixture.capsule.state.eip,
        "access_address": 0x000D0000,
        "access_kind": "read",
        "worker_process_id": process_id,
    }
    artifact_reproducible = (
        artifact.artifact_id == peer_artifact.artifact_id
        and sha256_file(artifact.executable) == sha256_file(peer_artifact.executable)
        and sha256_file(artifact.manifest_path) == sha256_file(peer_artifact.manifest_path)
    )
    checks = {
        "fault_identity_match": faults == [expected, expected],
        "worker_recovered_for_second_command": len(faults) == 2
        and all(record["worker_process_id"] == process_id for record in faults),
        "artifact_reproducible": artifact_reproducible,
        "normal_runtime_python_callbacks_zero": (
            artifact.manifest["normal_runtime_python_callbacks"] == 0
        ),
    }
    normalized_faults = [
        {key: value for key, value in record.items() if key != "worker_process_id"}
        for record in faults
    ]
    stable_identity = {
        "name": fixture.name,
        "contract_id": ia32_architecture_contract_id(),
        "capsule_id": fixture.capsule.capsule_id,
        "artifact_id": artifact.artifact_id,
        "peer_artifact_id": peer_artifact.artifact_id,
        "faults": normalized_faults,
        "same_resident_worker": checks["worker_recovered_for_second_command"],
        "checks": checks,
    }
    return {
        **stable_identity,
        "proof_id": _sha256(_canonical_json(stable_identity)),
        "passed": all(checks.values()),
        "observed_worker_process_id": process_id,
    }


def run_asset_free_phase3_proofs(build_dir: Path) -> dict[str, Any]:
    first = build_asset_free_phase3_fixtures(build_dir / "capsules-a")
    second = build_asset_free_phase3_fixtures(build_dir / "capsules-b")
    peers = {fixture.name: fixture for fixture in second}
    proofs = []
    for fixture in first:
        peer = peers[fixture.name]
        if fixture.capsule.capsule_id != peer.capsule.capsule_id:
            raise Ia32BackendError(f"{fixture.name} Phase-3 capsule rebuild changed its identity")
        if fixture.xbe_path is not None and fixture.decoded_block_store_path is not None:
            if peer.xbe_path is None or peer.decoded_block_store_path is None:
                raise Ia32BackendError("Phase-3 decoded-store proof peer omits its sources")
            artifact = build_ia32_decoded_store_architecture_artifact(
                fixture.capsule,
                xbe_path=fixture.xbe_path,
                decoded_block_store_path=fixture.decoded_block_store_path,
                stop_eip=fixture.stop_eip,
                build_dir=build_dir / "artifacts-a",
                profile=fixture.profile,
            )
            peer_artifact = build_ia32_decoded_store_architecture_artifact(
                peer.capsule,
                xbe_path=peer.xbe_path,
                decoded_block_store_path=peer.decoded_block_store_path,
                stop_eip=peer.stop_eip,
                build_dir=build_dir / "artifacts-b",
                profile=peer.profile,
            )
        else:
            artifact = build_ia32_architecture_artifact(
                fixture.capsule,
                stop_eip=fixture.stop_eip,
                build_dir=build_dir / "artifacts-a",
                profile=fixture.profile,
            )
            peer_artifact = build_ia32_architecture_artifact(
                peer.capsule,
                stop_eip=peer.stop_eip,
                build_dir=build_dir / "artifacts-b",
                profile=peer.profile,
            )
        proofs.append(
            _evaluate_phase3_fault(fixture, artifact, peer_artifact)
            if fixture.recoverable_fault
            else _evaluate_phase3_differential(fixture, artifact, peer_artifact)
        )
    stable_identity = {
        "format": PHASE3_PROOF_SET_FORMAT,
        "version": PHASE3_PROOF_SET_VERSION,
        "architecture_contract_id": ia32_architecture_contract_id(),
        "proof_ids": [str(proof["proof_id"]) for proof in proofs],
    }
    proof_set_id = _sha256(_canonical_json(stable_identity))
    if PHASE3_PROOF_SET_ID and proof_set_id != PHASE3_PROOF_SET_ID:
        raise Ia32BackendError("the frozen Phase-3 IA-32 proof set changed without a version bump")
    return {
        **stable_identity,
        "proof_set_id": proof_set_id,
        "passed": bool(proofs) and all(bool(proof["passed"]) for proof in proofs),
        "architecture_contract": ia32_architecture_contract(),
        "proofs": proofs,
    }


def build_asset_free_phase4_fixture(root: Path) -> Phase4ProofFixture:
    root.mkdir(parents=True, exist_ok=True)
    main_code = bytes.fromhex(
        "6800100D00"
        "FF1500000D00"
        "A310100D00"
        "6804100D00"
        "6800500C00"
        "FF1504000D00"
        "A314100D00"
        "6877000000"
        "6808100D00"
        "FF1508000D00"
        "83C408"
        "A318100D00"
        "FF150C000D00"
        "A31C100D00"
        "C3"
    )
    callback_code = bytes.fromhex("8B4424048B4C24080108B8EEFFC000C20800")
    state = CpuState.with_registers(
        eax=0x11111111,
        ecx=0x22222222,
        edx=0x33333333,
        ebx=0x44444444,
        esp=0x000E0800,
        ebp=0x55555555,
        esi=0x66666666,
        edi=0x77777777,
    )
    state.eip = HOST_ABI_ENTRY_EIP
    state.fpu_control_word = 0x033F
    memory = SparseMemory(
        {
            0x000D0000: HOST_ABI_SERVICE_TARGETS[0],
            0x000D0004: HOST_ABI_SERVICE_TARGETS[1],
            0x000D0008: HOST_ABI_SERVICE_TARGETS[2],
            0x000D000C: HOST_ABI_SERVICE_TARGETS[3],
            0x000D1000: 0x01020304,
            0x000D1004: 10,
            0x000D1008: 0x11223344,
            0x000D1010: 0,
            0x000D1014: 0,
            0x000D1018: 0,
            0x000D101C: 0,
            0x000E0000: 0,
        }
    )
    services = [
        {
            "target": HOST_ABI_SERVICE_TARGETS[0],
            "shim_name": "B2RMemoryWrite",
            "kind": NATIVE_HOST_SERVICE_WRITE_U32,
            "value": 0,
            "calling_convention": "stdcall",
            "argument_count": 1,
            "stack_cleanup_bytes": 4,
            "boundary": "memory",
            "execution": "native32",
            "memory_write_argument": 0,
            "memory_write_value": 0xAABBCCDD,
        },
        {
            "target": HOST_ABI_SERVICE_TARGETS[1],
            "shim_name": "B2RInvokeCallback",
            "kind": NATIVE_HOST_SERVICE_CALLBACK,
            "value": 0x12345678,
            "calling_convention": "stdcall",
            "argument_count": 2,
            "stack_cleanup_bytes": 8,
            "boundary": "title",
            "execution": "native32",
            "callback_argument": 0,
            "callback_context_argument": 1,
            "callback_value": 5,
            "callback_stack_cleanup_bytes": 8,
        },
        {
            "target": HOST_ABI_SERVICE_TARGETS[2],
            "shim_name": "B2RRenderPublish",
            "kind": NATIVE_HOST_SERVICE_WRITE_U32,
            "value": 0x80000005,
            "calling_convention": "cdecl",
            "argument_count": 2,
            "stack_cleanup_bytes": 0,
            "boundary": "render",
            "execution": "native64-broker",
            "memory_write_argument": 0,
            "memory_write_value": 0x55667788,
        },
        {
            "target": HOST_ABI_SERVICE_TARGETS[3],
            "shim_name": "B2RInputPoll",
            "kind": NATIVE_HOST_SERVICE_RETURN_CONSTANT,
            "value": 42,
            "calling_convention": "stdcall",
            "argument_count": 0,
            "stack_cleanup_bytes": 0,
            "boundary": "input",
            "execution": "native32",
        },
    ]
    capsule_path = capture_replay_capsule(
        root / "host-abi.b2rcap",
        state=state,
        memory=memory,
        functions=(
            lift_x86_function(
                main_code,
                base_address=HOST_ABI_ENTRY_EIP,
                symbol="phase4_service_rich_main",
            ),
            lift_x86_function(
                callback_code,
                base_address=HOST_ABI_CALLBACK_EIP,
                symbol="phase4_guest_callback",
            ),
        ),
        scheduler_state={"lane": "primary", "slice": 0},
        service_state={"service_registry": services},
        provenance={
            "synthetic": True,
            "manual_capture": False,
            "source": "ia32-phase4-contract",
            "proof": "service-rich-native-host-abi",
        },
        capture_kind="synthetic",
    )
    return Phase4ProofFixture(
        capsule=load_replay_capsule(capsule_path),
        stop_eip=HOST_ABI_STOP_EIP,
        expected_service_order=tuple(str(service["shim_name"]) for service in services),
    )


def _evaluate_phase4_proof(
    fixture: Phase4ProofFixture,
    artifact: Ia32SliceArtifact,
    peer_artifact: Ia32SliceArtifact,
) -> dict[str, Any]:
    reference = execute_ia32_slice_reference(fixture.capsule, stop_eip=fixture.stop_eip)
    with Ia32PersistentWorker.start(artifact, fixture.capsule) as worker:
        worker_process_id = worker.process_id
        broker_process_id = worker.broker_process_id
        native = worker.dispatch(fixture.capsule)
    accepted_state = normalize_ia32_phase3_state(reference.state)
    native_state = normalize_ia32_phase3_state(native.state)
    accepted = ReplayObservation(
        backend="decoded-reference",
        prefix_steps=reference.steps,
        steps_executed=reference.steps,
        exit_reason="phase4-fixed-slice-stop",
        state=accepted_state,
        memory=reference.memory,
        events=fixture.capsule.events,
    )
    experimental = ReplayObservation(
        backend="same-isa-ia32-phase4",
        prefix_steps=reference.steps,
        steps_executed=reference.steps,
        exit_reason="phase4-fixed-slice-stop",
        state=native_state,
        memory=native.memory,
        events=fixture.capsule.events,
    )
    service_order = tuple(call["shim_name"] for call in native.host_service_calls)
    broker = artifact.manifest["service_broker"]
    peer_broker = peer_artifact.manifest["service_broker"]
    artifact_reproducible = (
        artifact.artifact_id == peer_artifact.artifact_id
        and sha256_file(artifact.executable) == sha256_file(peer_artifact.executable)
        and broker["sha256"] == peer_broker["sha256"]
    )
    architectural_state_match = _state_identity(accepted_state) == _state_identity(native_state)
    service_memory_match = reference.memory.read(0x000D0000, 0x2000) == native.memory.read(
        0x000D0000, 0x2000
    )
    checks = {
        "architectural_match": architectural_state_match and service_memory_match,
        "architectural_state_match": architectural_state_match,
        "service_memory_effects_match": service_memory_match,
        "service_trace_match": list(reference.host_service_calls)
        == list(native.host_service_calls),
        "service_order_match": service_order == fixture.expected_service_order,
        "stack_restored": native.state.get_register("esp")
        == fixture.capsule.state.get_register("esp"),
        "callback_reentered_once": native.host_service_calls[1]["callback_count"] == 1
        and native.host_service_calls[1]["callback_target"] == HOST_ABI_CALLBACK_EIP,
        "native_broker_is_separate_process": broker_process_id is not None
        and broker_process_id != worker_process_id,
        "every_service_registered": artifact.manifest["registered_service_count"]
        == len(fixture.expected_service_order),
        "artifact_reproducible": artifact_reproducible,
        "normal_runtime_python_callbacks_zero": artifact.manifest["normal_runtime_python_callbacks"]
        == 0,
        "runtime_decoding": artifact.manifest["runtime_decoding"],
        "runtime_compilation": artifact.manifest["runtime_compilation"],
        "runtime_code_patching": artifact.manifest["runtime_code_patching"],
        "native_promotion": artifact.manifest["native_promotion"],
        "raw_xbe_execution": artifact.manifest["raw_xbe_execution"],
    }
    negative_checks = {
        "runtime_decoding",
        "runtime_compilation",
        "runtime_code_patching",
        "native_promotion",
        "raw_xbe_execution",
    }
    stable_identity = {
        "name": "asset-free-service-rich-host-abi",
        "contract_id": ia32_host_abi_contract_id(),
        "capsule_id": fixture.capsule.capsule_id,
        "artifact_id": artifact.artifact_id,
        "peer_artifact_id": peer_artifact.artifact_id,
        "service_broker_sha256": broker["sha256"],
        "service_calls": list(native.host_service_calls),
        "accepted_state_id": _state_identity(accepted_state),
        "native_state_id": _state_identity(native_state),
        "service_memory_sha256": _sha256(reference.memory.read(0x000D0000, 0x2000)),
        "checks": checks,
    }
    proof: dict[str, Any] = {
        **stable_identity,
        "proof_id": _sha256(_canonical_json(stable_identity)),
        "passed": all(
            value is False if key in negative_checks else bool(value)
            for key, value in checks.items()
        ),
        "worker_process_id": worker_process_id,
        "broker_process_id": broker_process_id,
        "reference_service_calls": list(reference.host_service_calls),
    }
    if not checks["architectural_match"]:
        proof["divergence"] = diagnose_observation_divergence(
            accepted,
            experimental,
            compact=True,
        )
    return proof


def run_asset_free_phase4_proofs(build_dir: Path) -> dict[str, Any]:
    first = build_asset_free_phase4_fixture(build_dir / "capsules-a")
    second = build_asset_free_phase4_fixture(build_dir / "capsules-b")
    if first.capsule.capsule_id != second.capsule.capsule_id:
        raise Ia32BackendError("Phase-4 capsule rebuild changed its identity")
    artifact = build_ia32_host_abi_artifact(
        first.capsule,
        stop_eip=first.stop_eip,
        build_dir=build_dir / "artifacts-a",
    )
    peer_artifact = build_ia32_host_abi_artifact(
        second.capsule,
        stop_eip=second.stop_eip,
        build_dir=build_dir / "artifacts-b",
    )
    proof = _evaluate_phase4_proof(first, artifact, peer_artifact)
    identity = {
        "format": PHASE4_PROOF_SET_FORMAT,
        "version": PHASE4_PROOF_SET_VERSION,
        "host_abi_contract_id": ia32_host_abi_contract_id(),
        "proof_ids": [proof["proof_id"]],
    }
    report = {
        **identity,
        "proof_set_id": _sha256(_canonical_json(identity)),
        "passed": proof["passed"],
        "host_abi_contract": ia32_host_abi_contract(),
        "proofs": [proof],
    }
    if PHASE4_PROOF_SET_ID and report["proof_set_id"] != PHASE4_PROOF_SET_ID:
        raise Ia32BackendError(
            "the frozen Phase-4 proof set changed without a proof-contract version bump"
        )
    return report


def _phase5_rel32(source_address: int, target_address: int) -> bytes:
    return ((target_address - (source_address + 5)) & 0xFFFFFFFF).to_bytes(4, "little")


def _phase5_step_code(
    entry_eip: int,
    *,
    output_address: int,
    call_service: bool,
) -> bytes:
    payload = bytearray(b"\x43\x89\x1d")
    payload.extend(output_address.to_bytes(4, "little"))
    call_address = entry_eip + len(payload)
    payload.append(0xE8)
    payload.extend(_phase5_rel32(call_address, SCHEDULER_TLS_HELPER_EIP))
    if call_service:
        payload.extend(b"\xff\x15")
        payload.extend(SCHEDULER_SERVICE_POINTER.to_bytes(4, "little"))
    jump_address = entry_eip + len(payload)
    payload.append(0xE9)
    payload.extend(_phase5_rel32(jump_address, SCHEDULER_STOP_EIP))
    return bytes(payload)


def _phase5_fault_code(entry_eip: int) -> bytes:
    payload = bytearray(b"\xa1\x00\x00\x00\x01")
    jump_address = entry_eip + len(payload)
    payload.append(0xE9)
    payload.extend(_phase5_rel32(jump_address, SCHEDULER_STOP_EIP))
    return bytes(payload)


def build_asset_free_phase5_fixture(
    root: Path,
    *,
    contained_fault: bool = False,
) -> Phase5ProofFixture:
    root.mkdir(parents=True, exist_ok=True)
    output_addresses = (
        SCHEDULER_RENDER_ADDRESS,
        SCHEDULER_RENDER_ADDRESS + 4,
        SCHEDULER_AUDIO_ADDRESS,
        SCHEDULER_RENDER_ADDRESS + 8,
        SCHEDULER_RENDER_ADDRESS + 12,
        SCHEDULER_AUDIO_ADDRESS + 4,
        SCHEDULER_AUDIO_ADDRESS + 8,
    )
    call_service = (True, False, True, True, False, True, True)
    lane_registers = (
        {
            "eax": 0,
            "ecx": 0x11111111,
            "edx": 0x12121212,
            "ebx": 10,
            "esp": 0x000E0800,
            "ebp": 0x13131313,
            "esi": 0x14141414,
            "edi": 0x15151515,
        },
        {
            "eax": 0,
            "ecx": 0x21212121,
            "edx": 0x22222222,
            "ebx": 20,
            "esp": 0x000E1800,
            "ebp": 0x23232323,
            "esi": 0x24242424,
            "edi": 0x25252525,
        },
        {
            "eax": 0,
            "ecx": 0x31313131,
            "edx": 0x32323232,
            "ebx": 30,
            "esp": 0x000E2800,
            "ebp": 0x33333333,
            "esi": 0x34343434,
            "edi": 0x35353535,
        },
    )
    lane_entries = (
        (SCHEDULER_STEP_EIPS[0], SCHEDULER_STEP_EIPS[3], SCHEDULER_STEP_EIPS[6]),
        (SCHEDULER_STEP_EIPS[2], SCHEDULER_STEP_EIPS[5]),
        (SCHEDULER_STEP_EIPS[1], SCHEDULER_STEP_EIPS[4]),
    )
    scheduler_steps = [
        {
            "sequence": 0,
            "lane": "primary",
            "entry_eip": lane_entries[0][0],
            "next_eip": lane_entries[0][1],
            "exit": "yield",
            "wake_lane": "worker",
        },
        {
            "sequence": 1,
            "lane": "vblank",
            "entry_eip": lane_entries[2][0],
            "next_eip": lane_entries[2][1],
            "exit": "flip",
        },
        {
            "sequence": 2,
            "lane": "worker",
            "entry_eip": lane_entries[1][0],
            "next_eip": lane_entries[1][1],
            "exit": "wait",
            "wait_object": 0x9002,
        },
        {
            "sequence": 3,
            "lane": "primary",
            "entry_eip": lane_entries[0][1],
            "next_eip": lane_entries[0][2],
            "exit": "yield",
            "wake_lane": "worker",
        },
        {
            "sequence": 4,
            "lane": "vblank",
            "entry_eip": lane_entries[2][1],
            "next_eip": SCHEDULER_STOP_EIP,
            "exit": "complete",
        },
        {
            "sequence": 5,
            "lane": "worker",
            "entry_eip": lane_entries[1][1],
            "next_eip": SCHEDULER_STOP_EIP,
            "exit": "complete",
        },
        {
            "sequence": 6,
            "lane": "primary",
            "entry_eip": lane_entries[0][2],
            "next_eip": SCHEDULER_STOP_EIP,
            "exit": "complete",
        },
    ]
    state = CpuState.with_registers(**lane_registers[0], fs_base=SCHEDULER_ACTIVE_TLS)
    state.eip = lane_entries[0][0]
    memory = SparseMemory(
        {
            SCHEDULER_SERVICE_POINTER: SCHEDULER_SERVICE_TARGET,
            SCHEDULER_RENDER_ADDRESS: bytes(16),
            SCHEDULER_AUDIO_ADDRESS: bytes(16),
            0x000E0000: bytes(4096),
            0x000E1000: bytes(4096),
            0x000E2000: bytes(4096),
            SCHEDULER_ACTIVE_TLS: bytes(4096),
            SCHEDULER_LANE_TLS[0]: (100).to_bytes(4, "little") + bytes(4092),
            SCHEDULER_LANE_TLS[1]: (200).to_bytes(4, "little") + bytes(4092),
            SCHEDULER_LANE_TLS[2]: (300).to_bytes(4, "little") + bytes(4092),
        }
    )
    functions = tuple(
        lift_x86_function(
            (
                _phase5_fault_code(entry)
                if contained_fault and index == 4
                else _phase5_step_code(
                    entry,
                    output_address=output,
                    call_service=service,
                )
            ),
            base_address=entry,
            symbol=f"phase5_scheduler_step_{index}",
        )
        for index, (entry, output, service) in enumerate(
            zip(SCHEDULER_STEP_EIPS, output_addresses, call_service)
        )
    ) + (
        lift_x86_function(
            bytes.fromhex("C3"),
            base_address=SCHEDULER_STOP_EIP,
            symbol="phase5_scheduler_stop",
        ),
        lift_x86_function(
            bytes.fromhex("64A1000000004064A300000000C3"),
            base_address=SCHEDULER_TLS_HELPER_EIP,
            symbol="phase5_lane_tls_increment",
        ),
    )
    capsule_path = capture_replay_capsule(
        root
        / ("resident-scheduler-fault.b2rcap" if contained_fault else "resident-scheduler.b2rcap"),
        state=state,
        memory=memory,
        functions=functions,
        scheduler_state={
            "resident_scheduler": {
                "version": 1,
                "active_tls_base": SCHEDULER_ACTIVE_TLS,
                "lanes": [
                    {
                        "name": "primary",
                        "initial_eip": lane_entries[0][0],
                        "tls_base": SCHEDULER_LANE_TLS[0],
                        "initial_state": "ready",
                        "registers": lane_registers[0],
                    },
                    {
                        "name": "worker",
                        "initial_eip": lane_entries[1][0],
                        "tls_base": SCHEDULER_LANE_TLS[1],
                        "initial_state": "waiting",
                        "wait_object": 0x9001,
                        "registers": lane_registers[1],
                    },
                    {
                        "name": "vblank",
                        "initial_eip": lane_entries[2][0],
                        "tls_base": SCHEDULER_LANE_TLS[2],
                        "initial_state": "ready",
                        "registers": lane_registers[2],
                    },
                ],
                "steps": scheduler_steps,
                "products": {
                    "render": {"address": SCHEDULER_RENDER_ADDRESS, "size": 16},
                    "audio": {"address": SCHEDULER_AUDIO_ADDRESS, "size": 16},
                },
            }
        },
        service_state={
            "service_registry": [
                {
                    "target": SCHEDULER_SERVICE_TARGET,
                    "shim_name": "B2RSchedulerSafePoint",
                    "kind": NATIVE_HOST_SERVICE_RETURN_CONSTANT,
                    "value": 0x51515151,
                    "calling_convention": "stdcall",
                    "argument_count": 0,
                    "stack_cleanup_bytes": 0,
                    "boundary": "synchronization",
                    "execution": "native32",
                }
            ]
        },
        provenance={
            "synthetic": True,
            "manual_capture": False,
            "source": "ia32-phase5-contract",
            "proof": (
                "resident-scheduler-fault-containment"
                if contained_fault
                else "resident-primary-worker-vblank-scheduler"
            ),
        },
        capture_kind="synthetic",
    )
    expected_lane_order = tuple(str(step["lane"]) for step in scheduler_steps)
    expected_service_order = tuple("B2RSchedulerSafePoint" for enabled in call_service if enabled)
    return Phase5ProofFixture(
        capsule=load_replay_capsule(capsule_path),
        stop_eip=SCHEDULER_STOP_EIP,
        expected_lane_order=expected_lane_order,
        expected_service_order=expected_service_order,
        contained_fault=contained_fault,
    )


def _evaluate_phase5_proof(
    fixture: Phase5ProofFixture,
    artifact: Ia32SliceArtifact,
    peer_artifact: Ia32SliceArtifact,
) -> dict[str, Any]:
    reference = execute_ia32_scheduler_reference(
        fixture.capsule,
        stop_eip=fixture.stop_eip,
    )
    with Ia32PersistentWorker.start(artifact, fixture.capsule) as worker:
        worker_process_id = worker.process_id
        broker_process_id = worker.broker_process_id
        native = worker.dispatch(
            fixture.capsule,
            command=WORKER_COMMAND_SCHEDULE,
        )
    scheduler = native.resident_scheduler
    if scheduler is None:
        raise Ia32BackendError("Phase-5 worker omitted its resident scheduler report")
    artifact_broker = artifact.manifest["service_broker"]
    peer_broker = peer_artifact.manifest["service_broker"]
    expected_service_order = tuple(call["shim_name"] for call in reference.host_service_calls)
    native_service_order = tuple(call["shim_name"] for call in native.host_service_calls)
    selected_ranges = (
        (SCHEDULER_SERVICE_POINTER, 4),
        (SCHEDULER_RENDER_ADDRESS, 16),
        (SCHEDULER_AUDIO_ADDRESS, 16),
        (SCHEDULER_ACTIVE_TLS, 4096),
        *((address, 4096) for address in SCHEDULER_LANE_TLS),
    )
    reference_memory_id = _sha256(
        b"".join(reference.memory.read(address, size) for address, size in selected_ranges)
    )
    native_memory_id = _sha256(
        b"".join(native.memory.read(address, size) for address, size in selected_ranges)
    )
    reference_primary = cpu_state_record(reference.state)
    reference_primary["fs_base"] = fixture.capsule.state.fs_base
    reference_primary_state = cpu_state_from_record(reference_primary)
    checks = {
        "scheduler_trace_match": list(reference.scheduler_trace) == scheduler["trace"],
        "scheduler_order_match": tuple(record["lane"] for record in scheduler["trace"])
        == fixture.expected_lane_order,
        "service_trace_match": list(reference.host_service_calls)
        == list(native.host_service_calls),
        "service_order_match": native_service_order
        == expected_service_order
        == fixture.expected_service_order,
        "lane_contexts_match": list(reference.lane_states) == scheduler["lanes"],
        "shared_memory_match": reference_memory_id == native_memory_id,
        "primary_state_match": _state_identity(normalize_ia32_phase3_state(reference_primary_state))
        == _state_identity(normalize_ia32_phase3_state(native.state)),
        "worker_wakeups_match": scheduler["worker_wakeups"] == reference.worker_wakeups == 2,
        "completed_flips_match": scheduler["completed_flips"] == reference.completed_flips == 1,
        "render_hash_match": scheduler["render_hash"] == reference.render_hash,
        "audio_hash_match": scheduler["audio_hash"] == reference.audio_hash,
        "per_lane_tls_persisted": [
            native.memory.read_u32(address) for address in SCHEDULER_LANE_TLS
        ]
        == [103, 202, 302],
        "no_stranded_context": scheduler["stranded_contexts"] == 0,
        "no_unclassified_exit": scheduler["unclassified_exits"] == 0,
        "no_unexpected_fault": scheduler["contained_faults"] == 0,
        "resident_processes_native": broker_process_id is not None
        and broker_process_id != worker_process_id,
        "artifact_reproducible": artifact.artifact_id == peer_artifact.artifact_id
        and sha256_file(artifact.executable) == sha256_file(peer_artifact.executable)
        and artifact_broker["sha256"] == peer_broker["sha256"],
        "normal_runtime_python_callbacks_zero": artifact.manifest["normal_runtime_python_callbacks"]
        == 0,
        "runtime_decoding": artifact.manifest["runtime_decoding"],
        "runtime_compilation": artifact.manifest["runtime_compilation"],
        "runtime_code_patching": artifact.manifest["runtime_code_patching"],
        "native_promotion": artifact.manifest["native_promotion"],
        "raw_xbe_execution": artifact.manifest["raw_xbe_execution"],
    }
    negative_checks = {
        "runtime_decoding",
        "runtime_compilation",
        "runtime_code_patching",
        "native_promotion",
        "raw_xbe_execution",
    }
    stable_identity = {
        "name": "asset-free-resident-primary-worker-vblank-scheduler",
        "contract_id": ia32_resident_scheduler_contract_id(),
        "capsule_id": fixture.capsule.capsule_id,
        "artifact_id": artifact.artifact_id,
        "peer_artifact_id": peer_artifact.artifact_id,
        "service_broker_sha256": artifact_broker["sha256"],
        "scheduler_trace": scheduler["trace"],
        "service_calls": list(native.host_service_calls),
        "lane_contexts": scheduler["lanes"],
        "render_hash": scheduler["render_hash"],
        "audio_hash": scheduler["audio_hash"],
        "checks": checks,
    }
    return {
        **stable_identity,
        "proof_id": _sha256(_canonical_json(stable_identity)),
        "passed": all(
            value is False if key in negative_checks else bool(value)
            for key, value in checks.items()
        ),
        "worker_process_id": worker_process_id,
        "broker_process_id": broker_process_id,
    }


def _evaluate_phase5_fault_proof(
    fixture: Phase5ProofFixture,
    artifact: Ia32SliceArtifact,
    peer_artifact: Ia32SliceArtifact,
) -> dict[str, Any]:
    with Ia32PersistentWorker.start(artifact, fixture.capsule) as worker:
        worker_process_id = worker.process_id
        native = worker.dispatch(
            fixture.capsule,
            command=WORKER_COMMAND_SCHEDULE,
        )
    scheduler = native.resident_scheduler
    if scheduler is None:
        raise Ia32BackendError("Phase-5 fault proof omitted its scheduler report")
    vblank = scheduler["lanes"][2]
    fault_trace = scheduler["trace"][4]
    checks = {
        "fault_contained_in_vblank": scheduler["contained_faults"] == 1
        and vblank["state"] == "faulted"
        and vblank["fault_code"] == 0xC0000005
        and vblank["fault_address"] == 0x01000000
        and fault_trace["lane"] == "vblank"
        and fault_trace["exit"] == "fault",
        "other_lanes_completed": scheduler["lanes"][0]["state"] == "completed"
        and scheduler["lanes"][1]["state"] == "completed",
        "schedule_continued_after_fault": tuple(record["lane"] for record in scheduler["trace"][5:])
        == ("worker", "primary"),
        "no_stranded_context": scheduler["stranded_contexts"] == 0,
        "no_unclassified_exit": scheduler["unclassified_exits"] == 0,
        "artifact_reproducible": artifact.artifact_id == peer_artifact.artifact_id
        and sha256_file(artifact.executable) == sha256_file(peer_artifact.executable),
        "normal_runtime_python_callbacks_zero": artifact.manifest["normal_runtime_python_callbacks"]
        == 0,
        "runtime_decoding": artifact.manifest["runtime_decoding"],
        "runtime_compilation": artifact.manifest["runtime_compilation"],
        "runtime_code_patching": artifact.manifest["runtime_code_patching"],
        "native_promotion": artifact.manifest["native_promotion"],
        "raw_xbe_execution": artifact.manifest["raw_xbe_execution"],
    }
    negative_checks = {
        "runtime_decoding",
        "runtime_compilation",
        "runtime_code_patching",
        "native_promotion",
        "raw_xbe_execution",
    }
    stable_identity = {
        "name": "asset-free-resident-scheduler-fault-containment",
        "contract_id": ia32_resident_scheduler_contract_id(),
        "capsule_id": fixture.capsule.capsule_id,
        "artifact_id": artifact.artifact_id,
        "peer_artifact_id": peer_artifact.artifact_id,
        "scheduler_trace": scheduler["trace"],
        "lane_contexts": scheduler["lanes"],
        "checks": checks,
    }
    return {
        **stable_identity,
        "proof_id": _sha256(_canonical_json(stable_identity)),
        "passed": all(
            value is False if key in negative_checks else bool(value)
            for key, value in checks.items()
        ),
        "worker_process_id": worker_process_id,
    }


def run_asset_free_phase5_proofs(build_dir: Path) -> dict[str, Any]:
    first = build_asset_free_phase5_fixture(build_dir / "capsules-a")
    second = build_asset_free_phase5_fixture(build_dir / "capsules-b")
    if first.capsule.capsule_id != second.capsule.capsule_id:
        raise Ia32BackendError("Phase-5 capsule rebuild changed its identity")
    artifact = build_ia32_resident_scheduler_artifact(
        first.capsule,
        stop_eip=first.stop_eip,
        build_dir=build_dir / "artifacts-a",
    )
    peer_artifact = build_ia32_resident_scheduler_artifact(
        second.capsule,
        stop_eip=second.stop_eip,
        build_dir=build_dir / "artifacts-b",
    )
    fault_first = build_asset_free_phase5_fixture(
        build_dir / "fault-capsules-a",
        contained_fault=True,
    )
    fault_second = build_asset_free_phase5_fixture(
        build_dir / "fault-capsules-b",
        contained_fault=True,
    )
    if fault_first.capsule.capsule_id != fault_second.capsule.capsule_id:
        raise Ia32BackendError("Phase-5 fault capsule rebuild changed its identity")
    fault_artifact = build_ia32_resident_scheduler_artifact(
        fault_first.capsule,
        stop_eip=fault_first.stop_eip,
        build_dir=build_dir / "fault-artifacts-a",
    )
    fault_peer_artifact = build_ia32_resident_scheduler_artifact(
        fault_second.capsule,
        stop_eip=fault_second.stop_eip,
        build_dir=build_dir / "fault-artifacts-b",
    )
    proofs = [
        _evaluate_phase5_proof(first, artifact, peer_artifact),
        _evaluate_phase5_fault_proof(
            fault_first,
            fault_artifact,
            fault_peer_artifact,
        ),
    ]
    identity = {
        "format": PHASE5_PROOF_SET_FORMAT,
        "version": PHASE5_PROOF_SET_VERSION,
        "resident_scheduler_contract_id": ia32_resident_scheduler_contract_id(),
        "proof_ids": [proof["proof_id"] for proof in proofs],
    }
    report = {
        **identity,
        "proof_set_id": _sha256(_canonical_json(identity)),
        "passed": all(proof["passed"] for proof in proofs),
        "resident_scheduler_contract": ia32_resident_scheduler_contract(),
        "proofs": proofs,
    }
    if PHASE5_PROOF_SET_ID and report["proof_set_id"] != PHASE5_PROOF_SET_ID:
        raise Ia32BackendError(
            "the frozen Phase-5 proof set changed without a proof-contract version bump"
        )
    return report


def _phase6_profile(*, mode: str = "normal-validation") -> Ia32CoverageProfile:
    target_slices = {
        SCHEDULER_STEP_EIPS[3]: ("lesson-one-hot-scc", "primary", "lesson-one-update"),
        SCHEDULER_STEP_EIPS[6]: ("lesson-one-hot-scc", "primary", "lesson-one-flip"),
        SCHEDULER_TLS_HELPER_EIP: ("lesson-one-hot-scc", "shared", "tls-service-exit"),
        SCHEDULER_STEP_EIPS[0]: (
            "boot-frontend-continuity",
            "primary",
            "boot-to-lesson-one",
        ),
        SCHEDULER_STEP_EIPS[1]: ("worker-vblank-cold", "vblank", "vblank-entry"),
        SCHEDULER_STEP_EIPS[2]: ("worker-vblank-cold", "worker", "worker-entry"),
        SCHEDULER_STEP_EIPS[4]: ("worker-vblank-cold", "vblank", "vblank-cold"),
        SCHEDULER_STEP_EIPS[5]: ("worker-vblank-cold", "worker", "worker-cold"),
        SCHEDULER_STOP_EIP: ("worker-vblank-cold", "shared", "closed-stop"),
    }
    targets = []
    for address, (slice_name, lane, role) in sorted(target_slices.items()):
        hot = address in {SCHEDULER_STEP_EIPS[3], SCHEDULER_STEP_EIPS[6]}
        targets.append(
            {
                "address": address,
                "slice": slice_name,
                "lane": lane,
                "role": role,
                "estimated_native_time_ns": 50_000 if hot else 100,
                "guest_steps": 20_000 if hot else 20,
                "module_calls": 2_000 if hot else 2,
            }
        )
    transitions = [
        {"source": SCHEDULER_STEP_EIPS[0], "target": SCHEDULER_STEP_EIPS[3], "count": 1},
        {"source": SCHEDULER_STEP_EIPS[3], "target": SCHEDULER_STEP_EIPS[6], "count": 800},
        {"source": SCHEDULER_STEP_EIPS[6], "target": SCHEDULER_STEP_EIPS[3], "count": 799},
        {"source": SCHEDULER_STEP_EIPS[0], "target": SCHEDULER_STEP_EIPS[1], "count": 1},
        {"source": SCHEDULER_STEP_EIPS[1], "target": SCHEDULER_STEP_EIPS[2], "count": 1},
        {"source": SCHEDULER_STEP_EIPS[2], "target": SCHEDULER_STEP_EIPS[4], "count": 1},
        {"source": SCHEDULER_STEP_EIPS[4], "target": SCHEDULER_STEP_EIPS[5], "count": 1},
        {"source": SCHEDULER_STEP_EIPS[5], "target": SCHEDULER_STOP_EIP, "count": 1},
        {"source": SCHEDULER_STEP_EIPS[3], "target": SCHEDULER_TLS_HELPER_EIP, "count": 800},
        {"source": SCHEDULER_TLS_HELPER_EIP, "target": SCHEDULER_STOP_EIP, "count": 1},
    ]
    return Ia32CoverageProfile(
        workload="asset-free-boot-frontend-lesson-one-worker-vblank",
        mode=mode,
        targets=tuple(targets),
        transitions=tuple(transitions),
        boundary_exits=(
            {
                "source": SCHEDULER_STEP_EIPS[3],
                "target": SCHEDULER_SERVICE_TARGET,
                "boundary": "service",
                "count": 800,
            },
            {
                "source": SCHEDULER_STEP_EIPS[6],
                "boundary": "render",
                "count": 60,
            },
        ),
    )


def build_asset_free_phase6_fixture(root: Path) -> Phase6ProofFixture:
    root.mkdir(parents=True, exist_ok=True)
    scheduler = build_asset_free_phase5_fixture(root / "scheduler")
    text_base = 0x000C5E00
    xbe_payload, _layout = build_synthetic_xbe(
        text_payload=b"\xc3",
        text_payload_offset=0x20,
        text_virtual_address=text_base,
    )
    mutable_xbe = bytearray(xbe_payload)
    for function in scheduler.capsule.functions:
        for instruction in function.instructions:
            offset = 0x1000 + instruction.address - text_base
            payload = bytes.fromhex(instruction.bytes_hex)
            mutable_xbe[offset : offset + len(payload)] = payload
    text_payload = bytes(mutable_xbe[0x1000:0x1400])
    mutable_xbe[0x400 + 36 : 0x400 + 56] = hashlib.sha1(
        struct.pack("<I", len(text_payload)) + text_payload
    ).digest()
    xbe_path = root / "phase6-fixture.xbe"
    xbe_path.write_bytes(mutable_xbe)
    store_path = root / "decoded-blocks.sqlite3"
    _write_phase2_decoded_store(
        store_path,
        xbe_sha256=_sha256(bytes(mutable_xbe)),
        functions=scheduler.capsule.functions,
    )
    return Phase6ProofFixture(
        scheduler=scheduler,
        xbe_path=xbe_path,
        decoded_block_store_path=store_path,
        coverage_profile=_phase6_profile(),
    )


def _evaluate_phase6_proof(
    fixture: Phase6ProofFixture,
    artifact: Ia32SliceArtifact,
    peer_artifact: Ia32SliceArtifact,
) -> dict[str, Any]:
    phase5 = _evaluate_phase5_proof(fixture.scheduler, artifact, peer_artifact)
    coverage_map = json.loads(
        (artifact.root / "coverage-growth-map.json").read_text(encoding="utf-8")
    )
    ranking = coverage_map["ranking"]
    hot_scc = ranking[0]
    plan = inspect_ia32_decoded_store(
        fixture.xbe_path,
        fixture.decoded_block_store_path,
    )
    discovery_profile = _phase6_profile(mode="diagnostic-discovery")
    discovery_profile = Ia32CoverageProfile(
        workload=discovery_profile.workload,
        mode=discovery_profile.mode,
        targets=discovery_profile.targets,
        transitions=discovery_profile.transitions
        + (
            {
                "source": SCHEDULER_STEP_EIPS[6],
                "target": 0x000C7000,
                "count": 1,
            },
        ),
        boundary_exits=discovery_profile.boundary_exits,
        frontier_interpreter_invocations=1,
        frontier_interpreter_steps=7,
    )
    discovery = plan_ia32_coverage_growth(
        plan,
        discovery_profile,
        registered_service_targets=(SCHEDULER_SERVICE_TARGET,),
    ).coverage_growth_map
    normal_unknown_rejected = False
    try:
        plan_ia32_coverage_growth(
            plan,
            replace(
                discovery_profile,
                mode="normal-validation",
                frontier_interpreter_invocations=0,
                frontier_interpreter_steps=0,
            ),
            registered_service_targets=(SCHEDULER_SERVICE_TARGET,),
        )
    except Ia32BackendError as exc:
        normal_unknown_rejected = "unknown executable targets" in str(exc)
    checks = {
        "phase5_scheduler_proof_passed": phase5["passed"],
        "artifact_reproducible": artifact.artifact_id == peer_artifact.artifact_id
        and sha256_file(artifact.executable) == sha256_file(peer_artifact.executable),
        "coverage_map_reproducible": sha256_file(artifact.root / "coverage-growth-map.json")
        == sha256_file(peer_artifact.root / "coverage-growth-map.json"),
        "hot_lesson_one_scc_ranked_first": set(hot_scc["targets"])
        == {SCHEDULER_STEP_EIPS[3], SCHEDULER_STEP_EIPS[6]},
        "ranking_not_source_adjacency": hot_scc["scc_entry"]
        != min(record["address"] for record in coverage_map["profile"]["targets"]),
        "three_monotonic_vertical_slices": [record["name"] for record in coverage_map["slices"]]
        == [
            "lesson-one-hot-scc",
            "boot-frontend-continuity",
            "worker-vblank-cold",
        ]
        and all(record["closed"] for record in coverage_map["slices"]),
        "lesson_one_service_and_render_exits": {
            record["boundary"] for record in coverage_map["slices"][0]["boundary_exits"]
        }
        == {"service", "render"},
        "normal_unknown_targets_zero": coverage_map["unknown_targets"] == [],
        "normal_frontier_interpreter_zero": coverage_map["profile"][
            "frontier_interpreter_invocations"
        ]
        == 0
        and coverage_map["profile"]["frontier_interpreter_steps"] == 0,
        "normal_promotion_eligible": coverage_map["promotion_eligible"] is True,
        "closed_cutover_manifest": coverage_map["final_cutover_candidate"] is True
        and artifact.manifest["coverage_closed"] is True,
        "diagnostic_unknown_recorded_for_next_store": discovery[
            "required_next_decoded_store_targets"
        ]
        == [0x000C7000]
        and discovery["promotion_eligible"] is False,
        "normal_unknown_rejected": normal_unknown_rejected,
        "normal_runtime_python_callbacks_zero": artifact.manifest["normal_runtime_python_callbacks"]
        == 0,
        "runtime_decoding": artifact.manifest["runtime_decoding"],
        "runtime_compilation": artifact.manifest["runtime_compilation"],
        "runtime_code_patching": artifact.manifest["runtime_code_patching"],
        "native_promotion": artifact.manifest["native_promotion"],
        "raw_xbe_execution": artifact.manifest["raw_xbe_execution"],
    }
    negative_checks = {
        "runtime_decoding",
        "runtime_compilation",
        "runtime_code_patching",
        "native_promotion",
        "raw_xbe_execution",
    }
    stable_identity = {
        "name": "asset-free-measured-vertical-slice-growth",
        "contract_id": ia32_coverage_growth_contract_id(),
        "capsule_id": fixture.scheduler.capsule.capsule_id,
        "artifact_id": artifact.artifact_id,
        "peer_artifact_id": peer_artifact.artifact_id,
        "coverage_profile_id": coverage_map["profile_id"],
        "coverage_ranking": ranking,
        "vertical_slices": coverage_map["slices"],
        "checks": checks,
    }
    return {
        **stable_identity,
        "proof_id": _sha256(_canonical_json(stable_identity)),
        "passed": all(
            value is False if key in negative_checks else bool(value)
            for key, value in checks.items()
        ),
        "discovery_audit": {
            "mode": discovery["profile"]["mode"],
            "frontier_interpreter_invocations": discovery["profile"][
                "frontier_interpreter_invocations"
            ],
            "frontier_interpreter_steps": discovery["profile"]["frontier_interpreter_steps"],
            "required_next_decoded_store_targets": discovery["required_next_decoded_store_targets"],
            "promotion_eligible": discovery["promotion_eligible"],
        },
    }


def run_asset_free_phase6_proofs(build_dir: Path) -> dict[str, Any]:
    first = build_asset_free_phase6_fixture(build_dir / "fixtures-a")
    second = build_asset_free_phase6_fixture(build_dir / "fixtures-b")
    if first.scheduler.capsule.capsule_id != second.scheduler.capsule.capsule_id:
        raise Ia32BackendError("Phase-6 capsule rebuild changed its identity")
    artifact = build_ia32_coverage_growth_artifact(
        first.scheduler.capsule,
        xbe_path=first.xbe_path,
        decoded_block_store_path=first.decoded_block_store_path,
        coverage_profile=first.coverage_profile,
        stop_eip=first.scheduler.stop_eip,
        build_dir=build_dir / "artifacts-a",
    )
    peer_artifact = build_ia32_coverage_growth_artifact(
        second.scheduler.capsule,
        xbe_path=second.xbe_path,
        decoded_block_store_path=second.decoded_block_store_path,
        coverage_profile=second.coverage_profile,
        stop_eip=second.scheduler.stop_eip,
        build_dir=build_dir / "artifacts-b",
    )
    proof = _evaluate_phase6_proof(first, artifact, peer_artifact)
    identity = {
        "format": PHASE6_PROOF_SET_FORMAT,
        "version": PHASE6_PROOF_SET_VERSION,
        "coverage_growth_contract_id": ia32_coverage_growth_contract_id(),
        "proof_ids": [proof["proof_id"]],
    }
    report = {
        **identity,
        "proof_set_id": _sha256(_canonical_json(identity)),
        "passed": proof["passed"],
        "coverage_growth_contract": ia32_coverage_growth_contract(),
        "proofs": [proof],
    }
    if PHASE6_PROOF_SET_ID and report["proof_set_id"] != PHASE6_PROOF_SET_ID:
        raise Ia32BackendError(
            "the frozen Phase-6 proof set changed without a proof-contract version bump"
        )
    return report


def _evaluate_phase7_proof(
    fixture: Phase6ProofFixture,
    artifact: Ia32SliceArtifact,
    peer_artifact: Ia32SliceArtifact,
) -> dict[str, Any]:
    first_reference = execute_ia32_scheduler_reference(
        fixture.scheduler.capsule,
        stop_eip=fixture.scheduler.stop_eip,
    )
    second_capsule = replace(
        fixture.scheduler.capsule,
        state=first_reference.state,
        memory=first_reference.memory,
    )
    second_reference = execute_ia32_scheduler_reference(
        second_capsule,
        stop_eip=fixture.scheduler.stop_eip,
    )
    host_memory = SparseMemory.from_pages(second_reference.memory.export_pages())
    publication_page = SCHEDULER_AUDIO_ADDRESS & ~4095
    publication_byte = host_memory.read(SCHEDULER_AUDIO_ADDRESS + 15, 1)[0] ^ 0x5A
    host_memory.write(SCHEDULER_AUDIO_ADDRESS + 15, bytes((publication_byte,)))
    third_capsule = replace(
        fixture.scheduler.capsule,
        state=second_reference.state,
        memory=host_memory,
    )
    third_reference = execute_ia32_scheduler_reference(
        third_capsule,
        stop_eip=fixture.scheduler.stop_eip,
    )
    with Ia32PersistentWorker.start(artifact, fixture.scheduler.capsule) as worker:
        worker_process_id = worker.process_id
        first_native = worker.dispatch(
            fixture.scheduler.capsule,
            command=WORKER_COMMAND_SCHEDULE,
            snapshot_memory=True,
        )
        second_native = worker.dispatch(
            fixture.scheduler.capsule,
            command=WORKER_COMMAND_SCHEDULE,
            snapshot_memory=True,
        )
        third_native = worker.dispatch(
            third_capsule,
            command=WORKER_COMMAND_SCHEDULE,
            republish_pages=(publication_page,),
            snapshot_memory=True,
        )
    first_transport = first_native.memory_transport
    second_transport = second_native.memory_transport
    if first_transport is None or second_transport is None:
        raise Ia32BackendError("Phase-7 worker omitted persistent-memory telemetry")
    third_transport = third_native.memory_transport
    if third_transport is None:
        raise Ia32BackendError("Phase-7 worker omitted host-publication telemetry")
    if (
        first_native.resident_scheduler is None
        or second_native.resident_scheduler is None
        or third_native.resident_scheduler is None
    ):
        raise Ia32BackendError("Phase-7 worker omitted resident scheduler results")
    selected_ranges = (
        (SCHEDULER_SERVICE_POINTER, 4),
        (SCHEDULER_RENDER_ADDRESS, 16),
        (SCHEDULER_AUDIO_ADDRESS, 16),
        (SCHEDULER_ACTIVE_TLS, 4096),
        *((address, 4096) for address in SCHEDULER_LANE_TLS),
    )

    def memory_id(memory: SparseMemory) -> str:
        return _sha256(b"".join(memory.read(address, size) for address, size in selected_ranges))

    def normalized_primary(state: CpuState) -> str:
        record = cpu_state_record(state)
        record["fs_base"] = fixture.scheduler.capsule.state.fs_base
        return _state_identity(normalize_ia32_phase3_state(cpu_state_from_record(record)))

    missing_artifact_rejected = False
    try:
        select_ia32_launcher_backend(None)
    except Ia32BackendError as exc:
        missing_artifact_rejected = "requires a verified" in str(exc)
    cutover_map = json.loads(
        (artifact.root / "launcher-cutover-map.json").read_text(encoding="utf-8")
    )
    checks = {
        "same_isa_is_default": select_ia32_launcher_backend(artifact) == "same-isa-ia32",
        "oracle_is_explicit_diagnostic": select_ia32_launcher_backend(
            None,
            requested="diagnostic-oracle",
        )
        == "fusion-only-64-bit-diagnostic",
        "missing_artifact_fails_closed": missing_artifact_rejected,
        "artifact_reproducible": artifact.artifact_id == peer_artifact.artifact_id
        and sha256_file(artifact.executable) == sha256_file(peer_artifact.executable),
        "cutover_map_reproducible": sha256_file(artifact.root / "launcher-cutover-map.json")
        == sha256_file(peer_artifact.root / "launcher-cutover-map.json"),
        "coverage_remains_closed": cutover_map["coverage_profile_id"]
        == artifact.manifest["coverage_profile_id"],
        "resident_process_reused": first_native.worker_process_id
        == second_native.worker_process_id
        == third_native.worker_process_id
        == worker_process_id
        and (
            first_native.worker_dispatch_index,
            second_native.worker_dispatch_index,
            third_native.worker_dispatch_index,
        )
        == (1, 2, 3),
        "first_state_matches": normalized_primary(first_reference.state)
        == normalized_primary(first_native.state),
        "second_state_matches": normalized_primary(second_reference.state)
        == normalized_primary(second_native.state),
        "first_memory_matches": memory_id(first_reference.memory)
        == memory_id(first_native.memory),
        "second_memory_matches": memory_id(second_reference.memory)
        == memory_id(second_native.memory),
        "host_publication_state_matches": normalized_primary(third_reference.state)
        == normalized_primary(third_native.state),
        "host_publication_memory_matches": memory_id(third_reference.memory)
        == memory_id(third_native.memory),
        "first_scheduler_matches": list(first_reference.scheduler_trace)
        == first_native.resident_scheduler["trace"],
        "second_scheduler_matches": list(second_reference.scheduler_trace)
        == second_native.resident_scheduler["trace"],
        "host_publication_scheduler_matches": list(third_reference.scheduler_trace)
        == third_native.resident_scheduler["trace"],
        "one_full_seed_only": first_transport["initial_page_seed_count"]
        == second_transport["initial_page_seed_count"]
        == len(artifact.manifest["page_addresses"]),
        "warm_inputs_are_control_only": first_transport["host_publication_page_count"] == 0
        and second_transport["host_publication_page_count"] == 0
        and first_transport["input_pages_bypassed"]
        == second_transport["input_pages_bypassed"]
        == len(artifact.manifest["page_addresses"]),
        "explicit_host_page_publication": third_transport["host_publication_page_count"] == 1
        and third_transport["host_publication_bytes"] == 4096
        and third_transport["input_pages_bypassed"]
        == len(artifact.manifest["page_addresses"]) - 1,
        "outputs_are_dirty_only": first_transport["native_publication_page_count"]
        == len(first_native.dirty_publications)
        and second_transport["native_publication_page_count"]
        == len(second_native.dirty_publications)
        and first_transport["native_publication_bytes"]
        == len(first_native.dirty_publications) * 4096
        and second_transport["native_publication_bytes"]
        == len(second_native.dirty_publications) * 4096,
        "bulk_roundtrip_eliminated": first_transport["total_command_bytes_transferred"]
        < first_transport["legacy_full_roundtrip_bytes"]
        and second_transport["total_command_bytes_transferred"]
        < second_transport["legacy_full_roundtrip_bytes"],
        "zero_cross_backend_exits": first_transport["cross_backend_exits"]
        == second_transport["cross_backend_exits"]
        == artifact.manifest["cross_backend_exits"]
        == 0,
        "normal_runtime_python_callbacks_zero": artifact.manifest[
            "normal_runtime_python_callbacks"
        ]
        == 0,
        "runtime_decoding": artifact.manifest["runtime_decoding"],
        "runtime_compilation": artifact.manifest["runtime_compilation"],
        "runtime_code_patching": artifact.manifest["runtime_code_patching"],
        "native_promotion": artifact.manifest["native_promotion"],
        "raw_xbe_execution": artifact.manifest["raw_xbe_execution"],
    }
    negative_checks = {
        "runtime_decoding",
        "runtime_compilation",
        "runtime_code_patching",
        "native_promotion",
        "raw_xbe_execution",
    }
    stable_identity = {
        "name": "asset-free-normal-launcher-cutover",
        "contract_id": ia32_launcher_cutover_contract_id(),
        "capsule_id": fixture.scheduler.capsule.capsule_id,
        "artifact_id": artifact.artifact_id,
        "peer_artifact_id": peer_artifact.artifact_id,
        "memory_transports": [first_transport, second_transport, third_transport],
        "checks": checks,
    }
    return {
        **stable_identity,
        "proof_id": _sha256(_canonical_json(stable_identity)),
        "passed": all(
            value is False if key in negative_checks else bool(value)
            for key, value in checks.items()
        ),
        "worker_process_id": worker_process_id,
        "worker_startup_ns": first_native.worker_startup_ns,
        "resident_dispatch_ns": [
            first_native.resident_dispatch_ns,
            second_native.resident_dispatch_ns,
            third_native.resident_dispatch_ns,
        ],
    }


def run_asset_free_phase7_proofs(build_dir: Path) -> dict[str, Any]:
    first = build_asset_free_phase6_fixture(build_dir / "fixtures-a")
    second = build_asset_free_phase6_fixture(build_dir / "fixtures-b")
    if first.scheduler.capsule.capsule_id != second.scheduler.capsule.capsule_id:
        raise Ia32BackendError("Phase-7 capsule rebuild changed its identity")
    artifact = build_ia32_launcher_cutover_artifact(
        first.scheduler.capsule,
        xbe_path=first.xbe_path,
        decoded_block_store_path=first.decoded_block_store_path,
        coverage_profile=first.coverage_profile,
        stop_eip=first.scheduler.stop_eip,
        build_dir=build_dir / "artifacts-a",
    )
    peer_artifact = build_ia32_launcher_cutover_artifact(
        second.scheduler.capsule,
        xbe_path=second.xbe_path,
        decoded_block_store_path=second.decoded_block_store_path,
        coverage_profile=second.coverage_profile,
        stop_eip=second.scheduler.stop_eip,
        build_dir=build_dir / "artifacts-b",
    )
    proof = _evaluate_phase7_proof(first, artifact, peer_artifact)
    identity = {
        "format": PHASE7_PROOF_SET_FORMAT,
        "version": PHASE7_PROOF_SET_VERSION,
        "launcher_cutover_contract_id": ia32_launcher_cutover_contract_id(),
        "proof_ids": [proof["proof_id"]],
    }
    report = {
        **identity,
        "proof_set_id": _sha256(_canonical_json(identity)),
        "passed": proof["passed"],
        "launcher_cutover_contract": ia32_launcher_cutover_contract(),
        "proofs": [proof],
    }
    if PHASE7_PROOF_SET_ID and report["proof_set_id"] != PHASE7_PROOF_SET_ID:
        raise Ia32BackendError(
            "the frozen Phase-7 proof set changed without a proof-contract version bump"
        )
    return report


def freeze_retained_phase0_proofs(
    *,
    compute_capsule_path: Path,
    compute_artifact_path: Path,
    service_capsule_path: Path,
    service_artifact_path: Path,
    expected_compute_capsule_id: str | None = None,
    expected_compute_artifact_id: str | None = None,
    expected_service_capsule_id: str | None = None,
    expected_service_artifact_id: str | None = None,
) -> dict[str, Any]:
    compute_capsule = load_replay_capsule(compute_capsule_path)
    service_capsule = load_replay_capsule(service_capsule_path)
    compute_artifact = load_ia32_slice_artifact(compute_artifact_path)
    service_artifact = load_ia32_slice_artifact(service_artifact_path)
    proofs = [
        _evaluate_proof(
            Phase0ProofFixture(
                "retained-compute",
                "compute",
                compute_capsule,
                _artifact_stop_eip(compute_artifact),
            ),
            compute_artifact,
            expected_capsule_id=expected_compute_capsule_id,
            expected_artifact_id=expected_compute_artifact_id,
            normalize_historical_mmx_alias=True,
        ),
        _evaluate_proof(
            Phase0ProofFixture(
                "retained-host-service",
                "host-service",
                service_capsule,
                _artifact_stop_eip(service_artifact),
            ),
            service_artifact,
            expected_capsule_id=expected_service_capsule_id,
            expected_artifact_id=expected_service_artifact_id,
        ),
    ]
    return _proof_set_report(proofs, kind="retained-local")


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(path)


def _require_local_report_output(path: Path) -> None:
    resolved = path.resolve()
    roots = (REPO_ROOT / "reports" / "local", REPO_ROOT / "data" / "local")
    if resolved.suffix.casefold() != ".json" or not any(
        resolved == root.resolve() or root.resolve() in resolved.parents for root in roots
    ):
        raise Ia32BackendError(
            f"retained Phase-0 report must be JSON under reports/local or data/local: {resolved}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    synthetic = commands.add_parser("synthetic", help="Run both asset-free Phase-0 proofs.")
    synthetic.add_argument(
        "--build-dir",
        type=Path,
        default=REPO_ROOT / "build" / "local" / "ia32-phase0",
    )
    synthetic.add_argument("--report", type=Path, required=True)
    persistent = commands.add_parser(
        "persistent",
        help="Run the asset-free Phase-1 resident-worker proofs.",
    )
    persistent.add_argument(
        "--build-dir",
        type=Path,
        default=REPO_ROOT / "build" / "local" / "ia32-phase1",
    )
    persistent.add_argument("--report", type=Path, required=True)
    decoded_store = commands.add_parser(
        "decoded-store",
        help="Run the asset-free Phase-2 decoded-store artifact proof.",
    )
    decoded_store.add_argument(
        "--build-dir",
        type=Path,
        default=REPO_ROOT / "build" / "local" / "ia32-phase2",
    )
    decoded_store.add_argument("--report", type=Path, required=True)
    architecture = commands.add_parser(
        "architecture",
        help="Run the asset-free Phase-3 architecture and memory proofs.",
    )
    architecture.add_argument(
        "--build-dir",
        type=Path,
        default=REPO_ROOT / "build" / "local" / "ia32-phase3",
    )
    architecture.add_argument("--report", type=Path, required=True)
    host_abi = commands.add_parser(
        "host-abi",
        help="Run the asset-free Phase-4 native host ABI proofs.",
    )
    host_abi.add_argument(
        "--build-dir",
        type=Path,
        default=REPO_ROOT / "build" / "local" / "ia32-phase4",
    )
    host_abi.add_argument("--report", type=Path, required=True)
    scheduler = commands.add_parser(
        "scheduler",
        help="Run the asset-free Phase-5 resident scheduler proof.",
    )
    scheduler.add_argument(
        "--build-dir",
        type=Path,
        default=REPO_ROOT / "build" / "local" / "ia32-phase5",
    )
    scheduler.add_argument("--report", type=Path, required=True)
    coverage = commands.add_parser(
        "coverage",
        help="Run the asset-free Phase-6 measured coverage-growth proof.",
    )
    coverage.add_argument(
        "--build-dir",
        type=Path,
        default=REPO_ROOT / "build" / "local" / "ia32-phase6",
    )
    coverage.add_argument("--report", type=Path, required=True)
    cutover = commands.add_parser(
        "cutover",
        help="Run the asset-free Phase-7 normal-launcher cutover proof.",
    )
    cutover.add_argument(
        "--build-dir",
        type=Path,
        default=REPO_ROOT / "build" / "local" / "ia32-phase7",
    )
    cutover.add_argument("--report", type=Path, required=True)
    freeze = commands.add_parser("freeze", help="Seal the two retained local prototype proofs.")
    freeze.add_argument("--compute-capsule", type=Path, required=True)
    freeze.add_argument("--compute-artifact", type=Path, required=True)
    freeze.add_argument("--compute-capsule-id")
    freeze.add_argument("--compute-artifact-id")
    freeze.add_argument("--service-capsule", type=Path, required=True)
    freeze.add_argument("--service-artifact", type=Path, required=True)
    freeze.add_argument("--service-capsule-id")
    freeze.add_argument("--service-artifact-id")
    freeze.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "synthetic":
            report = run_asset_free_phase0_proofs(args.build_dir)
        elif args.command == "persistent":
            report = run_asset_free_phase1_proofs(args.build_dir)
        elif args.command == "decoded-store":
            report = run_asset_free_phase2_proofs(args.build_dir)
        elif args.command == "architecture":
            report = run_asset_free_phase3_proofs(args.build_dir)
        elif args.command == "host-abi":
            report = run_asset_free_phase4_proofs(args.build_dir)
        elif args.command == "scheduler":
            report = run_asset_free_phase5_proofs(args.build_dir)
        elif args.command == "coverage":
            report = run_asset_free_phase6_proofs(args.build_dir)
        elif args.command == "cutover":
            report = run_asset_free_phase7_proofs(args.build_dir)
        else:
            _require_local_report_output(args.report)
            report = freeze_retained_phase0_proofs(
                compute_capsule_path=args.compute_capsule,
                compute_artifact_path=args.compute_artifact,
                service_capsule_path=args.service_capsule,
                service_artifact_path=args.service_artifact,
                expected_compute_capsule_id=args.compute_capsule_id,
                expected_compute_artifact_id=args.compute_artifact_id,
                expected_service_capsule_id=args.service_capsule_id,
                expected_service_artifact_id=args.service_artifact_id,
            )
        _write_report(args.report, report)
        return 0 if report["passed"] else 1
    except (Ia32BackendError, OSError, ReplayCapsuleError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    sys.exit(main())
