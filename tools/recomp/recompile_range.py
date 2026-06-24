#!/usr/bin/env python3
"""Lift a narrow x86 range and emit deterministic C++ prototype code."""

from __future__ import annotations

import argparse
from pathlib import Path

try:
    from tools.recomp.x86_lifter import (
        build_recompilation_summary,
        emit_cpp,
        lift_x86_function,
        lift_xbe_range,
        summary_json,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script execution fallback
    import sys

    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from tools.recomp.x86_lifter import (
        build_recompilation_summary,
        emit_cpp,
        lift_x86_function,
        lift_xbe_range,
        summary_json,
    )


def _parse_int(value: str) -> int:
    return int(value, 0)


def _parse_hex_bytes(value: str) -> bytes:
    clean = "".join(char for char in value if not char.isspace() and char != "_")
    if len(clean) % 2:
        raise argparse.ArgumentTypeError("hex byte input must contain whole bytes")
    try:
        return bytes.fromhex(clean)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Lift a small IA-32 range and emit deterministic C++17."
    )
    parser.add_argument(
        "xbe",
        nargs="?",
        type=Path,
        help="Local XBE path. Omit when using --hex.",
    )
    parser.add_argument(
        "--hex",
        dest="hex_bytes",
        type=_parse_hex_bytes,
        help="Synthetic bytes to lift instead of reading an XBE.",
    )
    parser.add_argument(
        "--virtual-address",
        type=_parse_int,
        default=0x1000,
        help="Guest virtual address for the first byte.",
    )
    parser.add_argument(
        "--size",
        type=_parse_int,
        help="Number of bytes to read from the XBE range.",
    )
    parser.add_argument(
        "--symbol",
        default="b2r_lifted_function",
        help="Generated function symbol.",
    )
    parser.add_argument(
        "--max-instructions",
        type=int,
        default=256,
        help="Maximum instructions to lift before failing.",
    )
    parser.add_argument(
        "--cpp-output",
        type=Path,
        help="Path for generated C++ output.",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        help="Optional path for the JSON summary.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    args = parser.parse_args()

    if args.hex_bytes is not None:
        function = lift_x86_function(
            args.hex_bytes,
            base_address=args.virtual_address,
            symbol=args.symbol,
            max_instructions=args.max_instructions,
        )
        source_kind = "synthetic_hex"
        source_name = None
        public_safe = True
    else:
        if args.xbe is None:
            parser.error("an XBE path or --hex is required")
        if args.size is None:
            parser.error("--size is required when reading from an XBE")
        function = lift_xbe_range(
            args.xbe,
            virtual_address=args.virtual_address,
            size=args.size,
            symbol=args.symbol,
            max_instructions=args.max_instructions,
        )
        source_kind = "xbe_range"
        source_name = args.xbe.name
        public_safe = False

    cpp_source = emit_cpp(function, exported_symbol=args.symbol)
    if args.cpp_output is not None:
        args.cpp_output.parent.mkdir(parents=True, exist_ok=True)
        args.cpp_output.write_text(cpp_source, encoding="utf-8", newline="\n")

    summary = build_recompilation_summary(
        function,
        cpp_source=cpp_source,
        cpp_output=args.cpp_output,
        source_kind=source_kind,
        source_name=source_name,
        public_safe=public_safe,
    )
    output = summary_json(summary, pretty=args.pretty)
    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(output + "\n", encoding="utf-8", newline="\n")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
