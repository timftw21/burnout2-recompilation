#!/usr/bin/env python3
"""Validate the exact native presenter compiler, Vulkan SDK, and SDL3 inputs."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TypedDict, cast


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK = REPO_ROOT / "tools" / "native_toolchain.lock.json"
DEFAULT_CLANGXX = Path("C:/Program Files/LLVM/bin/clang++.exe")


class BuildToolLock(TypedDict):
    cmake: str
    ninja: str


class CompilerLock(TypedDict):
    family: str
    version: str
    target: str
    sha256: str


class VulkanLock(TypedDict):
    version: str
    header_version: int
    artifacts: dict[str, str]


class SdlLock(TypedDict):
    version: str


class NativeToolchainLock(TypedDict):
    schema_version: int
    platform: str
    python_build_tools: BuildToolLock
    compiler: CompilerLock
    vulkan_sdk: VulkanLock
    sdl3: SdlLock


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_toolchain_lock(path: Path = DEFAULT_LOCK) -> NativeToolchainLock:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError(f"unsupported native toolchain lock: {path}")
    return cast(NativeToolchainLock, raw)


def toolchain_validation_errors(report: dict[str, object]) -> list[str]:
    """Normalize validation failures for callers without leaking untyped JSON."""
    errors = report.get("errors", [])
    if not isinstance(errors, list):
        return [str(errors)]
    return [str(error) for error in errors]


def _clang_version_and_target(output: str) -> tuple[str | None, str | None]:
    version_match = re.search(r"^clang version ([^\s]+)", output, re.MULTILINE)
    target_match = re.search(r"^Target: ([^\s]+)", output, re.MULTILINE)
    return (
        version_match.group(1) if version_match else None,
        target_match.group(1) if target_match else None,
    )


def _macro_integer(path: Path, name: str) -> int | None:
    pattern = re.compile(rf"^#define\s+{re.escape(name)}\s+(\d+)", re.MULTILINE)
    match = pattern.search(path.read_text(encoding="utf-8", errors="replace"))
    return int(match.group(1)) if match else None


def _resolve_clangxx(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.resolve()
    configured = os.environ.get("B2R_CLANGXX")
    if configured:
        return Path(configured).resolve()
    discovered = shutil.which("clang++")
    return Path(discovered).resolve() if discovered else DEFAULT_CLANGXX


def _resolve_vulkan_sdk(explicit: Path | None, version: str) -> Path:
    if explicit is not None:
        return explicit.resolve()
    configured = os.environ.get("VULKAN_SDK")
    if configured:
        return Path(configured).resolve()
    return Path(f"C:/VulkanSDK/{version}")


def validate_native_toolchain(
    *,
    lock_path: Path = DEFAULT_LOCK,
    clangxx: Path | None = None,
    vulkan_sdk: Path | None = None,
    include_build_tools: bool = True,
    include_presenter_tools: bool = True,
) -> dict[str, object]:
    lock = load_toolchain_lock(lock_path)
    errors: list[str] = []
    observed: dict[str, object] = {}

    if include_build_tools:
        build_tools: dict[str, str] = {}
        for package, expected in lock["python_build_tools"].items():
            try:
                actual = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                actual = "missing"
            build_tools[package] = actual
            if actual != expected:
                errors.append(f"{package} version {actual!r} != locked {expected!r}")
        observed["python_build_tools"] = build_tools

    if include_presenter_tools:
        compiler_path = _resolve_clangxx(clangxx)
        compiler_observed: dict[str, object] = {"path": str(compiler_path)}
        if not compiler_path.is_file():
            errors.append(f"locked compiler is missing: {compiler_path}")
        else:
            completed = subprocess.run(
                [str(compiler_path), "--version"],
                text=True,
                capture_output=True,
                check=False,
            )
            compiler_output = completed.stdout + completed.stderr
            version, target = _clang_version_and_target(compiler_output)
            digest = sha256_file(compiler_path)
            compiler_observed.update(
                {"version": version, "target": target, "sha256": digest}
            )
            expected_compiler = lock["compiler"]
            if completed.returncode != 0:
                errors.append(f"compiler version command failed: {compiler_path}")
            if version != expected_compiler["version"]:
                errors.append(
                    f"clang version {version!r} != locked {expected_compiler['version']!r}"
                )
            if target != expected_compiler["target"]:
                errors.append(
                    f"clang target {target!r} != locked {expected_compiler['target']!r}"
                )
            if digest != expected_compiler["sha256"]:
                errors.append("clang++ content hash does not match the lock")
        observed["compiler"] = compiler_observed

        expected_sdk = lock["vulkan_sdk"]
        sdk_path = _resolve_vulkan_sdk(vulkan_sdk, expected_sdk["version"])
        sdk_observed: dict[str, object] = {"path": str(sdk_path)}
        if not sdk_path.is_dir():
            errors.append(f"locked Vulkan SDK is missing: {sdk_path}")
        else:
            if sdk_path.name != expected_sdk["version"]:
                errors.append(
                    f"Vulkan SDK directory {sdk_path.name!r} != locked "
                    f"{expected_sdk['version']!r}"
                )
            vulkan_header = sdk_path / "Include" / "vulkan" / "vulkan_core.h"
            header_version = (
                _macro_integer(vulkan_header, "VK_HEADER_VERSION")
                if vulkan_header.is_file()
                else None
            )
            sdk_observed["header_version"] = header_version
            if header_version != expected_sdk["header_version"]:
                errors.append(
                    f"Vulkan header version {header_version!r} != locked "
                    f"{expected_sdk['header_version']!r}"
                )

            sdl_header = sdk_path / "Include" / "SDL3" / "SDL_version.h"
            sdl_version = None
            if sdl_header.is_file():
                components = [
                    _macro_integer(sdl_header, name)
                    for name in (
                        "SDL_MAJOR_VERSION",
                        "SDL_MINOR_VERSION",
                        "SDL_MICRO_VERSION",
                    )
                ]
                if all(component is not None for component in components):
                    sdl_version = ".".join(str(component) for component in components)
            sdk_observed["sdl3_version"] = sdl_version
            if sdl_version != lock["sdl3"]["version"]:
                errors.append(
                    f"SDL3 version {sdl_version!r} != locked {lock['sdl3']['version']!r}"
                )

            artifact_hashes: dict[str, str | None] = {}
            for relative, expected_hash in expected_sdk["artifacts"].items():
                artifact = sdk_path / Path(relative)
                actual_hash = sha256_file(artifact) if artifact.is_file() else None
                artifact_hashes[relative] = actual_hash
                if actual_hash != expected_hash:
                    errors.append(f"native SDK artifact does not match lock: {relative}")
            sdk_observed["artifacts"] = artifact_hashes
        observed["vulkan_sdk"] = sdk_observed

    return {
        "format": "b2-recomp-native-toolchain-validation",
        "schema_version": 1,
        "lock": str(lock_path.resolve()),
        "passed": not errors,
        "errors": errors,
        "observed": observed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--clangxx", type=Path)
    parser.add_argument("--vulkan-sdk", type=Path)
    parser.add_argument("--build-tools-only", action="store_true")
    parser.add_argument("--presenter-tools-only", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    if args.build_tools_only and args.presenter_tools_only:
        parser.error("tool-only modes are mutually exclusive")
    report = validate_native_toolchain(
        lock_path=args.lock,
        clangxx=args.clangxx,
        vulkan_sdk=args.vulkan_sdk,
        include_build_tools=not args.presenter_tools_only,
        include_presenter_tools=not args.build_tools_only,
    )
    print(json.dumps(report, indent=2 if args.pretty else None, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
