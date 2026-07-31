#!/usr/bin/env python3
"""Build and query stable guest-to-native debug metadata for AOT modules."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.recomp.x86_lifter import LiftedFunction, X86Instruction


DEBUG_METADATA_FORMAT = "b2-recomp-native-debug-metadata"
DEBUG_INDEX_FORMAT = "b2-recomp-native-debug-index"
DEBUG_METADATA_VERSION = 1
CASE_PATTERN = re.compile(r"^\s*case\s+(0x[0-9A-Fa-f]{8})u:\s*(?:\{\s*)?$")
FUNCTION_PATTERN = re.compile(
    r"^(?:static __declspec\(noinline\) uint32_t|extern \"C\" "
    r"__declspec\(dllexport\) uint32_t)\s+([A-Za-z_][A-Za-z0-9_]*)\("
)


class NativeDebugMetadataError(RuntimeError):
    """Raised when generated-source metadata cannot be produced or resolved."""


def guest_address_ranges(function: LiftedFunction) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    for instruction in sorted(function.instructions, key=lambda item: item.address):
        if ranges and ranges[-1][1] == instruction.address:
            ranges[-1] = (ranges[-1][0], instruction.next_address)
        else:
            ranges.append((instruction.address, instruction.next_address))
    return tuple(ranges)


def _write_json_if_changed(path: Path, value: object) -> None:
    payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if path.is_file() and path.read_text(encoding="utf-8") == payload:
        return
    path.write_text(payload, encoding="utf-8", newline="\n")


def _artifact_name(path: Path | None) -> str | None:
    return path.name if path is not None else None


def _case_end_line(source_lines: Sequence[str], source_line: int) -> int:
    depth = 0
    found_brace = False
    for index in range(source_line - 1, len(source_lines)):
        line = source_lines[index]
        if "{" in line:
            found_brace = True
        depth += line.count("{") - line.count("}")
        if found_brace and depth == 0:
            return index + 1
        if index > source_line - 1 and CASE_PATTERN.match(line):
            return index
    return len(source_lines)


def _decoded_block_ranges(function: LiftedFunction) -> dict[int, tuple[int, int]]:
    instructions = sorted(function.instructions, key=lambda item: item.address)
    if not instructions:
        return {}
    starters = {instructions[0].address}
    addresses = {instruction.address for instruction in instructions}
    for index, instruction in enumerate(instructions):
        if instruction.target in addresses:
            starters.add(int(instruction.target))
        control_transfer = (
            instruction.mnemonic == "call"
            or instruction.mnemonic.startswith("j")
            or instruction.mnemonic.startswith("loop")
            or instruction.mnemonic in {"ret", "int", "int3"}
        )
        if control_transfer and index + 1 < len(instructions):
            starters.add(instructions[index + 1].address)
        if index and instructions[index - 1].next_address != instruction.address:
            starters.add(instruction.address)
    ranges: dict[int, tuple[int, int]] = {}
    current: list[X86Instruction] = []
    for instruction in instructions:
        if current and instruction.address in starters:
            start = current[0].address
            end = current[-1].next_address
            ranges.update((item.address, (start, end)) for item in current)
            current = []
        current.append(instruction)
    if current:
        start = current[0].address
        end = current[-1].next_address
        ranges.update((item.address, (start, end)) for item in current)
    return ranges


def build_module_debug_metadata(
    function: LiftedFunction,
    source: str,
    *,
    source_path: Path,
    object_path: Path,
    artifact_path: Path,
    pdb_path: Path | None,
    native_symbol: str,
    configuration_key: str,
    content_digest: str,
) -> dict[str, Any]:
    """Map every decoded guest instruction to its generated case and artifacts."""

    source_lines = source.splitlines()
    cases: dict[int, int] = {}
    generated_function_by_line: dict[int, str] = {}
    active_function = native_symbol
    for line_number, line in enumerate(source_lines, start=1):
        function_match = FUNCTION_PATTERN.match(line)
        if function_match:
            active_function = function_match.group(1)
        case_match = CASE_PATTERN.match(line)
        if case_match:
            address = int(case_match.group(1), 16)
            cases[address] = line_number
            generated_function_by_line[line_number] = active_function
    instruction_by_address = {
        instruction.address: instruction for instruction in function.instructions
    }
    missing = sorted(set(instruction_by_address) - set(cases))
    if missing:
        raise NativeDebugMetadataError(
            f"generated source has no case for guest address 0x{missing[0]:08X}"
        )
    block_ranges = _decoded_block_ranges(function)
    entries = []
    for address in sorted(instruction_by_address):
        instruction = instruction_by_address[address]
        source_line = cases[address]
        source_end_line = _case_end_line(source_lines, source_line)
        block_start, block_end = block_ranges[address]
        entries.append(
            {
                "guest_address": address,
                "guest_address_hex": f"0x{address:08X}",
                "decoded_block": {
                    "start": block_start,
                    "start_hex": f"0x{block_start:08X}",
                    "end": block_end,
                    "end_hex": f"0x{block_end:08X}",
                },
                "generated_function": generated_function_by_line[source_line],
                "generated_ir": instruction.to_dict(include_bytes=True),
                "native_symbol": native_symbol,
                "source": source_path.name,
                "source_line": source_line,
                "source_end_line": source_end_line,
                "object": object_path.name,
                "artifact": artifact_path.name,
                "pdb": _artifact_name(pdb_path),
            }
        )
    return {
        "format": DEBUG_METADATA_FORMAT,
        "version": DEBUG_METADATA_VERSION,
        "module": function.symbol,
        "partition_start": function.base_address,
        "partition_end": function.base_address + function.code_size,
        "configuration_key": configuration_key,
        "content_digest": content_digest,
        "native_symbol": native_symbol,
        "address_ranges": [
            {"start": start, "end": end}
            for start, end in guest_address_ranges(function)
        ],
        "source": source_path.name,
        "object": object_path.name,
        "artifact": artifact_path.name,
        "pdb": _artifact_name(pdb_path),
        "entries": entries,
    }


def write_module_debug_metadata(path: Path, metadata: Mapping[str, Any]) -> Path:
    payload = json.dumps(metadata, separators=(",", ":"), sort_keys=True) + "\n"
    if not path.is_file() or path.read_text(encoding="utf-8") != payload:
        path.write_text(payload, encoding="utf-8", newline="\n")
    return path


def write_native_debug_index(
    build_dir: Path,
    module_metadata: Iterable[tuple[Path, Mapping[str, Any]]],
) -> Path:
    modules: list[dict[str, Any]] = []
    for metadata_path, metadata in module_metadata:
        modules.append(
            {
                "metadata": metadata_path.name,
                "partition_start": int(metadata["partition_start"]),
                "partition_end": int(metadata["partition_end"]),
                "native_symbol": str(metadata["native_symbol"]),
                "address_ranges": list(metadata.get("address_ranges", ())),
                "source": str(metadata["source"]),
                "object": str(metadata["object"]),
                "artifact": str(metadata["artifact"]),
                "pdb": metadata.get("pdb"),
                "entry_count": int(
                    metadata.get("entry_count", len(metadata.get("entries", ())))
                ),
            }
        )
    modules.sort(key=lambda value: int(value["partition_start"]))
    owned_ranges = sorted(
        (
            int(address_range["start"]),
            int(address_range["end"]),
            str(module["metadata"]),
        )
        for module in modules
        for address_range in module["address_ranges"]
        if isinstance(address_range, dict)
    )
    for previous, current in zip(owned_ranges, owned_ranges[1:]):
        if previous[1] > current[0]:
            raise NativeDebugMetadataError(
                "overlapping native debug partitions at "
                f"0x{current[0]:08X} in {previous[2]} and {current[2]}"
            )
    index = {
        "format": DEBUG_INDEX_FORMAT,
        "version": DEBUG_METADATA_VERSION,
        "modules": modules,
    }
    path = build_dir / "native-debug-index.json"
    _write_json_if_changed(path, index)
    return path


def load_native_debug_index(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("format") != DEBUG_INDEX_FORMAT:
        raise NativeDebugMetadataError(f"not a native debug index: {path}")
    if value.get("version") != DEBUG_METADATA_VERSION:
        raise NativeDebugMetadataError(f"unsupported native debug index: {path}")
    value["_index_path"] = str(path.resolve())
    return value


def lookup_guest_address(index: Mapping[str, Any], guest_address: int) -> dict[str, Any] | None:
    # Early version-one development indexes embedded every entry. Keep lookup
    # compatibility while current indexes remain proportional to module count.
    for entry in index.get("entries", []):
        if isinstance(entry, dict) and int(entry.get("guest_address", -1)) == guest_address:
            return dict(entry)
    index_path_value = index.get("_index_path")
    if not isinstance(index_path_value, str):
        return None
    index_path = Path(index_path_value)
    for module in index.get("modules", []):
        if not isinstance(module, dict):
            continue
        owns_address = any(
            isinstance(address_range, dict)
            and int(address_range.get("start", -1))
            <= guest_address
            < int(address_range.get("end", -1))
            for address_range in module.get("address_ranges", [])
        )
        if not owns_address:
            continue
        metadata_path = index_path.parent / str(module["metadata"])
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            raise NativeDebugMetadataError(
                f"invalid native module metadata: {metadata_path}"
            )
        for entry in metadata.get("entries", []):
            if (
                isinstance(entry, dict)
                and int(entry.get("guest_address", -1)) == guest_address
            ):
                return {**entry, "metadata": metadata_path.name}
        return None
    return None


def generated_body(
    index_path: Path,
    entry: Mapping[str, Any],
    *,
    context_lines: int = 0,
) -> str:
    source_path = index_path.parent / str(entry["source"])
    lines = source_path.read_text(encoding="utf-8").splitlines()
    start = max(1, int(entry["source_line"]) - context_lines)
    end = min(len(lines), int(entry["source_end_line"]) + context_lines)
    return "\n".join(
        f"{line_number:7d}  {lines[line_number - 1]}" for line_number in range(start, end + 1)
    )


def _parse_address(value: str) -> int:
    try:
        return int(value, 0) & 0xFFFFFFFF
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid guest address: {value}") from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("address", type=_parse_address)
    parser.add_argument(
        "--build-dir",
        type=Path,
        default=REPO_ROOT / "build" / "native-guest-loop",
    )
    parser.add_argument("--context", type=int, default=0)
    parser.add_argument("--metadata-only", action="store_true")
    args = parser.parse_args(argv)
    index_path = args.build_dir / "native-debug-index.json"
    try:
        index = load_native_debug_index(index_path)
        entry = lookup_guest_address(index, args.address)
        if entry is None:
            parser.error(f"guest address 0x{args.address:08X} is not in {index_path}")
        print(json.dumps(entry, indent=2, sort_keys=True))
        if not args.metadata_only:
            print()
            print(generated_body(index_path, entry, context_lines=max(0, args.context)))
        return 0
    except (OSError, ValueError, NativeDebugMetadataError, json.JSONDecodeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    sys.exit(main())
