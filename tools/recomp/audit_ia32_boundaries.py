#!/usr/bin/env python3
"""Resolve and batch Phase-7 IA-32 guarded boundaries without launching gameplay."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from tools.playability.replay_capsule import load_replay_capsule
from tools.recomp.ia32_native_backend import (
    PHASE7_CAPTURED_REGISTER_IMPORT_BINDINGS,
    PHASE7_OFFLINE_BOUNDARY_OPTIMIZATION_INTERVAL,
    PHASE7_OFFLINE_BOUNDARY_OPTIMIZATIONS,
    PHASE7_OFFLINE_KERNEL_SERVICE_SPECS,
    PHASE7_OFFLINE_VTABLE_RDATA_END,
    PHASE7_OFFLINE_VTABLE_RDATA_START,
    PHASE7_OFFLINE_VTABLE_SLOT_LIMIT,
    PHASE7_PROFILED_BOOT_SERVICE_SPECS,
    SUPPORTED_WORKLOAD_SERVICE_SPECS,
    Ia32BackendError,
    _canonical_json,
    _guarded_static_boundaries,
    _normal_live_boot_capsule,
    _normalized_coverage_profile,
    _phase6_host_service_thunks,
    _phase6_verified_indirect_targets,
    _phase7_offline_closed_vtable_targets,
    _phase7_observed_preflight,
    _sha256,
    inspect_ia32_decoded_store,
    load_ia32_coverage_profile,
)
from tools.recomp.x86_lifter import X86Instruction
from tools.xbe.xbe_info import parse_xbe_file


REPORT_FORMAT = "b2-recomp-ia32-offline-boundary-audit"
REPORT_VERSION = 3


def _load_artifact_manifest(path: Path) -> tuple[Path, dict[str, Any]]:
    resolved = path.resolve()
    record = json.loads(resolved.read_text(encoding="utf-8"))
    if record.get("format") == "b2-recomp-ia32-artifact-index":
        resolved = (resolved.parent / str(record["artifact_manifest"])).resolve()
        record = json.loads(resolved.read_text(encoding="utf-8"))
    if record.get("format") != "b2-recomp-ia32-slice":
        raise Ia32BackendError(f"not an IA-32 artifact manifest: {resolved}")
    if record.get("normal_live_launch") is not True:
        raise Ia32BackendError(f"baseline is not a normal-live artifact: {resolved}")
    return resolved, record


def _require_provenance(label: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise Ia32BackendError(
            f"baseline {label} mismatch: expected {expected!r}, got {actual!r}"
        )


def _kernel_import_bindings(xbe_path: Path) -> dict[int, dict[str, Any]]:
    info = parse_xbe_file(xbe_path)
    imports = sorted(
        info["kernel_imports"]["imports"],
        key=lambda record: int(record["ordinal"]),
    )
    return {
        int(record["thunk_address"]): {
            "cell": int(record["thunk_address"]),
            "ordinal": int(record["ordinal"]),
            "name": str(record.get("name") or f"ordinal_{int(record['ordinal']):04d}"),
            "target": 0xE0000000 + index * 0x10,
        }
        for index, record in enumerate(imports)
    }


def _instruction_map(plan: Any) -> dict[int, X86Instruction]:
    return {
        instruction.address: instruction
        for function in plan.functions
        for instruction in function.instructions
    }


def _manifest_guard_map(manifest: Mapping[str, Any]) -> dict[int, str]:
    """Return the recursive guard inventory emitted by the native artifact."""

    return {
        int(record["address"]): str(record["reason"])
        for record in manifest["guarded_boundaries"]
    }


def _operand_family(instruction: X86Instruction) -> tuple[str, str]:
    operand = instruction.operands[0]
    if operand.kind == "reg":
        return "register-callback", str(operand.reg)
    if operand.kind != "mem":
        return "unknown-indirect", operand.kind
    if operand.absolute is not None:
        return "absolute-callback-cell", f"0x{int(operand.absolute):08X}"
    if operand.index is not None:
        return (
            "indexed-table",
            f"base={operand.base or '-'};index={operand.index};scale={operand.scale};"
            f"disp={operand.displacement}",
        )
    return (
        "virtual-slot",
        f"base={operand.base or '-'};slot=0x{operand.displacement & 0xFFFFFFFF:X}",
    )


def _optimization_budget(
    resolved_count: int,
    baseline_optimization_ids: Sequence[str] = (),
    optimization_records: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    required = resolved_count // PHASE7_OFFLINE_BOUNDARY_OPTIMIZATION_INTERVAL
    baseline_ids = {str(value) for value in baseline_optimization_ids}
    optimizations = [
        dict(record)
        for record in (
            PHASE7_OFFLINE_BOUNDARY_OPTIMIZATIONS
            if optimization_records is None
            else optimization_records
        )
        if str(record.get("id")) not in baseline_ids
    ]
    credited = sum(
        int(record.get("boundary_credit", 0))
        for record in optimizations
        if record.get("kind") == "runtime"
    )
    return {
        "interval": PHASE7_OFFLINE_BOUNDARY_OPTIMIZATION_INTERVAL,
        "resolved_boundaries": resolved_count,
        "baseline_optimization_ids": sorted(baseline_ids),
        "new_optimization_count": len(optimizations),
        "required_optimization_count": required,
        "credited_boundary_count": credited,
        "passed": credited >= required * PHASE7_OFFLINE_BOUNDARY_OPTIMIZATION_INTERVAL,
        "optimizations": optimizations,
    }


def audit_ia32_boundaries(
    capsule_path: Path,
    *,
    xbe_path: Path,
    decoded_block_store_path: Path,
    coverage_profile_path: Path,
    baseline_manifest_path: Path,
    candidate_manifest_path: Path | None = None,
) -> dict[str, Any]:
    """Project a normal-live guard inventory using only retained static evidence."""

    baseline_path, baseline = _load_artifact_manifest(baseline_manifest_path)
    evidence = load_replay_capsule(capsule_path)
    stop_eip = int(baseline["normal_live_flip_stop_eip"])
    capsule = _normal_live_boot_capsule(
        evidence,
        xbe_path=xbe_path,
        stop_eip=stop_eip,
    )
    profile = load_ia32_coverage_profile(coverage_profile_path)
    baseline_inputs = baseline.get("inputs")
    if not isinstance(baseline_inputs, Mapping):
        raise Ia32BackendError("baseline artifact has no input provenance")
    _require_provenance(
        "capsule ID",
        capsule.capsule_id,
        baseline_inputs.get("capsule_id"),
    )
    _require_provenance(
        "coverage profile ID",
        _sha256(_canonical_json(_normalized_coverage_profile(profile))),
        baseline.get("coverage_profile_id"),
    )
    services = _phase6_host_service_thunks(
        capsule,
        profile,
        complete_offline_registry=True,
    )
    capsule = replace(
        capsule,
        service_state={
            **capsule.service_state,
            "service_registry": [
                {
                    "target": service.target,
                    "shim_name": service.shim_name,
                    "kind": service.kind,
                    "value": service.value,
                    "stack_cleanup_bytes": service.stack_cleanup_bytes,
                    "calling_convention": service.calling_convention,
                    "argument_count": service.argument_count,
                    "boundary": service.boundary,
                    "execution": service.execution,
                    "runtime_kind": service.runtime_kind,
                    "runtime_value": service.runtime_value,
                    "normal_live_body": service.normal_live_body,
                }
                for service in (services[target] for target in sorted(services))
            ],
        },
    )
    plan = inspect_ia32_decoded_store(
        xbe_path,
        decoded_block_store_path,
        host_services=services,
    )
    decoded_inputs = baseline_inputs.get("decoded_store_artifact")
    if not isinstance(decoded_inputs, Mapping):
        raise Ia32BackendError("baseline artifact has no decoded-store provenance")
    _require_provenance(
        "XBE SHA-256",
        plan.xbe_sha256,
        decoded_inputs.get("xbe_sha256"),
    )
    if candidate_manifest_path is None:
        _require_provenance(
            "decoded-store snapshot ID",
            plan.store_snapshot_id,
            decoded_inputs.get("store_snapshot_id"),
        )
    expanded = replace(capsule, functions=plan.functions)
    verified = _phase6_verified_indirect_targets(
        expanded,
        plan,
        profile,
        services,
        {},
        observed_memory=evidence.memory,
    )
    plan = inspect_ia32_decoded_store(
        xbe_path,
        decoded_block_store_path,
        host_services=services,
        verified_indirect_targets=verified,
    )
    expanded = replace(expanded, functions=plan.functions)
    preflight = _phase7_observed_preflight(
        expanded,
        plan,
        profile,
        services,
        verified,
    )
    raw_projected_guards = _guarded_static_boundaries(
        preflight.instructions,
        stop_eip,
        verified,
        services,
    )
    baseline_guards = _manifest_guard_map(baseline)
    candidate_path: Path | None = None
    candidate: dict[str, Any] | None = None
    if candidate_manifest_path is None:
        # Projection-only compatibility mode. Architecture rewrites (TLS, MMIO,
        # timestamps, and privileged seams) are validated later in the build
        # pipeline, so retain the baseline's filtered non-indirect set.
        projected_guards = {
            address: reason
            for address, reason in baseline_guards.items()
            if "indirect-target" not in reason
        }
        projected_guards.update(
            {
                address: reason
                for address, reason in raw_projected_guards.items()
                if "indirect-target" in reason
            }
        )
    else:
        candidate_path, candidate = _load_artifact_manifest(candidate_manifest_path)
        candidate_inputs = candidate.get("inputs")
        candidate_decoded_inputs = (
            candidate_inputs.get("decoded_store_artifact")
            if isinstance(candidate_inputs, Mapping)
            else None
        )
        if not isinstance(candidate_decoded_inputs, Mapping):
            raise Ia32BackendError("candidate artifact has no decoded-store provenance")
        _require_provenance(
            "candidate capsule ID",
            capsule.capsule_id,
            candidate_inputs.get("capsule_id"),
        )
        _require_provenance(
            "candidate coverage profile ID",
            _sha256(_canonical_json(_normalized_coverage_profile(profile))),
            candidate.get("coverage_profile_id"),
        )
        _require_provenance(
            "candidate XBE SHA-256",
            plan.xbe_sha256,
            candidate_decoded_inputs.get("xbe_sha256"),
        )
        _require_provenance(
            "candidate decoded-store snapshot ID",
            plan.store_snapshot_id,
            candidate_decoded_inputs.get("store_snapshot_id"),
        )
        required_contract = {
            "coverage_closed": True,
            "normal_execution_eligible": True,
            "runtime_compilation": False,
            "runtime_decoding": False,
            "runtime_code_patching": False,
            "native_promotion": False,
            "raw_xbe_execution": False,
            "automatic_cross_backend_fallback": False,
            "frontier_interpreter_invocations": 0,
            "frontier_interpreter_steps": 0,
            "cross_backend_exits": 0,
            "normal_runtime_python_callbacks": 0,
            "normal_live_pending_host_service_count": 0,
        }
        for field, expected in required_contract.items():
            if candidate.get(field) != expected:
                raise Ia32BackendError(
                    f"candidate artifact is not static-clean: {field}="
                    f"{candidate.get(field)!r}, expected {expected!r}"
                )
        projected_guards = _manifest_guard_map(candidate)
    instructions = _instruction_map(plan)
    closed_vtable_targets = _phase7_offline_closed_vtable_targets(
        evidence.memory,
        instructions,
        set(baseline_guards),
        set(instructions) | set(services),
    )
    import_bindings = _kernel_import_bindings(xbe_path)
    service_specs: dict[int, Mapping[str, Any]] = {
        **SUPPORTED_WORKLOAD_SERVICE_SPECS,
        **PHASE7_PROFILED_BOOT_SERVICE_SPECS,
        **PHASE7_OFFLINE_KERNEL_SERVICE_SPECS,
    }

    resolved_records = []
    for address in sorted(set(baseline_guards) - set(projected_guards)):
        instruction = instructions.get(address)
        targets = list(verified.get(address, ()))
        proof: dict[str, Any] = {"kind": "offline-static-proof"}
        if instruction is not None and instruction.operands:
            operand = instruction.operands[0]
            if address in closed_vtable_targets:
                proof = {
                    "kind": "closed-static-vtable-corpus",
                    "slot": operand.displacement,
                    "slot_hex": f"0x{operand.displacement:02X}",
                    "rdata_start": PHASE7_OFFLINE_VTABLE_RDATA_START,
                    "rdata_end": PHASE7_OFFLINE_VTABLE_RDATA_END,
                    "slot_limit": PHASE7_OFFLINE_VTABLE_SLOT_LIMIT,
                    "target_count": len(closed_vtable_targets[address]),
                }
            elif operand.absolute in import_bindings:
                proof = {
                    "kind": "immutable-kernel-import-cell",
                    **import_bindings[int(operand.absolute)],
                }
            elif address in PHASE7_CAPTURED_REGISTER_IMPORT_BINDINGS:
                load, cell, target = PHASE7_CAPTURED_REGISTER_IMPORT_BINDINGS[address]
                binding = import_bindings.get(cell, {})
                proof = {
                    "kind": "callee-saved-kernel-import",
                    "load": load,
                    "cell": cell,
                    "target": target,
                    "name": binding.get("name"),
                }
        resolved_records.append(
            {
                "address": address,
                "address_hex": f"0x{address:08X}",
                "instruction": instruction.text() if instruction is not None else None,
                "targets": targets,
                "target_hex": [f"0x{target:08X}" for target in targets],
                "proof": proof,
            }
        )

    family_records: dict[tuple[str, str], list[int]] = {}
    remaining_records = []
    for address, reason in sorted(projected_guards.items()):
        instruction = instructions.get(address)
        category = "non-indirect-static-boundary"
        family = reason
        detail: dict[str, Any] = {}
        if instruction is not None and "indirect-target" in reason:
            category, family = _operand_family(instruction)
            operand = instruction.operands[0]
            if operand.absolute in import_bindings:
                binding = import_bindings[int(operand.absolute)]
                spec = service_specs.get(int(binding["target"]))
                category = (
                    "kernel-import-native-body-missing"
                    if spec is None or spec.get("normal_live_body") != "implemented-native32"
                    else "kernel-import-proof-gap"
                )
                family = str(binding["name"])
                detail = dict(binding)
        family_records.setdefault((category, family), []).append(address)
        remaining_records.append(
            {
                "address": address,
                "address_hex": f"0x{address:08X}",
                "reason": reason,
                "instruction": instruction.text() if instruction is not None else None,
                "category": category,
                "family": family,
                **detail,
            }
        )

    batches = [
        {
            "category": category,
            "family": family,
            "count": len(addresses),
            "addresses": addresses,
            "address_hex": [f"0x{address:08X}" for address in addresses],
        }
        for (category, family), addresses in sorted(
            family_records.items(), key=lambda item: (-len(item[1]), item[0])
        )
    ]
    added_addresses = sorted(set(projected_guards) - set(baseline_guards))
    net_guard_reduction = len(baseline_guards) - len(projected_guards)
    baseline_optimizations = baseline.get("ia32_runtime_optimizations", ())
    baseline_optimization_ids = [
        str(record["id"])
        for record in baseline_optimizations
        if isinstance(record, Mapping) and isinstance(record.get("id"), str)
    ]
    budget = _optimization_budget(
        max(net_guard_reduction, 0),
        baseline_optimization_ids,
        (
            candidate.get("ia32_runtime_optimizations", ())
            if candidate is not None
            else None
        ),
    )
    if not budget["passed"]:
        raise Ia32BackendError(
            "offline boundary resolution exceeded the IA-32 optimization budget: "
            f"net {net_guard_reduction}, credited {budget['credited_boundary_count']}"
        )
    return {
        "format": REPORT_FORMAT,
        "version": REPORT_VERSION,
        "mode": "offline-static-evidence",
        "launched_gameplay": False,
        "inputs": {
            "capsule": str(capsule_path.resolve()),
            "capsule_id": evidence.capsule_id,
            "xbe": str(xbe_path.resolve()),
            "decoded_block_store": str(decoded_block_store_path.resolve()),
            "coverage_profile": str(coverage_profile_path.resolve()),
            "baseline_manifest": str(baseline_path),
            "baseline_artifact_id": baseline["artifact_id"],
            "candidate_manifest": str(candidate_path) if candidate_path else None,
            "candidate_artifact_id": (
                candidate.get("artifact_id") if candidate is not None else None
            ),
        },
        "summary": {
            "baseline_guard_count": len(baseline_guards),
            "projected_guard_count": len(projected_guards),
            "resolved_guard_count": len(resolved_records),
            "added_guard_count": len(added_addresses),
            "net_guard_reduction": net_guard_reduction,
            "remaining_indirect_count": sum(
                "indirect-target" in reason for reason in projected_guards.values()
            ),
            "registered_service_count": len(services),
            "source_provenance_validated": True,
            "candidate_manifest_validated": candidate is not None,
            "optimization_budget_passed": True,
        },
        "optimization_budget": budget,
        "resolved_boundaries": resolved_records,
        "added_boundaries": [
            {
                "address": address,
                "address_hex": f"0x{address:08X}",
                "reason": projected_guards[address],
                "instruction": (
                    instructions[address].text() if address in instructions else None
                ),
            }
            for address in added_addresses
        ],
        "remaining_batches": batches,
        "remaining_boundaries": remaining_records,
    }


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capsule", type=Path)
    parser.add_argument("--xbe", type=Path, required=True)
    parser.add_argument("--decoded-block-store", type=Path, required=True)
    parser.add_argument("--coverage-profile", type=Path, required=True)
    parser.add_argument("--baseline-manifest", type=Path, required=True)
    parser.add_argument("--candidate-manifest", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = audit_ia32_boundaries(
            args.capsule,
            xbe_path=args.xbe,
            decoded_block_store_path=args.decoded_block_store,
            coverage_profile_path=args.coverage_profile,
            baseline_manifest_path=args.baseline_manifest,
            candidate_manifest_path=args.candidate_manifest,
        )
        _write_report(args.report, report)
    except (Ia32BackendError, OSError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(1, f"offline IA-32 boundary audit failed: {exc}\n")
    summary = report["summary"]
    print(
        f"{args.report}: resolved {summary['resolved_guard_count']} old guards offline; "
        f"added {summary['added_guard_count']}; net {summary['net_guard_reduction']}; "
        f"{summary['projected_guard_count']} remain; optimization budget PASS"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
