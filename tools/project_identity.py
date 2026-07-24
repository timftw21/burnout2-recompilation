"""Project, target, build, and run identity helpers.

This module deliberately stays independent of the live runtime.  Launchers and
offline tools can use the same exact target gate without importing the large
playability implementation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import struct
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

MODULE_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(MODULE_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(MODULE_REPO_ROOT))

from tools.analysis.analysis_db import SCHEMA_VERSION as ANALYSIS_SCHEMA_VERSION
from tools.loader.xbe_loader import load_xbe_bytes
from tools.xbe.xbe_info import parse_xbe_bytes


REPO_ROOT = MODULE_REPO_ROOT
DEFAULT_SUPPORTED_TARGETS = REPO_ROOT / "tools" / "targets" / "supported_targets.json"
TARGET_MANIFEST_SCHEMA_VERSION = 1
RUN_MANIFEST_SCHEMA_VERSION = 1
RUNTIME_COMPATIBILITY_VERSION = 1
GENERATED_CODE_ABI_VERSION = 1
NORMALIZED_XBE_HASH_FORMAT = "b2-recomp-normalized-xbe-v1"
GENERATED_CODE_INPUTS = (
    REPO_ROOT / "tools" / "recomp" / "x86_lifter.py",
    REPO_ROOT / "tools" / "recomp" / "native_executor.py",
    REPO_ROOT / "tools" / "playability" / "playability_probe.py",
    REPO_ROOT / "runtime" / "xbox" / "shims.py",
)


class ProjectIdentityError(RuntimeError):
    """Raised when a target or generated artifact has unsupported identity."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_run_id() -> str:
    return str(uuid.uuid4())


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _normalized_xbe_sha256(data: bytes) -> str:
    loaded = load_xbe_bytes(data, resolve_imports=False)
    digest = hashlib.sha256()
    digest.update(NORMALIZED_XBE_HASH_FORMAT.encode("ascii") + b"\0")
    for region in loaded.arena.regions:
        digest.update(struct.pack("<II", region.virtual_address, region.size))
        digest.update(loaded.arena.read(region.virtual_address, region.size))
    return digest.hexdigest().upper()


