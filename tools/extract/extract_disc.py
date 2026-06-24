#!/usr/bin/env python3
"""Extract a local Xbox XISO and generate an ignored metadata report."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zlib
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "data" / "local" / "extracted" / "burnout_2_poi_usa"
DEFAULT_REPORT_PATH = REPO_ROOT / "reports" / "local" / "disc-extraction.json"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.xbe.xbe_info import XbeFormatError, parse_xbe_file  # noqa: E402


def _utc_now() -> str:
    return dt.datetime.now(tz=dt.UTC).isoformat(timespec="seconds")


def _relative(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _ensure_inside_data_local(path: Path) -> None:
    resolved = path.resolve()
    allowed_root = (REPO_ROOT / "data" / "local").resolve()
    if resolved != allowed_root and allowed_root not in resolved.parents:
        raise SystemExit(
            f"Refusing to write outside data/local without changing the script: {resolved}"
        )


def _ensure_ignored_report_path(path: Path) -> None:
    resolved = path.resolve()
    allowed_roots = [
        (REPO_ROOT / "reports" / "local").resolve(),
        (REPO_ROOT / "data" / "local").resolve(),
    ]
    for allowed_root in allowed_roots:
        if resolved == allowed_root or allowed_root in resolved.parents:
            return
    raise SystemExit(
        "Refusing to write local extraction metadata outside reports/local "
        f"or data/local: {resolved}"
    )


def _hash_file(path: Path) -> dict[str, Any]:
    md5 = hashlib.md5(usedforsecurity=False)
    sha1 = hashlib.sha1(usedforsecurity=False)
    sha256 = hashlib.sha256()
    crc = 0
    size = 0

    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            crc = zlib.crc32(chunk, crc)
            md5.update(chunk)
            sha1.update(chunk)
            sha256.update(chunk)

    return {
        "size_bytes": size,
        "crc32": f"{crc & 0xFFFFFFFF:08X}",
        "md5": md5.hexdigest().upper(),
        "sha1": sha1.hexdigest().upper(),
        "sha256": sha256.hexdigest().upper(),
    }


def _find_extract_xiso(explicit: Path | None) -> Path:
    if explicit is not None:
        if explicit.is_file():
            return explicit.resolve()
        raise SystemExit(f"extract-xiso executable was not found: {explicit}")

    env_path = os.environ.get("EXTRACT_XISO")
    if env_path:
        candidate = Path(env_path)
        if candidate.is_file():
            return candidate.resolve()

    for command in ("extract-xiso", "extract-xiso.exe"):
        found = shutil.which(command)
        if found:
            return Path(found).resolve()

    local_tool_root = REPO_ROOT / "data" / "local" / "tools" / "extract-xiso"
    if local_tool_root.exists():
        candidates = sorted(
            [
                path
                for path in local_tool_root.rglob("*")
                if path.is_file()
                and path.name.lower() in {"extract-xiso", "extract-xiso.exe"}
            ],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0].resolve()

    raise SystemExit(
        "extract-xiso was not found. Run tools/extract/install_extract_xiso.ps1 "
        "or pass --extract-xiso."
    )


def _run_command(command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _extract_iso(tool: Path, iso_path: Path, output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    result = _run_command([str(tool), "-x", "-d", str(output_dir), str(iso_path)])
    if result["returncode"] != 0:
        raise SystemExit(
            "extract-xiso failed with code "
            f"{result['returncode']}\n{result['stderr'] or result['stdout']}"
        )
    return result


def _file_extension(path: Path) -> str:
    suffix = path.suffix.lower()
    return suffix if suffix else "[no extension]"


def _layout_report(output_dir: Path) -> dict[str, Any]:
    files = [path for path in output_dir.rglob("*") if path.is_file()]
    dirs = [path for path in output_dir.rglob("*") if path.is_dir()]
    total_bytes = sum(path.stat().st_size for path in files)

    normalized_paths: dict[str, list[str]] = defaultdict(list)
    for path in [*files, *dirs]:
        rel = path.relative_to(output_dir).as_posix()
        normalized_paths[rel.lower()].append(rel)
    case_collisions = [
        sorted(paths) for paths in normalized_paths.values() if len(set(paths)) > 1
    ]

    root_entries = []
    for child in sorted(output_dir.iterdir(), key=lambda item: item.name.lower()):
        entry = {
            "name": child.name,
            "type": "directory" if child.is_dir() else "file",
        }
        if child.is_file():
            entry["size_bytes"] = child.stat().st_size
        root_entries.append(entry)

    extensions = Counter(_file_extension(path) for path in files)

    return {
        "output_dir": _relative(output_dir),
        "file_count": len(files),
        "directory_count": len(dirs),
        "total_file_bytes": total_bytes,
        "root_entries": root_entries,
        "extension_counts": dict(sorted(extensions.items())),
        "case_only_collisions": case_collisions,
        "case_only_collision_count": len(case_collisions),
    }


def _main_executable(xbes: list[Path], output_dir: Path) -> Path | None:
    if not xbes:
        return None

    default_xbes = [
        path for path in xbes if path.relative_to(output_dir).as_posix().lower() == "default.xbe"
    ]
    if default_xbes:
        return default_xbes[0]

    basename_matches = [path for path in xbes if path.name.lower() == "default.xbe"]
    if basename_matches:
        return sorted(basename_matches, key=lambda path: len(path.parts))[0]

    return xbes[0]


def _executable_reports(output_dir: Path) -> tuple[list[dict[str, Any]], Path | None]:
    xbes = sorted(output_dir.rglob("*.xbe"), key=lambda path: path.as_posix().lower())
    main = _main_executable(xbes, output_dir)
    reports = []

    for path in xbes:
        item: dict[str, Any] = {
            "path": _relative(path),
            "relative_to_extraction": path.relative_to(output_dir).as_posix(),
            "is_main_candidate": main == path,
            "hashes": _hash_file(path),
        }
        try:
            item["xbe"] = parse_xbe_file(path)
        except (OSError, XbeFormatError) as exc:
            item["xbe_error"] = str(exc)
        reports.append(item)

    return reports, main


def _load_iso_details(iso_path: Path) -> dict[str, Any] | None:
    detail_path = iso_path.with_name("iso_details.txt")
    if not detail_path.is_file():
        return None
    return {
        "path": _relative(detail_path),
        "text": detail_path.read_text(encoding="utf-8", errors="replace"),
    }


def build_report(
    iso_path: Path,
    output_dir: Path,
    tool: Path,
    extraction_result: dict[str, Any] | None,
) -> dict[str, Any]:
    executables, main = _executable_reports(output_dir)
    return {
        "schema_version": 1,
        "created_at_utc": _utc_now(),
        "public_safe": False,
        "input": {
            "iso": {
                "path": _relative(iso_path),
                "hashes": _hash_file(iso_path),
                "source_details": _load_iso_details(iso_path),
            }
        },
        "tooling": {
            "extract_xiso": {
                "path": _relative(tool),
                "hashes": _hash_file(tool),
            }
        },
        "extraction": {
            "ran": extraction_result is not None,
            "output_dir": _relative(output_dir),
            "result": extraction_result,
        },
        "layout": _layout_report(output_dir),
        "main_executable": _relative(main) if main else None,
        "executables": executables,
    }


def write_report(report: dict[str, Any], report_path: Path) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract a local Xbox XISO and write an ignored JSON report."
    )
    parser.add_argument(
        "--iso",
        type=Path,
        required=True,
        help="Path to the local XISO/ISO. This file is never copied into the repo.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Extraction destination. Default: {_relative(DEFAULT_OUTPUT_DIR)}",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT_PATH,
        help=f"Report path. Default: {_relative(DEFAULT_REPORT_PATH)}",
    )
    parser.add_argument(
        "--extract-xiso",
        type=Path,
        default=None,
        help="Path to extract-xiso. Defaults to EXTRACT_XISO, PATH, then data/local/tools.",
    )
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="Skip extraction and only generate a report from an existing output directory.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    iso_path = args.iso.resolve()
    output_dir = args.output_dir.resolve()
    report_path = args.report.resolve()

    if not iso_path.is_file():
        raise SystemExit(f"ISO path does not exist: {iso_path}")
    _ensure_inside_data_local(output_dir)
    _ensure_ignored_report_path(report_path)

    tool = _find_extract_xiso(args.extract_xiso)
    extraction_result = None

    if args.scan_only:
        if not output_dir.is_dir():
            raise SystemExit(f"Cannot scan missing output directory: {output_dir}")
    else:
        if output_dir.exists() and any(output_dir.iterdir()):
            raise SystemExit(
                "Output directory is not empty. Use --scan-only to report on it, "
                "or choose a new --output-dir."
            )
        extraction_result = _extract_iso(tool, iso_path, output_dir)

    report = build_report(iso_path, output_dir, tool, extraction_result)
    write_report(report, report_path)

    main_executable = report["main_executable"] or "none"
    print(f"Wrote report: {_relative(report_path)}")
    print(f"Extracted files: {report['layout']['file_count']}")
    print(f"XBE files: {len(report['executables'])}")
    print(f"Main executable: {main_executable}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
