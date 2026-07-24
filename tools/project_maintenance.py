#!/usr/bin/env python3
"""Inspect and safely prune generated b2_recomp artifacts."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, cast

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
REPORT_ROOT = REPO_ROOT / "reports" / "local"
BUILD_ROOT = REPO_ROOT / "build"
NATIVE_BUILD_ROOT = BUILD_ROOT / "native-guest-loop"
NATIVE_MANIFEST = NATIVE_BUILD_ROOT / "native-module-manifest.sqlite3"

DEFAULT_REPORT_MAX_AGE_DAYS = 30.0
DEFAULT_REPORT_MAX_FILES = 2_000
DEFAULT_REPORT_MAX_BYTES = 2 * 1024**3
DEFAULT_BUILD_MAX_FILES = 8_000
DEFAULT_BUILD_MAX_BYTES = 6 * 1024**3
DEFAULT_NATIVE_MAX_ARTIFACTS = 2_048
DEFAULT_NATIVE_MAX_BYTES = 4 * 1024**3
PROTECTED_REPORT_NAMES = frozenset({"run-manifest.json", ".gitkeep"})


class MaintenanceError(RuntimeError):
    """Raised when a maintenance request is unsafe or malformed."""


@dataclass(frozen=True)
class FileRecord:
    path: Path
    size: int
    modified_ns: int

    def to_dict(self, *, relative_to: Path | None = None) -> dict[str, Any]:
        path = self.path
        if relative_to is not None:
            try:
                path = path.relative_to(relative_to)
            except ValueError:
                pass
        return {
            "path": str(path),
            "bytes": self.size,
            "modified_ns": self.modified_ns,
        }


def parse_byte_budget(value: str) -> int:
    text = value.strip().casefold()
    suffixes = {
        "gib": 1024**3,
        "mib": 1024**2,
        "kib": 1024,
        "gb": 1000**3,
        "mb": 1000**2,
        "kb": 1000,
        "b": 1,
    }
    multiplier = 1
    for suffix, candidate in suffixes.items():
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
            multiplier = candidate
            break
    try:
        parsed = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid byte budget: {value}") from exc
    result = int(parsed * multiplier)
    if result < 0:
        raise argparse.ArgumentTypeError("byte budgets must not be negative")
    return result


def inventory_files(root: Path) -> list[FileRecord]:
    if not root.exists():
        return []
    root = root.resolve()
    records: list[FileRecord] = []
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        resolved = path.resolve()
        if root not in resolved.parents:
            raise MaintenanceError(f"generated file escapes managed root: {path}")
        stat = resolved.stat()
        records.append(FileRecord(resolved, stat.st_size, stat.st_mtime_ns))
    return sorted(records, key=lambda record: str(record.path).casefold())


def retention_plan(
    records: Iterable[FileRecord],
    *,
    now_ns: int,
    max_age_ns: int,
    max_files: int,
    max_bytes: int,
    protected_paths: Iterable[Path] = (),
) -> list[FileRecord]:
    protected = {path.resolve() for path in protected_paths}
    active = list(records)
    selected: dict[Path, FileRecord] = {}
    if max_age_ns > 0:
        cutoff = now_ns - max_age_ns
        for record in active:
            if record.path not in protected and record.modified_ns < cutoff:
                selected[record.path] = record
    remaining = [record for record in active if record.path not in selected]
    remaining_bytes = sum(record.size for record in remaining)
    for record in sorted(
        remaining,
        key=lambda item: (item.modified_ns, str(item.path).casefold()),
    ):
        over_files = max_files >= 0 and len(remaining) > max_files
        over_bytes = max_bytes >= 0 and remaining_bytes > max_bytes
        if not over_files and not over_bytes:
            break
        if record.path in protected:
            continue
        selected[record.path] = record
        remaining.remove(record)
        remaining_bytes -= record.size
    return sorted(
        selected.values(),
        key=lambda item: (item.modified_ns, str(item.path).casefold()),
    )


def summarize_records(
    name: str,
    root: Path,
    records: Iterable[FileRecord],
    *,
    max_files: int,
    max_bytes: int,
) -> dict[str, Any]:
    retained = list(records)
    total_bytes = sum(record.size for record in retained)
    return {
        "name": name,
        "root": str(root.resolve()),
        "file_count": len(retained),
        "bytes": total_bytes,
        "max_files": max_files,
        "max_bytes": max_bytes,
        "over_file_budget": len(retained) > max_files,
        "over_byte_budget": total_bytes > max_bytes,
    }


def inspect_native_manifest(
    path: Path = NATIVE_MANIFEST,
    *,
    max_artifacts: int = DEFAULT_NATIVE_MAX_ARTIFACTS,
    max_bytes: int = DEFAULT_NATIVE_MAX_BYTES,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "path": str(path.resolve()),
        "exists": path.is_file(),
        "artifact_count": 0,
        "artifact_bytes": 0,
        "source_bytes": 0,
        "last_prune_ns": None,
        "max_artifacts": max_artifacts,
        "max_bytes": max_bytes,
        "over_artifact_budget": False,
        "over_byte_budget": False,
    }
    if not path.is_file():
        return summary
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        row = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(artifact_bytes), 0), "
            "COALESCE(SUM(source_bytes), 0) FROM native_modules"
        ).fetchone()
        prune_row = connection.execute(
            "SELECT value FROM cache_metadata WHERE key='last_prune_ns'"
        ).fetchone()
    except sqlite3.Error as exc:
        summary["error"] = str(exc)
        return summary
    finally:
        if connection is not None:
            connection.close()
    summary.update(
        {
            "artifact_count": int(row[0]),
            "artifact_bytes": int(row[1]),
            "source_bytes": int(row[2]),
            "last_prune_ns": int(prune_row[0]) if prune_row is not None else None,
            "over_artifact_budget": int(row[0]) > max_artifacts,
            "over_byte_budget": int(row[1]) + int(row[2]) > max_bytes,
        }
    )
    return summary


def build_status_report() -> dict[str, Any]:
    reports = inventory_files(REPORT_ROOT)
    build = inventory_files(BUILD_ROOT)
    categories = [
        summarize_records(
            "reports",
            REPORT_ROOT,
            reports,
            max_files=DEFAULT_REPORT_MAX_FILES,
            max_bytes=DEFAULT_REPORT_MAX_BYTES,
        ),
        summarize_records(
            "build",
            BUILD_ROOT,
            build,
            max_files=DEFAULT_BUILD_MAX_FILES,
            max_bytes=DEFAULT_BUILD_MAX_BYTES,
        ),
    ]
    native_module_cache = inspect_native_manifest()
    return {
        "format": "b2-recomp-maintenance-status",
        "schema_version": 1,
        "repository": str(REPO_ROOT),
        "categories": categories,
        "native_module_cache": native_module_cache,
        "within_budget": not any(
            category["over_file_budget"] or category["over_byte_budget"]
            for category in categories
        )
        and not native_module_cache["over_artifact_budget"]
        and not native_module_cache["over_byte_budget"],
    }


def _protected_report_paths(records: Iterable[FileRecord]) -> set[Path]:
    return {
        record.path
        for record in records
        if record.path.name in PROTECTED_REPORT_NAMES
    }


def build_report_prune_plan(
    *,
    max_age_days: float,
    max_files: int,
    max_bytes: int,
    now_ns: int | None = None,
) -> dict[str, Any]:
    if max_age_days < 0 or max_files < 0 or max_bytes < 0:
        raise MaintenanceError("retention budgets must not be negative")
    records = inventory_files(REPORT_ROOT)
    selected = retention_plan(
        records,
        now_ns=time.time_ns() if now_ns is None else now_ns,
        max_age_ns=int(max_age_days * 24 * 60 * 60 * 1_000_000_000),
        max_files=max_files,
        max_bytes=max_bytes,
        protected_paths=_protected_report_paths(records),
    )
    return {
        "root": str(REPORT_ROOT.resolve()),
        "candidate_count": len(selected),
        "candidate_bytes": sum(record.size for record in selected),
        "candidates": [
            record.to_dict(relative_to=REPO_ROOT) for record in selected
        ],
    }


def delete_records(records: Iterable[FileRecord], *, root: Path) -> dict[str, int]:
    managed_root = root.resolve()
    removed_files = 0
    removed_bytes = 0
    for record in records:
        target = record.path.resolve()
        if managed_root not in target.parents:
            raise MaintenanceError(f"refusing to remove unmanaged path: {target}")
        try:
            target.unlink(missing_ok=True)
        except OSError as exc:
            raise MaintenanceError(f"could not remove {target}: {exc}") from exc
        removed_files += 1
        removed_bytes += record.size
    if managed_root.exists():
        directories = sorted(
            (path for path in managed_root.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        )
        for directory in directories:
            resolved = directory.resolve()
            if managed_root not in resolved.parents or directory.is_symlink():
                continue
            try:
                directory.rmdir()
            except OSError:
                pass
    return {"removed_files": removed_files, "removed_bytes": removed_bytes}


def _records_from_plan(plan: dict[str, Any]) -> list[FileRecord]:
    records = {record.path: record for record in inventory_files(REPORT_ROOT)}
    selected = []
    for candidate in plan["candidates"]:
        path = (REPO_ROOT / candidate["path"]).resolve()
        record = records.get(path)
        if record is not None:
            selected.append(record)
    return selected


def prune_native_cache(
    *,
    max_artifacts: int,
    max_bytes: int,
) -> dict[str, Any]:
    if not NATIVE_MANIFEST.is_file():
        return inspect_native_manifest(
            max_artifacts=max_artifacts,
            max_bytes=max_bytes,
        )
    from tools.recomp.native_executor import NativeModuleManifest

    manifest = NativeModuleManifest(
        NATIVE_BUILD_ROOT,
        max_artifacts=max_artifacts,
        max_artifact_bytes=max_bytes,
    )
    try:
        manifest.prune()
        return cast(dict[str, Any], manifest.summary())
    finally:
        manifest.close()


def _print_report(report: dict[str, Any], *, pretty: bool) -> None:
    print(json.dumps(report, indent=2 if pretty else None, sort_keys=True))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect or safely prune generated b2_recomp artifacts."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="Inspect generated-artifact budgets.")
    status.add_argument("--pretty", action="store_true")
    status.add_argument("--fail-over-budget", action="store_true")

    prune = subparsers.add_parser(
        "prune",
        help="Apply age/count/size retention; dry-run unless --apply is present.",
    )
    prune.add_argument("--apply", action="store_true")
    prune.add_argument("--pretty", action="store_true")
    prune.add_argument(
        "--report-max-age-days",
        type=float,
        default=DEFAULT_REPORT_MAX_AGE_DAYS,
    )
    prune.add_argument(
        "--report-max-files",
        type=int,
        default=DEFAULT_REPORT_MAX_FILES,
    )
    prune.add_argument(
        "--report-max-bytes",
        type=parse_byte_budget,
        default=DEFAULT_REPORT_MAX_BYTES,
    )
    prune.add_argument(
        "--native-max-artifacts",
        type=int,
        default=DEFAULT_NATIVE_MAX_ARTIFACTS,
    )
    prune.add_argument(
        "--native-max-bytes",
        type=parse_byte_budget,
        default=DEFAULT_NATIVE_MAX_BYTES,
    )

    clean_reports = subparsers.add_parser(
        "clean-reports",
        help="Remove generated reports; dry-run unless --apply is present.",
    )
    clean_reports.add_argument("--apply", action="store_true")
    clean_reports.add_argument("--pretty", action="store_true")

    clean = subparsers.add_parser(
        "clean",
        help="Remove generated build and report files; dry-run unless --apply is present.",
    )
    clean.add_argument("--apply", action="store_true")
    clean.add_argument("--pretty", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "status":
        report = build_status_report()
        _print_report(report, pretty=args.pretty)
        return 1 if args.fail_over_budget and not report["within_budget"] else 0

    if args.command == "prune":
        if args.native_max_artifacts < 0 or args.native_max_bytes < 0:
            parser.error("native retention budgets must not be negative")
        report_plan = build_report_prune_plan(
            max_age_days=args.report_max_age_days,
            max_files=args.report_max_files,
            max_bytes=args.report_max_bytes,
        )
        result: dict[str, Any] = {
            "format": "b2-recomp-maintenance-prune",
            "schema_version": 1,
            "applied": bool(args.apply),
            "reports": report_plan,
            "native_module_cache": inspect_native_manifest(
                max_artifacts=args.native_max_artifacts,
                max_bytes=args.native_max_bytes,
            ),
        }
        if args.apply:
            result["reports"]["removed"] = delete_records(
                _records_from_plan(report_plan), root=REPORT_ROOT
            )
            result["native_module_cache"] = prune_native_cache(
                max_artifacts=args.native_max_artifacts,
                max_bytes=args.native_max_bytes,
            )
        _print_report(result, pretty=args.pretty)
        return 0

    roots = [REPORT_ROOT] if args.command == "clean-reports" else [REPORT_ROOT, BUILD_ROOT]
    root_plans = []
    for root in roots:
        records = inventory_files(root)
        plan = {
            "root": str(root.resolve()),
            "candidate_count": len(records),
            "candidate_bytes": sum(record.size for record in records),
        }
        if args.apply:
            plan["removed"] = delete_records(records, root=root)
        root_plans.append(plan)
    _print_report(
        {
            "format": "b2-recomp-maintenance-clean",
            "schema_version": 1,
            "applied": bool(args.apply),
            "roots": root_plans,
        },
        pretty=args.pretty,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
