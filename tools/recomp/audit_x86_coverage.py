#!/usr/bin/env python3
"""Audit lifter coverage along control flow reachable from recovered XBE blocks."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, deque
from pathlib import Path
from typing import Any

from capstone import CS_ARCH_X86, CS_GRP_CALL, CS_GRP_IRET, CS_GRP_JUMP, CS_GRP_RET
from capstone import CS_MODE_32, Cs
from capstone.x86_const import X86_OP_IMM

try:
    from tools.loader.xbe_loader import load_xbe_file
    from tools.recomp.x86_lifter import X86Decoder
    from tools.xbe.xbe_info import parse_xbe_file
except ModuleNotFoundError:  # pragma: no cover - direct script fallback
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tools.loader.xbe_loader import load_xbe_file
    from tools.recomp.x86_lifter import X86Decoder
    from tools.xbe.xbe_info import parse_xbe_file


DEFAULT_MAX_BLOCKS = 65536
DEFAULT_MAX_BLOCK_BYTES = 4096


def _dynamic_block_cache_seed_addresses(path: Path) -> tuple[int, set[int]]:
    with path.open("rb") as source:
        prefix = source.read(16)
    if prefix.startswith(b"SQLite format 3"):
        connection = sqlite3.connect(str(path))
        try:
            rows = connection.execute(
                "SELECT target FROM decoded_blocks"
            ).fetchall()
        finally:
            connection.close()
        return len(rows), {int(row[0]) for row in rows}

    cache = json.loads(path.read_text(encoding="utf-8"))
    records = cache.get("records", {})
    return len(records), {
        int(record["base_address"])
        for record in records.values()
        if isinstance(record, dict) and "base_address" in record
    }


def audit_x86_coverage(
    xbe_path: Path,
    *,
    dynamic_block_cache_path: Path | None = None,
    seeds: list[int] | None = None,
    max_blocks: int = DEFAULT_MAX_BLOCKS,
    max_block_bytes: int = DEFAULT_MAX_BLOCK_BYTES,
) -> dict[str, Any]:
    info = parse_xbe_file(xbe_path)
    loaded = load_xbe_file(xbe_path)
    executable_ranges = [
        (section["virtual_address"], section["virtual_end"])
        for section in info["sections"]
        if "EXECUTABLE" in section["flag_names"]
    ]
    initial_seeds = set(seeds or [])
    cache_record_count = 0
    if dynamic_block_cache_path is not None and dynamic_block_cache_path.exists():
        cache_record_count, cache_seeds = _dynamic_block_cache_seed_addresses(
            dynamic_block_cache_path
        )
        initial_seeds.update(cache_seeds)

    def executable_range(address: int) -> tuple[int, int] | None:
        for start, end in executable_ranges:
            if start <= address < end:
                return start, end
        return None

    queue = deque(sorted(address for address in initial_seeds if executable_range(address)))
    queued = set(queue)
    visited_blocks: set[int] = set()
    visited_instructions: set[int] = set()
    gaps: dict[int, dict[str, Any]] = {}
    direct_targets: set[int] = set()
    truncated_blocks: list[int] = []
    capstone = Cs(CS_ARCH_X86, CS_MODE_32)
    capstone.detail = True
    decoder = X86Decoder()

    def enqueue(address: int) -> None:
        if address in queued or address in visited_blocks or executable_range(address) is None:
            return
        queued.add(address)
        queue.append(address)

    while queue and len(visited_blocks) < max_blocks:
        block_address = queue.popleft()
        if block_address in visited_blocks:
            continue
        visited_blocks.add(block_address)
        address_range = executable_range(block_address)
        if address_range is None:
            continue
        _, range_end = address_range
        read_size = min(max_block_bytes, range_end - block_address)
        code = loaded.arena.read(block_address, read_size)
        terminated = False
        for instruction in capstone.disasm(code, block_address):
            if instruction.address in visited_instructions:
                terminated = True
                break
            visited_instructions.add(instruction.address)
            try:
                lifted = decoder.decode_function(
                    bytes(instruction.bytes),
                    base_address=instruction.address,
                    symbol="coverage_audit",
                    max_instructions=1,
                )
                if lifted.code_size != instruction.size:
                    raise ValueError(
                        f"decoder consumed {lifted.code_size} bytes; Capstone consumed {instruction.size}"
                    )
            except Exception as exc:  # Coverage tool must continue past any decoder gap.
                gaps.setdefault(
                    instruction.address,
                    {
                        "address": instruction.address,
                        "address_hex": f"0x{instruction.address:08X}",
                        "bytes_hex": bytes(instruction.bytes).hex().upper(),
                        "mnemonic": instruction.mnemonic,
                        "operands": instruction.op_str,
                        "error": str(exc),
                    },
                )

            direct_target = None
            if instruction.operands and instruction.operands[0].type == X86_OP_IMM:
                direct_target = instruction.operands[0].imm & 0xFFFFFFFF
                direct_targets.add(direct_target)
            if instruction.group(CS_GRP_CALL):
                if direct_target is not None:
                    enqueue(direct_target)
                continue
            if instruction.group(CS_GRP_JUMP):
                if direct_target is not None:
                    enqueue(direct_target)
                if instruction.mnemonic != "jmp":
                    enqueue(instruction.address + instruction.size)
                terminated = True
                break
            if instruction.group(CS_GRP_RET) or instruction.group(CS_GRP_IRET):
                terminated = True
                break
            if instruction.mnemonic in {"int", "int1", "int3", "ud2", "hlt"}:
                terminated = True
                break
        if not terminated:
            truncated_blocks.append(block_address)

    mnemonic_counts = Counter(gap["mnemonic"] for gap in gaps.values())
    return {
        "format": "b2-recomp-x86-coverage-audit",
        "public_safe": False,
        "source": {"kind": "xbe", "name": xbe_path.name},
        "cache_record_count": cache_record_count,
        "seed_count": len(initial_seeds),
        "visited_block_count": len(visited_blocks),
        "visited_instruction_count": len(visited_instructions),
        "direct_target_count": len(direct_targets),
        "pending_block_count": len(queue),
        "max_blocks_reached": bool(queue),
        "truncated_block_count": len(truncated_blocks),
        "gap_count": len(gaps),
        "gap_mnemonic_counts": dict(sorted(mnemonic_counts.items())),
        "gaps": [gaps[address] for address in sorted(gaps)],
    }


def _parse_int(value: str) -> int:
    return int(value, 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xbe", type=Path)
    parser.add_argument("--dynamic-block-cache", type=Path)
    parser.add_argument("--seed", type=_parse_int, action="append", default=[])
    parser.add_argument("--max-blocks", type=int, default=DEFAULT_MAX_BLOCKS)
    parser.add_argument("--max-block-bytes", type=int, default=DEFAULT_MAX_BLOCK_BYTES)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    summary = audit_x86_coverage(
        args.xbe,
        dynamic_block_cache_path=args.dynamic_block_cache,
        seeds=args.seed,
        max_blocks=args.max_blocks,
        max_block_bytes=args.max_block_bytes,
    )
    output = json.dumps(summary, indent=2 if args.pretty else None, sort_keys=True)
    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
