#!/usr/bin/env python3
"""Run one external gate with timing budgets and a bounded failure capsule."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.validation_runner import ValidationNode, run_validation_graph


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--budget-seconds", type=float)
    parser.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        parser.error("a command is required after --")
    if args.budget_seconds is not None and args.budget_seconds <= 0:
        parser.error("--budget-seconds must be positive")
    node = ValidationNode(
        args.name,
        tuple(command),
        f"standalone {args.name} gate",
        cacheable=False,
        exclusive=True,
        budget_seconds=args.budget_seconds,
    )
    summary = run_validation_graph(
        {node.name: node},
        (node.name,),
        cache_path=REPO_ROOT / "build" / "local" / "dev-check" / "command-cache.json",
        root=REPO_ROOT,
        jobs=args.jobs,
        no_cache=True,
    )
    return 0 if summary.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
