#!/usr/bin/env python3
"""Smoke-test runtime shim registration for an XBE."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

try:
    from runtime.xbox.shims import XboxRuntimeConfig, XboxRuntimeShims
    from tools.loader.xbe_loader import ImportResolver, load_xbe_file
    from tools.xbe.xbe_info import parse_xbe_file
except ModuleNotFoundError:  # pragma: no cover - direct script execution fallback
    import sys

    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from runtime.xbox.shims import XboxRuntimeConfig, XboxRuntimeShims
    from tools.loader.xbe_loader import ImportResolver, load_xbe_file
    from tools.xbe.xbe_info import parse_xbe_file


def build_runtime_smoke_summary(
    xbe_path: Path, *, extracted_root: Path | None = None
) -> dict[str, Any]:
    info = parse_xbe_file(xbe_path)
    imported_ordinals = [
        import_info["ordinal"] for import_info in info["kernel_imports"]["imports"]
    ]

    runtime = XboxRuntimeShims(
        XboxRuntimeConfig(extracted_disc_root=extracted_root)
    )
    resolver = ImportResolver()
    runtime.register_kernel_imports(resolver, imported_ordinals)
    loaded = load_xbe_file(xbe_path, resolver=resolver)
    unresolved = [
        resolution for resolution in loaded.import_resolutions if not resolution.resolved
    ]
    runtime_summary = runtime.summary()

    return {
        "format": "b2-recomp-runtime-smoke",
        "public_safe": False,
        "source": {
            "input_file_name": xbe_path.name,
            "title_name": info["certificate"]["title_name"],
            "kernel_import_count": len(imported_ordinals),
        },
        "registered_kernel_shim_count": len(runtime.registered_shims),
        "unresolved_import_count": len(unresolved),
        "registered_behavior_counts": runtime_summary["registered_behavior_counts"],
        "registered_subsystem_counts": runtime_summary["registered_subsystem_counts"],
        "loader_patched_import_count": len(loaded.import_resolutions),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Register runtime shims for an XBE and emit a non-byte smoke summary."
    )
    parser.add_argument("xbe", type=Path, help="Path to the XBE file to smoke-test.")
    parser.add_argument(
        "--extracted-root",
        type=Path,
        help="Optional extracted disc root used by filesystem shims.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    args = parser.parse_args()

    summary = build_runtime_smoke_summary(args.xbe, extracted_root=args.extracted_root)
    print(json.dumps(summary, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
