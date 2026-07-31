#!/usr/bin/env python3
"""Create, validate, inspect, and restore deterministic local replay capsules."""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence, cast


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.recomp.x86_lifter import (
    CpuFlags,
    CpuState,
    LiftedFunction,
    Operand,
    SparseMemory,
    X86Instruction,
    lift_x86_function,
)


CAPSULE_FORMAT = "b2-recomp-replay-capsule"
CAPSULE_VERSION = 1
CAPSULE_SUFFIX = ".b2rcap"
PAGE_SIZE = 4096
LOCAL_CAPSULE_ROOTS = (
    REPO_ROOT / "reports" / "local",
    REPO_ROOT / "data" / "local",
)
SYNTHETIC_CAPSULE_ROOT = REPO_ROOT / "build" / "local"


class ReplayCapsuleError(RuntimeError):
    """Raised when a replay capsule is malformed or violates local policy."""


@dataclass(frozen=True)
class ReplayCapsule:
    path: Path
    manifest: dict[str, Any]
    state: CpuState
    memory: SparseMemory
    functions: tuple[LiftedFunction, ...]
    scheduler_state: dict[str, Any]
    service_state: dict[str, Any]
    events: dict[str, list[Any]]
    resources: dict[str, bytes]

    @property
    def capsule_id(self) -> str:
        return str(self.manifest["content_sha256"])

    def combined_function(self) -> LiftedFunction:
        instructions = tuple(
            instruction for function in self.functions for instruction in function.instructions
        )
        if not instructions:
            raise ReplayCapsuleError("capsule contains no decoded instructions")
        ordered = tuple(sorted(instructions, key=lambda item: item.address))
        return LiftedFunction(
            symbol=f"capsule_{self.capsule_id[:16]}",
            base_address=ordered[0].address,
            code_size=ordered[-1].next_address - ordered[0].address,
            instructions=ordered,
        )

    def summary(self) -> dict[str, Any]:
        return {
            "format": CAPSULE_FORMAT,
            "version": CAPSULE_VERSION,
            "path": str(self.path),
            "capsule_id": self.capsule_id,
            "capture_kind": self.manifest["capture_kind"],
            "entry_eip": self.state.eip,
            "entry_eip_hex": f"0x{self.state.eip:08X}",
            "function_count": len(self.functions),
            "instruction_count": sum(function.instruction_count for function in self.functions),
            "memory_page_count": len(self.memory.export_pages()),
            "resource_count": len(self.resources),
            "event_counts": {name: len(records) for name, records in sorted(self.events.items())},
            "provenance": self.manifest["provenance"],
        }


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _float_bits(value: float) -> str:
    return struct.pack("<d", float(value)).hex()


def _float_from_bits(value: object) -> float:
    if not isinstance(value, str):
        raise ReplayCapsuleError("floating-point state is not a bit string")
    try:
        payload = bytes.fromhex(value)
    except ValueError as exc:
        raise ReplayCapsuleError("floating-point state is not hexadecimal") from exc
    if len(payload) != 8:
        raise ReplayCapsuleError("floating-point state must contain eight bytes")
    return float(struct.unpack("<d", payload)[0])


def cpu_state_record(state: CpuState) -> dict[str, Any]:
    return {
        "registers": {name: int(value) & 0xFFFFFFFF for name, value in state.registers.items()},
        "flags": state.flags.to_dict(),
        "eip": int(state.eip) & 0xFFFFFFFF,
        "fs_base": int(state.fs_base) & 0xFFFFFFFF,
        "cs_selector": int(state.cs_selector) & 0xFFFF,
        "gdtr_base": int(state.gdtr_base) & 0xFFFFFFFF,
        "gdtr_limit": int(state.gdtr_limit) & 0xFFFF,
        "timestamp_counter": int(state.timestamp_counter) & 0xFFFFFFFFFFFFFFFF,
        "mxcsr": int(state.mxcsr) & 0xFFFFFFFF,
        "fpu_control_word": int(state.fpu_control_word) & 0xFFFF,
        "fpu_status_word": int(state.fpu_status_word) & 0xFFFF,
        "fpu_stack_bits": [_float_bits(value) for value in state.fpu_stack],
        "xmm_register_bits": {
            name: [_float_bits(value) for value in lanes]
            for name, lanes in sorted(state.xmm_registers.items())
        },
        "mmx_registers": {
            name: int(value) & 0xFFFFFFFFFFFFFFFF
            for name, value in sorted(state.mmx_registers.items())
        },
    }


