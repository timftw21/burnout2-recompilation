#!/usr/bin/env python3
"""Install and operate the repository-pinned content-addressed compiler cache."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = REPO_ROOT / "tools" / "compiler_cache.lock.json"
DEFAULT_CACHE_DIR = REPO_ROOT / "build" / "cache" / "sccache"
DEFAULT_TOOL_ROOT = REPO_ROOT / "build" / "tools"


class CompilerCacheError(RuntimeError):
    """Raised when the pinned compiler cache cannot be installed or resolved."""


@dataclass(frozen=True)
class CompilerCacheLock:
    version: str
    platform: str
    archive_url: str
    archive_sha256: str
    executable_sha256: str | None = None

    @property
    def install_directory_name(self) -> str:
        return f"sccache-v{self.version}-{self.platform}"


def load_lock(path: Path = LOCK_PATH) -> CompilerCacheLock:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or payload.get("tool") != "sccache":
        raise CompilerCacheError(f"unsupported compiler-cache lock: {path}")
    return CompilerCacheLock(
        version=str(payload["version"]),
        platform=str(payload["platform"]),
        archive_url=str(payload["archive_url"]),
        archive_sha256=str(payload["archive_sha256"]).lower(),
        executable_sha256=str(payload["executable_sha256"]).lower(),
    )


def pinned_executable(
    lock: CompilerCacheLock | None = None,
    *,
    tool_root: Path = DEFAULT_TOOL_ROOT,
) -> Path:
    selected = lock or load_lock()
    return tool_root / selected.install_directory_name / "sccache.exe"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_archive_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise CompilerCacheError("sccache archive URL must use HTTPS")
    return value


def install_pinned_cache(
    *,
    lock: CompilerCacheLock | None = None,
    tool_root: Path = DEFAULT_TOOL_ROOT,
) -> Path:
    selected = lock or load_lock()
    executable = pinned_executable(selected, tool_root=tool_root)
    if (
        executable.is_file()
        and (
            selected.executable_sha256 is None
            or _sha256(executable) == selected.executable_sha256
        )
    ):
        return executable
    install_directory = executable.parent
    install_directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="b2r-sccache-") as temp_directory:
        archive = Path(temp_directory) / "sccache.zip"
        # _validated_archive_url restricts this call to HTTPS with a hostname.
        urllib.request.urlretrieve(  # nosec B310
            _validated_archive_url(selected.archive_url),
            archive,
        )
        actual_digest = _sha256(archive)
        if actual_digest != selected.archive_sha256:
            raise CompilerCacheError(
                "sccache archive hash mismatch: "
                f"expected {selected.archive_sha256}, received {actual_digest}"
            )
        with zipfile.ZipFile(archive) as bundle:
            candidates = [
                info for info in bundle.infolist()
                if Path(info.filename).name.lower() == "sccache.exe"
            ]
            if len(candidates) != 1:
                raise CompilerCacheError("pinned sccache archive has no unique executable")
            with bundle.open(candidates[0]) as source, executable.open("wb") as destination:
                shutil.copyfileobj(source, destination)
    if (
        selected.executable_sha256 is not None
        and _sha256(executable) != selected.executable_sha256
    ):
        executable.unlink(missing_ok=True)
        raise CompilerCacheError("extracted sccache executable hash mismatch")
    return executable


def _reported_version(executable: Path) -> str | None:
    completed = subprocess.run(
        (str(executable), "--version"),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        return None
    fields = completed.stdout.strip().split()
    return fields[-1] if fields else None


def resolve_compiler_cache(
    mode: str = "auto",
    *,
    lock: CompilerCacheLock | None = None,
    tool_root: Path = DEFAULT_TOOL_ROOT,
) -> Path | None:
    if mode == "off":
        return None
    selected = lock or load_lock()
    configured = os.environ.get("B2R_SCCACHE")
    candidates = (
        Path(configured) if configured else None,
        pinned_executable(selected, tool_root=tool_root),
    )
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            if (
                selected.executable_sha256 is not None
                and _sha256(candidate) != selected.executable_sha256
            ):
                if mode == "required":
                    raise CompilerCacheError(f"sccache executable hash rejected: {candidate}")
                continue
            if _reported_version(candidate) != selected.version:
                if mode == "required":
                    raise CompilerCacheError(
                        f"sccache {selected.version} required, received {candidate}"
                    )
                continue
            return candidate.resolve()
    if mode == "required":
        raise CompilerCacheError(
            "pinned sccache is missing; run `python tools/compiler_cache.py install`"
        )
    return None


def cache_environment(
    base: Mapping[str, str] | None = None,
    *,
    cache_directory: Path = DEFAULT_CACHE_DIR,
) -> dict[str, str]:
    environment = dict(os.environ if base is None else base)
    cache_directory.mkdir(parents=True, exist_ok=True)
    environment["SCCACHE_DIR"] = str(cache_directory.resolve())
    environment["SCCACHE_BASEDIRS"] = str(REPO_ROOT.resolve())
    environment.setdefault("SCCACHE_CACHE_SIZE", "20G")
    environment.setdefault("SCCACHE_CLIENT_SIDE", "1")
    return environment


def read_stats(executable: Path, environment: Mapping[str, str]) -> dict[str, Any]:
    completed = subprocess.run(
        (str(executable), "--show-stats", "--stats-format", "json"),
        cwd=REPO_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise CompilerCacheError(completed.stderr.strip() or "sccache stats failed")
    payload = json.loads(completed.stdout)
    if not isinstance(payload, dict):
        raise CompilerCacheError("sccache stats were not a JSON object")
    return payload


def write_stats(
    executable: Path,
    environment: Mapping[str, str],
    output: Path,
    *,
    source_bytes_compiled: int | None = None,
    compiled_source_count: int | None = None,
) -> dict[str, Any]:
    payload = read_stats(executable, environment)
    raw_stats = payload.get("stats")
    if isinstance(raw_stats, dict):
        hit_groups = raw_stats.get("cache_hits", {})
        miss_groups = raw_stats.get("cache_misses", {})

        def count_groups(value: object) -> int:
            if not isinstance(value, dict):
                return 0
            for key in ("counts", "adv_counts"):
                group = value.get(key)
                if isinstance(group, dict):
                    return sum(
                        int(count) for count in group.values() if isinstance(count, int)
                    )
            return 0

        hits = count_groups(hit_groups)
        misses = count_groups(miss_groups)
        requests = hits + misses
        payload["b2_recomp"] = {
            "cache_hit_count": hits,
            "cache_miss_count": misses,
            "cache_hit_rate": hits / requests if requests else None,
            "source_bytes_compiled": source_bytes_compiled,
            "compiled_source_count": compiled_source_count,
        }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("install", "path", "stats"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        executable = (
            install_pinned_cache()
            if args.command == "install"
            else resolve_compiler_cache("required")
        )
        assert executable is not None
        if args.command in {"install", "path"}:
            print(executable)
            return 0
        environment = cache_environment()
        payload = (
            write_stats(executable, environment, args.output)
            if args.output is not None
            else read_stats(executable, environment)
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    except (CompilerCacheError, OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    sys.exit(main())
