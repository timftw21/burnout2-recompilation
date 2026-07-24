#!/usr/bin/env python3
"""Run the repository's asset-free validation gates from one entry point."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable

BASE_COMMANDS: tuple[tuple[str, ...], ...] = (
    (PYTHON, "-m", "compileall", "-q", "runtime", "tools", "tests"),
    (PYTHON, "-m", "ruff", "check", "runtime", "tools", "tests"),
    (PYTHON, "-m", "mypy"),
    (PYTHON, "tools/native_toolchain.py", "--build-tools-only"),
    (
        PYTHON,
        "tools/project_maintenance.py",
        "status",
        "--fail-over-budget",
    ),
    (
        PYTHON,
        "tools/playability/preflight_validation.py",
        "--synthetic-fixtures",
    ),
    (PYTHON, "-m", "unittest", "discover", "-s", "tests", "-t", "."),
)

FULL_COMMANDS: tuple[tuple[str, ...], ...] = (
    (PYTHON, "tools/native_build.py", "--preset", "debug"),
)


def run_commands(commands: tuple[tuple[str, ...], ...]) -> int:
    for command in commands:
        print(f"+ {subprocess.list2cmdline(command)}", flush=True)
        result = subprocess.run(command, cwd=REPO_ROOT, check=False)
        if result.returncode != 0:
            return result.returncode
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full",
        action="store_true",
        help="also configure, build, and run the debug native CTest suite",
    )
    args = parser.parse_args(argv)
    commands = BASE_COMMANDS + (FULL_COMMANDS if args.full else ())
    return run_commands(commands)


if __name__ == "__main__":
    raise SystemExit(main())