def cpu_state_from_record(record: Mapping[str, Any]) -> CpuState:
    registers = record.get("registers")
    flags = record.get("flags")
    if not isinstance(registers, dict) or not isinstance(flags, dict):
        raise ReplayCapsuleError("capsule CPU state is missing registers or flags")
    state = CpuState()
    for name in state.registers:
        value = registers.get(name)
        if not isinstance(value, int):
            raise ReplayCapsuleError(f"capsule CPU register {name} is invalid")
        state.set_register(name, value)
    state.flags = CpuFlags(
        **{
            name: bool(flags.get(name, getattr(state.flags, name)))
            for name in state.flags.to_dict()
        }
    )
    integer_fields = (
        "eip",
        "fs_base",
        "cs_selector",
        "gdtr_base",
        "gdtr_limit",
        "timestamp_counter",
        "mxcsr",
        "fpu_control_word",
        "fpu_status_word",
    )
    for name in integer_fields:
        value = record.get(name)
        if not isinstance(value, int):
            raise ReplayCapsuleError(f"capsule CPU field {name} is invalid")
        setattr(state, name, value)
    fpu_stack = record.get("fpu_stack_bits", [])
    xmm_registers = record.get("xmm_register_bits", {})
    mmx_registers = record.get("mmx_registers", {})
    if not isinstance(fpu_stack, list) or not isinstance(xmm_registers, dict):
        raise ReplayCapsuleError("capsule floating-point state is invalid")
    if not isinstance(mmx_registers, dict):
        raise ReplayCapsuleError("capsule MMX state is invalid")
    state.fpu_stack = [_float_from_bits(value) for value in fpu_stack]
    for name in state.xmm_registers:
        lanes = xmm_registers.get(name)
        if not isinstance(lanes, list) or len(lanes) != 4:
            raise ReplayCapsuleError(f"capsule XMM register {name} is invalid")
        state.set_xmm_register(name, (_float_from_bits(value) for value in lanes))
    for name in state.mmx_registers:
        value = mmx_registers.get(name)
        if not isinstance(value, int):
            raise ReplayCapsuleError(f"capsule MMX register {name} is invalid")
        state.set_mmx_register(name, value)
    return state


def lifted_function_record(function: LiftedFunction) -> dict[str, Any]:
    return cast(dict[str, Any], function.to_dict(include_bytes=True))


def lifted_function_from_record(record: Mapping[str, Any]) -> LiftedFunction:
    instructions_value = record.get("instructions")
    if not isinstance(instructions_value, list):
        raise ReplayCapsuleError("capsule function instructions are invalid")
    instructions: list[X86Instruction] = []
    for value in instructions_value:
        if not isinstance(value, dict):
            raise ReplayCapsuleError("capsule instruction is invalid")
        operands_value = value.get("operands", [])
        if not isinstance(operands_value, list):
            raise ReplayCapsuleError("capsule instruction operands are invalid")
        operands = tuple(
            Operand(
                kind=str(operand["kind"]),
                size=int(operand.get("size", 32)),
                reg=str(operand["reg"]) if operand.get("reg") is not None else None,
                immediate=int(operand["immediate"])
                if operand.get("immediate") is not None
                else None,
                base=str(operand["base"]) if operand.get("base") is not None else None,
                index=str(operand["index"]) if operand.get("index") is not None else None,
                scale=int(operand.get("scale", 1)),
                displacement=int(operand.get("displacement", 0)),
                absolute=int(operand["absolute"]) if operand.get("absolute") is not None else None,
                segment=str(operand["segment"]) if operand.get("segment") is not None else None,
            )
            for operand in operands_value
            if isinstance(operand, dict)
        )
        instructions.append(
            X86Instruction(
                address=int(value["address"]),
                size=int(value["size"]),
                mnemonic=str(value["mnemonic"]),
                operands=operands,
                bytes_hex=str(value.get("bytes_hex", "")),
                target=int(value["target"]) if value.get("target") is not None else None,
                condition=str(value["condition"]) if value.get("condition") is not None else None,
                ret_stack_adjust=int(value.get("ret_stack_adjust", 0)),
            )
        )
    return LiftedFunction(
        symbol=str(record["symbol"]),
        base_address=int(record["base_address"]),
        code_size=int(record["code_size"]),
        instructions=tuple(instructions),
        target_platform=str(record.get("target_platform", "windows-x86")),
        generated_language=str(record.get("generated_language", "c++17")),
        renderer_backend=str(record.get("renderer_backend", "vulkan")),
    )