def inspect_xbe_identity(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    info = parse_xbe_bytes(data)
    certificate = info["certificate"]
    header = info["header"]
    return {
        "path": str(path.resolve()),
        "file_name": path.name,
        "file_size": len(data),
        "file_sha256": sha256_bytes(data),
        "normalized_image_hash_format": NORMALIZED_XBE_HASH_FORMAT,
        "normalized_image_sha256": _normalized_xbe_sha256(data),
        "header": {
            "base_address": header["base_address"],
            "image_size": header["image_size"],
            "headers_size": header["headers_size"],
            "section_count": header["section_count"],
            "timestamp": header["timestamp"],
        },
        "certificate": {
            "title_id": certificate["title_id"]["hex"],
            "title_name": certificate["title_name"],
            "timestamp": certificate["timestamp"],
            "game_region": certificate["game_region"],
            "game_region_names": certificate["game_region_names"],
            "disc_number": certificate["disc_number"],
            "version": certificate["version"],
        },
        "sections": [
            {
                "index": section["index"],
                "name": section["name"],
                "virtual_address": section["virtual_address"],
                "virtual_size": section["virtual_size"],
                "raw_address": section["raw_address"],
                "raw_size": section["raw_size"],
                "expected_sha1": section["digest_verification"]["expected_sha1"],
                "actual_sha1": section["digest_verification"]["actual_sha1"],
                "embedded_digest_matches": section["digest_verification"]["matches"],
            }
            for section in info["sections"]
        ],
    }


def load_supported_targets(path: Path = DEFAULT_SUPPORTED_TARGETS) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectIdentityError(f"cannot read supported-target manifest {path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ProjectIdentityError("supported-target manifest root must be an object")
    if manifest.get("schema_version") != TARGET_MANIFEST_SCHEMA_VERSION:
        raise ProjectIdentityError(
            "unsupported target-manifest schema: "
            f"{manifest.get('schema_version')!r}; expected {TARGET_MANIFEST_SCHEMA_VERSION}"
        )
    tool_contract = manifest.get("tool_contract", {})
    expected_contract = {
        "analysis_schema_version": ANALYSIS_SCHEMA_VERSION,
        "generated_code_abi_version": GENERATED_CODE_ABI_VERSION,
        "runtime_compatibility_version": RUNTIME_COMPATIBILITY_VERSION,
    }
    for key, expected in expected_contract.items():
        if tool_contract.get(key) != expected:
            raise ProjectIdentityError(
                f"supported-target manifest {key} is {tool_contract.get(key)!r}; "
                f"runtime requires {expected}"
            )
    if not isinstance(manifest.get("targets"), list) or not manifest["targets"]:
        raise ProjectIdentityError("supported-target manifest contains no targets")
    if not all(isinstance(target, dict) for target in manifest["targets"]):
        raise ProjectIdentityError("supported-target manifest contains a malformed target")
    return manifest


def _compare_value(
    mismatches: list[dict[str, Any]],
    field: str,
    expected: Any,
    actual: Any,
) -> None:
    if actual != expected:
        mismatches.append({"field": field, "expected": expected, "actual": actual})


def _target_mismatches(target: dict[str, Any], actual: dict[str, Any]) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    for field in ("file_size", "file_sha256", "normalized_image_sha256"):
        _compare_value(mismatches, field, target.get(field), actual.get(field))
    for group in ("header", "certificate"):
        expected_group = target.get(group, {})
        actual_group = actual.get(group, {})
        for field, expected in expected_group.items():
            _compare_value(
                mismatches,
                f"{group}.{field}",
                expected,
                actual_group.get(field),
            )
    expected_sections = target.get("sections", [])
    actual_sections = actual.get("sections", [])
    _compare_value(mismatches, "sections.count", len(expected_sections), len(actual_sections))
    for index, expected_section in enumerate(expected_sections):
        if index >= len(actual_sections):
            break
        actual_section = actual_sections[index]
        for field, expected in expected_section.items():
            _compare_value(
                mismatches,
                f"sections[{index}].{field}",
                expected,
                actual_section.get(field),
            )
    return mismatches


def verify_supported_xbe(
    xbe_path: Path,
    *,
    manifest_path: Path = DEFAULT_SUPPORTED_TARGETS,
    allow_unsupported: bool = False,
) -> dict[str, Any]:
    manifest = load_supported_targets(manifest_path)
    actual = inspect_xbe_identity(xbe_path)
    candidates: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for target in manifest["targets"]:
        candidates.append((target, _target_mismatches(target, actual)))
    matched = next((target for target, mismatches in candidates if not mismatches), None)
    if matched is not None:
        return {
            "status": "supported",
            "supported": True,
            "override_used": False,
            "target_id": matched["target_id"],
            "target_description": matched["description"],
            "manifest": str(manifest_path.resolve()),
            "manifest_schema_version": manifest["schema_version"],
            "tool_contract": manifest["tool_contract"],
            "identity": actual,
            "mismatches": [],
        }

    closest_target, mismatches = min(candidates, key=lambda item: len(item[1]))
    result = {
        "status": "unsupported_override" if allow_unsupported else "unsupported",
        "supported": False,
        "override_used": bool(allow_unsupported),
        "target_id": closest_target.get("target_id"),
        "target_description": closest_target.get("description"),
        "manifest": str(manifest_path.resolve()),
        "manifest_schema_version": manifest["schema_version"],
        "tool_contract": manifest["tool_contract"],
        "identity": actual,
        "mismatches": mismatches,
    }
    if allow_unsupported:
        return result
    details = "; ".join(
        f"{item['field']} expected {item['expected']!r}, got {item['actual']!r}"
        for item in mismatches[:5]
    )
    raise ProjectIdentityError(
        f"unsupported XBE {xbe_path}; closest target {closest_target.get('target_id')}: "
        f"{details}. Use --allow-unsupported-xbe only for explicitly unsupported research runs."
    )


def _git_output(repo_root: Path, *args: str) -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def git_identity(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    commit = _git_output(repo_root, "rev-parse", "HEAD")
    branch = _git_output(repo_root, "branch", "--show-current")
    porcelain = _git_output(repo_root, "status", "--porcelain=v1", "--untracked-files=all")
    status = porcelain.splitlines() if porcelain else []
    return {
        "available": commit is not None,
        "commit": commit,
        "branch": branch or None,
        "dirty": bool(status),
        "status": status,
    }


def hash_source_set(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted((item.resolve() for item in paths), key=str):
        try:
            relative = path.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            relative = str(path)
        data = path.read_bytes()
        encoded_name = relative.encode("utf-8")
        digest.update(struct.pack("<I", len(encoded_name)))
        digest.update(encoded_name)
        digest.update(struct.pack("<Q", len(data)))
        digest.update(data)
    return digest.hexdigest().upper()


def generated_code_identity() -> dict[str, Any]:
    return {
        "abi_version": GENERATED_CODE_ABI_VERSION,
        "build_id": hash_source_set(GENERATED_CODE_INPUTS),
        "inputs": [path.relative_to(REPO_ROOT).as_posix() for path in GENERATED_CODE_INPUTS],
    }


def file_identity(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    exists = resolved.is_file()
    return {
        "path": str(resolved),
        "exists": exists,
        "size": resolved.stat().st_size if exists else None,
        "sha256": sha256_file(resolved) if exists else None,
    }


def machine_identity() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "processor": platform.processor() or None,
        "logical_cpu_count": os.cpu_count(),
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
    }


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify a local XBE against the exact supported-target manifest."
    )
    parser.add_argument("xbe", type=Path)
    parser.add_argument(
        "--supported-targets",
        type=Path,
        default=DEFAULT_SUPPORTED_TARGETS,
    )
    parser.add_argument("--allow-unsupported-xbe", action="store_true")
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    try:
        result = verify_supported_xbe(
            args.xbe,
            manifest_path=args.supported_targets,
            allow_unsupported=args.allow_unsupported_xbe,
        )
    except ProjectIdentityError as exc:
        print(f"Target identity rejected: {exc}", file=sys.stderr)
        return 2
    if args.json_output is not None:
        write_json_atomic(args.json_output, result)
    print(json.dumps(result, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
