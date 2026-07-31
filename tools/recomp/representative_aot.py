#!/usr/bin/env python3
"""Compile a small asset-free AOT corpus for emitter iteration."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable, Sequence


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.recomp.native_executor import NativeResumableExecutor
from tools.recomp.x86_lifter import LiftedFunction, lift_x86_function


DEFAULT_BUILD_DIRECTORY = REPO_ROOT / "build" / "local" / "representative-aot"


def build_synthetic_hot_corpus() -> tuple[LiftedFunction, ...]:
    """Cover control flow, memory/flags, x87, SSE, and host-boundary emission."""

    specifications = (
        (
            "integer_memory_flags",
            0x1000,
            "558BEC83EC048B450803450C8945FC83F8077505908B45FCC9C20800",
        ),
        (
            "packed_sse",
            0x3000,
            "0F2805002000000FC6C0550F5905102000000F580520200000"
            "0F290530200000C3",
        ),
        (
            "x87_stack",
            0x5000,
            "D90500200000D90504200000DEC1D91D08200000C3",
        ),
        (
            "system_and_string",
            0x7000,
            "0F31F3A5C3",
        ),
    )
    return tuple(
        lift_x86_function(bytes.fromhex(code), base_address=base, symbol=name)
        for name, base, code in specifications
    )


def select_representative_functions(
    functions: Iterable[LiftedFunction],
    *,
    hot_addresses: Iterable[int] = (),
    instruction_budget: int = 12_000,
) -> tuple[LiftedFunction, ...]:
    """Choose deterministic hot-first title functions under an iteration budget."""

    hot = {int(address) & 0xFFFFFFFF for address in hot_addresses}
    candidates = tuple(functions)

    def score(function: LiftedFunction) -> tuple[int, int, int]:
        addresses = {instruction.address for instruction in function.instructions}
        hot_hits = len(addresses & hot)
        semantic_families = len(
            {
                instruction.mnemonic.split("_", 1)[0]
                for instruction in function.instructions
            }
        )
        return (-hot_hits, -semantic_families, function.base_address)

    selected: list[LiftedFunction] = []
    instruction_count = 0
    for function in sorted(candidates, key=score):
        size = max(1, function.instruction_count)
        if selected and instruction_count + size > max(1, instruction_budget):
            continue
        selected.append(function)
        instruction_count += size
        if instruction_count >= max(1, instruction_budget):
            break
    return tuple(selected)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, default=DEFAULT_BUILD_DIRECTORY)
    parser.add_argument(
        "--compiler-cache",
        choices=("auto", "required", "off"),
        default="auto",
    )
    parser.add_argument("--instruction-budget", type=int, default=12_000)
    args = parser.parse_args(argv)
    if args.instruction_budget <= 0:
        parser.error("--instruction-budget must be positive")
    corpus = select_representative_functions(
        build_synthetic_hot_corpus(),
        hot_addresses=(0x1000, 0x3000, 0x5000, 0x7000),
        instruction_budget=args.instruction_budget,
    )
    executor = NativeResumableExecutor(
        corpus[0],
        build_dir=args.build_dir,
        module_functions=corpus,
        aot_optimization_mode="combined",
        compiler_cache_mode=args.compiler_cache,
    )
    report = {
        "format": "b2-recomp-representative-aot-v1",
        "corpus": "asset-free-hot-semantics-v1",
        "function_count": len(corpus),
        "instruction_count": sum(item.instruction_count for item in corpus),
        "cache": executor.cache_summary,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