def _resource_entry_name(name: str) -> str:
    path = PurePosixPath(name.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ReplayCapsuleError(f"unsafe capsule resource name: {name}")
    return str(PurePosixPath("resources", *path.parts))


def _write_zip_entry(bundle: zipfile.ZipFile, name: str, payload: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    bundle.writestr(info, payload, compresslevel=9)


def capture_replay_capsule(
    output: Path,
    *,
    state: CpuState,
    memory: SparseMemory,
    functions: Iterable[LiftedFunction],
    scheduler_state: Mapping[str, Any],
    service_state: Mapping[str, Any],
    provenance: Mapping[str, Any],
    resources: Mapping[str, bytes | Path] | None = None,
    events: Mapping[str, Sequence[Any]] | None = None,
    capture_kind: str = "manual",
) -> Path:
    """Write one content-addressed checkpoint; callers choose the manual boundary."""

    if capture_kind not in {"manual", "synthetic", "divergence"}:
        raise ReplayCapsuleError(f"unsupported capsule capture kind: {capture_kind}")
    if capture_kind == "manual" and provenance.get("manual_capture") is not True:
        raise ReplayCapsuleError("manual capsules must declare manual_capture=true")
    captured_functions = tuple(functions)
    if not captured_functions:
        raise ReplayCapsuleError("replay capsule needs at least one decoded function")
    decoded_addresses: set[int] = set()
    for function in captured_functions:
        for instruction in function.instructions:
            if instruction.address in decoded_addresses:
                raise ReplayCapsuleError(
                    "duplicate decoded guest address in capsule: "
                    f"0x{instruction.address:08X}"
                )
            decoded_addresses.add(instruction.address)
    entries: dict[str, bytes] = {}
    page_records = []
    for page_number, payload in memory.export_pages():
        name = f"memory/page-{page_number:05X}.bin"
        entries[name] = payload
        page_records.append(
            {
                "page_index": page_number,
                "address": page_number * PAGE_SIZE,
                "entry": name,
                "bytes": len(payload),
                "sha256": _sha256(payload),
            }
        )
    resource_records = []
    for name, value in sorted((resources or {}).items()):
        entry_name = _resource_entry_name(name)
        if entry_name in entries:
            raise ReplayCapsuleError(
                f"duplicate normalized capsule resource name: {entry_name}"
            )
        payload = value.read_bytes() if isinstance(value, Path) else bytes(value)
        entries[entry_name] = payload
        resource_records.append(
            {
                "name": name,
                "entry": entry_name,
                "bytes": len(payload),
                "sha256": _sha256(payload),
            }
        )
    event_records = {str(name): list(records) for name, records in sorted((events or {}).items())}
    core = {
        "format": CAPSULE_FORMAT,
        "version": CAPSULE_VERSION,
        "capture_kind": capture_kind,
        "cpu_state": cpu_state_record(state),
        "memory_pages": page_records,
        "program": [
            lifted_function_record(function) for function in captured_functions
        ],
        "scheduler_state": dict(scheduler_state),
        "service_state": dict(service_state),
        "events": event_records,
        "resources": resource_records,
        "provenance": dict(provenance),
    }
    digest = hashlib.sha256()
    digest.update(_canonical_json(core))
    for name in sorted(entries):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(entries[name])
    manifest = {**core, "content_sha256": digest.hexdigest()}
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f"{output.name}.tmp")
    with zipfile.ZipFile(temporary, "w") as bundle:
        _write_zip_entry(bundle, "manifest.json", _canonical_json(manifest))
        for name in sorted(entries):
            _write_zip_entry(bundle, name, entries[name])
    temporary.replace(output)
    return output


def load_replay_capsule(path: Path) -> ReplayCapsule:
    try:
        with zipfile.ZipFile(path) as bundle:
            archive_names = bundle.namelist()
            if len(archive_names) != len(set(archive_names)):
                raise ReplayCapsuleError("capsule contains duplicate archive members")
            manifest_value = json.loads(bundle.read("manifest.json"))
            if not isinstance(manifest_value, dict):
                raise ReplayCapsuleError("capsule manifest is not an object")
            manifest = manifest_value
            if (
                manifest.get("format") != CAPSULE_FORMAT
                or manifest.get("version") != CAPSULE_VERSION
            ):
                raise ReplayCapsuleError("unsupported replay capsule format")
            page_payloads = []
            entries_for_digest: dict[str, bytes] = {}
            for record in manifest.get("memory_pages", []):
                if not isinstance(record, dict):
                    raise ReplayCapsuleError("capsule memory record is invalid")
                payload = bundle.read(str(record["entry"]))
                if len(payload) != PAGE_SIZE or _sha256(payload) != record.get("sha256"):
                    raise ReplayCapsuleError("capsule memory page failed validation")
                page_payloads.append((int(record["page_index"]), payload))
                entries_for_digest[str(record["entry"])] = payload
            resources: dict[str, bytes] = {}
            for record in manifest.get("resources", []):
                if not isinstance(record, dict):
                    raise ReplayCapsuleError("capsule resource record is invalid")
                payload = bundle.read(str(record["entry"]))
                if len(payload) != int(record["bytes"]) or _sha256(payload) != record.get("sha256"):
                    raise ReplayCapsuleError("capsule resource failed validation")
                resources[str(record["name"])] = payload
                entries_for_digest[str(record["entry"])] = payload
            expected_names = {"manifest.json", *entries_for_digest}
            if set(archive_names) != expected_names:
                raise ReplayCapsuleError("capsule contains undeclared archive members")
    except (OSError, KeyError, ValueError, zipfile.BadZipFile) as exc:
        raise ReplayCapsuleError(f"could not load replay capsule {path}: {exc}") from exc
    digest_manifest = dict(manifest)
    expected_digest = str(digest_manifest.pop("content_sha256", ""))
    digest = hashlib.sha256()
    digest.update(_canonical_json(digest_manifest))
    for name in sorted(entries_for_digest):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(entries_for_digest[name])
    if digest.hexdigest() != expected_digest:
        raise ReplayCapsuleError("capsule content identity does not match its payload")
    cpu_value = manifest.get("cpu_state")
    program_value = manifest.get("program")
    if not isinstance(cpu_value, dict) or not isinstance(program_value, list):
        raise ReplayCapsuleError("capsule CPU/program record is invalid")
    functions = tuple(
        lifted_function_from_record(value) for value in program_value if isinstance(value, dict)
    )
    if len(functions) != len(program_value):
        raise ReplayCapsuleError("capsule function record is invalid")
    scheduler = manifest.get("scheduler_state", {})
    services = manifest.get("service_state", {})
    events_value = manifest.get("events", {})
    if not isinstance(scheduler, dict) or not isinstance(services, dict):
        raise ReplayCapsuleError("capsule scheduler/service state is invalid")
    if not isinstance(events_value, dict) or not all(
        isinstance(value, list) for value in events_value.values()
    ):
        raise ReplayCapsuleError("capsule event streams are invalid")
    return ReplayCapsule(
        path=path.resolve(),
        manifest=manifest,
        state=cpu_state_from_record(cpu_value),
        memory=SparseMemory.from_pages(page_payloads),
        functions=functions,
        scheduler_state=scheduler,
        service_state=services,
        events={str(name): list(value) for name, value in events_value.items()},
        resources=resources,
    )


def clone_capsule_state(capsule: ReplayCapsule) -> tuple[CpuState, SparseMemory]:
    return (
        cpu_state_from_record(cpu_state_record(capsule.state)),
        SparseMemory.from_pages(capsule.memory.export_pages()),
    )


def build_synthetic_capsule(output: Path) -> Path:
    function = lift_x86_function(
        bytes.fromhex("40890500200000C3"),
        base_address=0x1000,
        symbol="synthetic_capsule_increment",
    )
    state = CpuState.with_registers(eax=41, esp=0x8000)
    state.eip = function.base_address
    memory = SparseMemory({0x8000: 0, 0x2000: 0})
    return capture_replay_capsule(
        output,
        state=state,
        memory=memory,
        functions=(function,),
        scheduler_state={"primary_lane": "runnable", "slice": 0},
        service_state={"sequence": 0, "current_service": None},
        resources={"render/typed-draw.json": _canonical_json({"draw": 1})},
        events={
            "service_sequence": [],
            "render_events": [{"kind": "typed_draw", "sequence": 1}],
            "audio_events": [],
        },
        provenance={
            "synthetic": True,
            "manual_capture": False,
            "source": "asset-free-ci",
            "target_sha256": "0" * 64,
        },
        capture_kind="synthetic",
    )


def require_local_capsule_output(path: Path, *, synthetic: bool) -> None:
    resolved = path.resolve()
    if resolved.suffix.casefold() != CAPSULE_SUFFIX:
        raise ReplayCapsuleError(
            f"replay capsule output must use {CAPSULE_SUFFIX}: {resolved}"
        )
    roots = (*LOCAL_CAPSULE_ROOTS, SYNTHETIC_CAPSULE_ROOT) if synthetic else LOCAL_CAPSULE_ROOTS
    if not any(resolved == root.resolve() or root.resolve() in resolved.parents for root in roots):
        allowed = "reports/local or data/local" + (" or build/local" if synthetic else "")
        raise ReplayCapsuleError(f"capsule output must stay under {allowed}: {resolved}")


def _capture_from_spec(spec_path: Path, output: Path) -> Path:
    value = json.loads(spec_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ReplayCapsuleError("capsule capture specification is not an object")
    cpu_value = value.get("cpu_state")
    functions_value = value.get("program")
    pages_value = value.get("memory_pages", [])
    if not isinstance(cpu_value, dict) or not isinstance(functions_value, list):
        raise ReplayCapsuleError("capture specification needs cpu_state and program")
    if not all(isinstance(record, dict) for record in functions_value):
        raise ReplayCapsuleError("capture specification has an invalid function record")
    if not isinstance(pages_value, list):
        raise ReplayCapsuleError("capture specification memory_pages is not a list")
    pages = []
    for record in pages_value:
        if not isinstance(record, dict):
            raise ReplayCapsuleError("capture memory page is invalid")
        pages.append((int(record["page_index"]), bytes.fromhex(str(record["data_hex"]))))
    resources_value = value.get("resources", [])
    if not isinstance(resources_value, list) or not all(
        isinstance(record, dict)
        and isinstance(record.get("name"), str)
        and isinstance(record.get("path"), str)
        for record in resources_value
    ):
        raise ReplayCapsuleError("capture specification resources are invalid")
    resources = {
        str(record["name"]): Path(str(record["path"]))
        for record in resources_value
    }
    scheduler_state = value.get("scheduler_state", {})
    service_state = value.get("service_state", {})
    provenance = value.get("provenance", {})
    events = value.get("events", {})
    if not all(
        isinstance(record, dict)
        for record in (scheduler_state, service_state, provenance, events)
    ):
        raise ReplayCapsuleError("capture specification state/provenance is invalid")
    if not all(isinstance(records, list) for records in events.values()):
        raise ReplayCapsuleError("capture specification event streams are invalid")
    return capture_replay_capsule(
        output,
        state=cpu_state_from_record(cpu_value),
        memory=SparseMemory.from_pages(pages),
        functions=(lifted_function_from_record(record) for record in functions_value),
        scheduler_state=scheduler_state,
        service_state=service_state,
        provenance=provenance,
        resources=resources,
        events=events,
        capture_kind="manual",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    synthetic = subparsers.add_parser("synthetic", help="Write the asset-free CI capsule.")
    synthetic.add_argument("--output", type=Path, required=True)
    capture = subparsers.add_parser("capture", help="Pack a manually produced checkpoint spec.")
    capture.add_argument("--spec", type=Path, required=True)
    capture.add_argument("--output", type=Path, required=True)
    inspect = subparsers.add_parser("inspect", help="Validate and summarize a capsule.")
    inspect.add_argument("capsule", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "synthetic":
            require_local_capsule_output(args.output, synthetic=True)
            result = build_synthetic_capsule(args.output)
            print(result)
        elif args.command == "capture":
            require_local_capsule_output(args.output, synthetic=False)
            result = _capture_from_spec(args.spec, args.output)
            print(result)
        else:
            print(json.dumps(load_replay_capsule(args.capsule).summary(), indent=2, sort_keys=True))
        return 0
    except (ReplayCapsuleError, OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    sys.exit(main())
