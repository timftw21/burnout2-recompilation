#!/usr/bin/env python3
"""Run the repository's asset-free validation gates through the cached DAG."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full",
        action="store_true",
        help="also configure, build, and run the debug native CTest suite",
    )
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--explain", action="store_true")
    parser.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1))
    args = parser.parse_args(argv)
    from tools.dev_check import main as dev_check_main

    forwarded = ["--all", "--jobs", str(args.jobs)]
    if args.full:
        forwarded.append("--full")
    if args.no_cache:
        forwarded.append("--no-cache")
    if args.explain:
        forwarded.append("--explain")
    return dev_check_main(forwarded)


if __name__ == "__main__":
    raise SystemExit(main())
