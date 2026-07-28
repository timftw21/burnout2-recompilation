#!/usr/bin/env python3
"""Loader skeleton for mapping Xbox XBE images into a controlled arena."""

from __future__ import annotations

import argparse
import json
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from tools.xbe.xbe_info import KERNEL_EXPORT_NAMES, parse_xbe_bytes
except ModuleNotFoundError:  # pragma: no cover - direct script execution fallback
    import sys

    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from tools.xbe.xbe_info import KERNEL_EXPORT_NAMES, parse_xbe_bytes


class XbeLoaderError(RuntimeError):
    """Base exception for XBE loader failures."""


class XbeMemoryAccessError(XbeLoaderError):
    """Raised when an emulated memory access is outside mapped XBE memory."""


class XbeImportResolutionError(XbeLoaderError):
    """Raised when an import cannot be resolved under the selected policy."""


def _hex32(value: int) -> str:
    return f"0x{value:08X}"


def _u32_bytes(value: int) -> bytes:
    if value < 0 or value > 0xFFFFFFFF:
        raise ValueError(f"u32 value out of range: {value}")
    return struct.pack("<I", value)


@dataclass(frozen=True)
class TraceEvent:
    sequence: int
    phase: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "phase": self.phase,
            "message": self.message,
            "details": self.details,
        }


class TraceLog:
    def __init__(self) -> None:
        self._events: list[TraceEvent] = []

    def add(self, phase: str, message: str, **details: Any) -> TraceEvent:
        event = TraceEvent(len(self._events), phase, message, details)
        self._events.append(event)
        return event

    def to_list(self) -> list[dict[str, Any]]:
        return [event.to_dict() for event in self._events]

    def __iter__(self):
        return iter(self._events)


@dataclass(frozen=True)
class MemoryRegion:
    name: str
    kind: str
    index: int | None
    virtual_address: int
    size: int
    permissions: frozenset[str]
    raw_address: int | None = None
    raw_size: int = 0
    file_backed_size: int = 0
    zero_fill_size: int = 0

    @property
    def virtual_end(self) -> int:
        return self.virtual_address + self.size

    def contains(self, address: int, size: int = 1) -> bool:
        return self.virtual_address <= address and address + size <= self.virtual_end

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "index": self.index,
            "virtual_address": self.virtual_address,
            "virtual_address_hex": _hex32(self.virtual_address),
            "virtual_end": self.virtual_end,
            "virtual_end_hex": _hex32(self.virtual_end),
            "size": self.size,
            "permissions": sorted(self.permissions),
            "raw_address": self.raw_address,
            "raw_address_hex": _hex32(self.raw_address)
            if self.raw_address is not None
            else None,
            "raw_size": self.raw_size,
            "file_backed_size": self.file_backed_size,
            "zero_fill_size": self.zero_fill_size,
        }


class XbeMemoryArena:
    """Bounds-checked Xbox image memory mapped into host-owned bytes."""

    def __init__(self, base_address: int, image_size: int) -> None:
        if base_address < 0 or base_address > 0xFFFFFFFF:
            raise ValueError(f"invalid base address: {base_address}")
        if image_size <= 0:
            raise ValueError(f"invalid image size: {image_size}")
        if base_address + image_size > 0x1_0000_0000:
            raise ValueError("XBE image range exceeds 32-bit address space")

        self.base_address = base_address
        self.image_size = image_size
        self.image_end = base_address + image_size
        self._data = bytearray(image_size)
        self._regions: list[MemoryRegion] = []
        self._sorted_regions: tuple[MemoryRegion, ...] | None = None

    @property
    def regions(self) -> tuple[MemoryRegion, ...]:
        if self._sorted_regions is None:
            self._sorted_regions = tuple(
                sorted(self._regions, key=lambda region: region.virtual_address)
            )
        return self._sorted_regions

    def map_region(self, region: MemoryRegion, payload: bytes) -> None:
        if region.size < 0:
            raise XbeMemoryAccessError(f"negative region size for {region.name}")
        if len(payload) > region.size:
            raise XbeMemoryAccessError(
                f"payload for {region.name} is larger than mapped region"
            )
        self._validate_image_range(region.virtual_address, region.size)
        for existing in self._regions:
            if not (
                region.virtual_end <= existing.virtual_address
                or existing.virtual_end <= region.virtual_address
            ):
                raise XbeMemoryAccessError(
                    f"region {region.name} overlaps mapped region {existing.name}"
                )

        offset = self._offset(region.virtual_address, region.size)
        self._data[offset : offset + len(payload)] = payload
        if len(payload) < region.size:
            self._data[offset + len(payload) : offset + region.size] = b"\x00" * (
                region.size - len(payload)
            )
        self._regions.append(region)
        self._sorted_regions = None

    def region_for(self, address: int, size: int = 1) -> MemoryRegion:
        self._validate_image_range(address, size)
        for region in self._regions:
            if region.contains(address, size):
                return region
        raise XbeMemoryAccessError(
            f"address range {_hex32(address)}..{_hex32(address + size)} is not mapped"
        )

    def read(self, address: int, size: int) -> bytes:
        if size < 0:
            raise XbeMemoryAccessError("cannot read a negative size")
        self.region_for(address, size)
        offset = self._offset(address, size)
        return bytes(self._data[offset : offset + size])

    def read_u32(self, address: int) -> int:
        return struct.unpack("<I", self.read(address, 4))[0]

    def write(self, address: int, payload: bytes, *, loader_patch: bool = False) -> None:
        region = self.region_for(address, len(payload))
        if not loader_patch and "write" not in region.permissions:
            raise XbeMemoryAccessError(f"region {region.name} is not writable")
        offset = self._offset(address, len(payload))
        self._data[offset : offset + len(payload)] = payload

    def write_u32(self, address: int, value: int, *, loader_patch: bool = False) -> None:
        self.write(address, _u32_bytes(value), loader_patch=loader_patch)

    def snapshot(self, address: int, size: int) -> bytes:
        """Read mapped bytes for tests and diagnostics without exposing full memory."""
        return self.read(address, size)

    def _offset(self, address: int, size: int) -> int:
        self._validate_image_range(address, size)
        return address - self.base_address

    def _validate_image_range(self, address: int, size: int) -> None:
        if size < 0:
            raise XbeMemoryAccessError("negative access size")
        if address < self.base_address or address + size > self.image_end:
            raise XbeMemoryAccessError(
                f"address range {_hex32(address)}..{_hex32(address + size)} "
                f"is outside image {_hex32(self.base_address)}..{_hex32(self.image_end)}"
            )


@dataclass(frozen=True)
class ImportBinding:
    namespace: str
    ordinal: int
    target_address: int
    name: str | None = None
    handler: Callable[..., Any] | None = None
    resolved: bool = True


@dataclass(frozen=True)
class ImportResolution:
    namespace: str
    ordinal: int
    thunk_address: int
    target_address: int
    name: str | None
    image_name: str | None
    resolved: bool
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "image_name": self.image_name,
            "ordinal": self.ordinal,
            "name": self.name,
            "thunk_address": self.thunk_address,
            "thunk_address_hex": _hex32(self.thunk_address),
            "target_address": self.target_address,
            "target_address_hex": _hex32(self.target_address),
            "resolved": self.resolved,
            "source": self.source,
        }


class ImportResolver:
    """Host-side hook registry for XBE import thunk resolution."""

    def __init__(
        self,
        *,
        stub_unresolved: bool = True,
        stub_base: int = 0xF0000000,
        stub_stride: int = 4,
    ) -> None:
        self.stub_unresolved = stub_unresolved
        self._next_stub = stub_base
        self._stub_stride = stub_stride
        self._kernel: dict[int, ImportBinding] = {}
        self._libraries: dict[tuple[str, int], ImportBinding] = {}
        self._unresolved_stubs: dict[tuple[str, str | None, int], int] = {}

    def register_kernel(
        self,
        *,
        ordinal: int | None = None,
        name: str | None = None,
        target_address: int | None = None,
        handler: Callable[..., Any] | None = None,
    ) -> int:
        if ordinal is None:
            if not name:
                raise ValueError("kernel registration needs an ordinal or name")
            ordinal = self._kernel_ordinal_for_name(name)
        resolved_name = name or KERNEL_EXPORT_NAMES.get(ordinal)
        target = self._target_or_stub(target_address)
        self._kernel[ordinal] = ImportBinding(
            "kernel", ordinal, target, resolved_name, handler, True
        )
        return target

    def register_library(
        self,
        image_name: str,
        ordinal: int,
        *,
        name: str | None = None,
        target_address: int | None = None,
        handler: Callable[..., Any] | None = None,
    ) -> int:
        key = (self._library_key(image_name), ordinal)
        target = self._target_or_stub(target_address)
        self._libraries[key] = ImportBinding(
            "library", ordinal, target, name, handler, True
        )
        return target

    def resolve_kernel(self, import_info: dict[str, Any]) -> ImportResolution:
        ordinal = import_info["ordinal"]
        binding = self._kernel.get(ordinal)
        if binding:
            return ImportResolution(
                "kernel",
                ordinal,
                import_info["thunk_address"],
                binding.target_address,
                binding.name,
                None,
                True,
                "registered",
            )

        target = self._unresolved_target("kernel", None, ordinal)
        return ImportResolution(
            "kernel",
            ordinal,
            import_info["thunk_address"],
            target,
            import_info.get("name"),
            None,
            False,
            "unresolved_stub",
        )

    def resolve_library(
        self, image_name: str, thunk_info: dict[str, Any]
    ) -> ImportResolution:
        ordinal = thunk_info["ordinal"]
        normalized_image_name = self._library_key(image_name)
        binding = self._libraries.get((normalized_image_name, ordinal))
        if binding:
            return ImportResolution(
                "library",
                ordinal,
                thunk_info["thunk_address"],
                binding.target_address,
                binding.name,
                image_name,
                True,
                "registered",
            )

        target = self._unresolved_target("library", normalized_image_name, ordinal)
        return ImportResolution(
            "library",
            ordinal,
            thunk_info["thunk_address"],
            target,
            None,
            image_name,
            False,
            "unresolved_stub",
        )

    def _target_or_stub(self, target_address: int | None) -> int:
        if target_address is not None:
            if target_address < 0 or target_address > 0xFFFFFFFF:
                raise ValueError(f"target address out of u32 range: {target_address}")
            return target_address
        return self._allocate_stub()

    def _unresolved_target(
        self, namespace: str, image_name: str | None, ordinal: int
    ) -> int:
        if not self.stub_unresolved:
            raise XbeImportResolutionError(
                f"unresolved {namespace} import {image_name or ''}!{ordinal}"
            )
        key = (namespace, image_name, ordinal)
        if key not in self._unresolved_stubs:
            self._unresolved_stubs[key] = self._allocate_stub()
        return self._unresolved_stubs[key]

    def _allocate_stub(self) -> int:
        target = self._next_stub
        if target > 0xFFFFFFFF:
            raise XbeImportResolutionError("stub address space exhausted")
        self._next_stub += self._stub_stride
        return target

    @staticmethod
    def _library_key(image_name: str) -> str:
        return image_name.casefold()

    @staticmethod
    def _kernel_ordinal_for_name(name: str) -> int:
        for ordinal, export_name in KERNEL_EXPORT_NAMES.items():
            if export_name == name:
                return ordinal
        raise ValueError(f"unknown kernel export name: {name}")


@dataclass
class LoadedXbeImage:
    info: dict[str, Any]
    arena: XbeMemoryArena
    import_resolutions: list[ImportResolution]
    trace: TraceLog

    @property
    def base_address(self) -> int:
        return self.info["header"]["base_address"]

    @property
    def entry_point(self) -> int | None:
        selected = self.info["header"]["entry_point"]["selected"]
        return selected["value"] if selected else None

    def summary(self) -> dict[str, Any]:
        unresolved = [
            resolution for resolution in self.import_resolutions if not resolution.resolved
        ]
        return {
            "title_name": self.info["certificate"]["title_name"],
            "base_address": self.base_address,
            "base_address_hex": _hex32(self.base_address),
            "image_size": self.info["header"]["image_size"],
            "entry_point": self.entry_point,
            "entry_point_hex": _hex32(self.entry_point) if self.entry_point else None,
            "mapped_regions": [region.to_dict() for region in self.arena.regions],
            "mapped_region_count": len(self.arena.regions),
            "zero_fill_total": self.info["memory_map"]["zero_fill_total"],
            "kernel_import_count": self.info["kernel_imports"]["count"],
            "non_kernel_import_count": sum(
                item["thunk_count"] for item in self.info["non_kernel_imports"]
            ),
            "patched_import_count": len(self.import_resolutions),
            "unresolved_import_count": len(unresolved),
            "unresolved_imports": [item.to_dict() for item in unresolved],
            "trace": self.trace.to_list(),
        }


def load_xbe_bytes(
    data: bytes,
    *,
    resolver: ImportResolver | None = None,
    resolve_imports: bool = True,
    allow_unresolved_imports: bool = True,
    require_valid_digests: bool = False,
) -> LoadedXbeImage:
    trace = TraceLog()
    trace.add("parse", "parse XBE metadata")
    info = parse_xbe_bytes(data)

    _trace_digest_state(info, trace, require_valid_digests)

    header = info["header"]
    arena = XbeMemoryArena(header["base_address"], header["image_size"])
    trace.add(
        "arena",
        "created controlled image arena",
        base_address=header["base_address"],
        base_address_hex=_hex32(header["base_address"]),
        image_size=header["image_size"],
    )

    _map_headers(data, info, arena, trace)
    _map_sections(data, info, arena, trace)

    import_resolutions: list[ImportResolution] = []
    if resolve_imports:
        active_resolver = resolver or ImportResolver(stub_unresolved=True)
        import_resolutions = _resolve_imports(
            info, arena, active_resolver, allow_unresolved_imports, trace
        )
    else:
        trace.add("imports", "skipped import resolution")

    loaded = LoadedXbeImage(info, arena, import_resolutions, trace)
    trace.add(
        "complete",
        "loaded XBE image skeleton",
        entry_point=loaded.entry_point,
        entry_point_hex=_hex32(loaded.entry_point) if loaded.entry_point else None,
        mapped_region_count=len(arena.regions),
        patched_import_count=len(import_resolutions),
    )
    return loaded


def load_xbe_file(path: Path, **kwargs: Any) -> LoadedXbeImage:
    return load_xbe_bytes(path.read_bytes(), **kwargs)


def _trace_digest_state(
    info: dict[str, Any], trace: TraceLog, require_valid_digests: bool
) -> None:
    mismatches = [
        section
        for section in info["sections"]
        if not section["digest_verification"]["matches"]
    ]
    if mismatches:
        trace.add(
            "digest",
            "section digest mismatch detected",
            mismatches=[
                {
                    "index": section["index"],
                    "name": section["name"],
                    "expected_sha1": section["digest_verification"]["expected_sha1"],
                    "actual_sha1": section["digest_verification"]["actual_sha1"],
                }
                for section in mismatches
            ],
        )
        if require_valid_digests:
            names = ", ".join(section["name"] for section in mismatches)
            raise XbeLoaderError(f"section digest mismatch: {names}")
    else:
        trace.add("digest", "all section digests match")


def _map_headers(
    data: bytes, info: dict[str, Any], arena: XbeMemoryArena, trace: TraceLog
) -> None:
    header = info["header"]
    headers_size = header["headers_size"]
    payload = data[:headers_size]
    if len(payload) != headers_size:
        raise XbeLoaderError("XBE header bytes are truncated")
    region = MemoryRegion(
        "$headers",
        "headers",
        None,
        header["base_address"],
        headers_size,
        frozenset({"read"}),
        0,
        headers_size,
        headers_size,
        0,
    )
    arena.map_region(region, payload)
    trace.add(
        "map_headers",
        "mapped XBE headers",
        virtual_address=region.virtual_address,
        virtual_address_hex=_hex32(region.virtual_address),
        size=region.size,
    )


def _map_sections(
    data: bytes, info: dict[str, Any], arena: XbeMemoryArena, trace: TraceLog
) -> None:
    for section in info["sections"]:
        file_backed_size = min(section["raw_size"], section["virtual_size"])
        raw_start = section["raw_address"]
        raw_end = raw_start + file_backed_size
        if raw_end > len(data):
            raise XbeLoaderError(f"section {section['name']} raw bytes are truncated")
        payload = data[raw_start:raw_end]
        region = MemoryRegion(
            section["name"],
            "section",
            section["index"],
            section["virtual_address"],
            section["virtual_size"],
            _permissions_for_section(section),
            section["raw_address"],
            section["raw_size"],
            file_backed_size,
            section["zero_fill_size"],
        )
        arena.map_region(region, payload)
        trace.add(
            "map_section",
            "mapped XBE section",
            index=section["index"],
            name=section["name"],
            virtual_address=region.virtual_address,
            virtual_address_hex=_hex32(region.virtual_address),
            virtual_size=region.size,
            file_backed_size=file_backed_size,
            zero_fill_size=region.zero_fill_size,
            permissions=sorted(region.permissions),
        )
        if section["raw_overflow_size"]:
            trace.add(
                "map_section",
                "ignored raw bytes beyond virtual section size",
                index=section["index"],
                name=section["name"],
                raw_overflow_size=section["raw_overflow_size"],
            )


def _permissions_for_section(section: dict[str, Any]) -> frozenset[str]:
    permissions = {"read"}
    names = set(section["flag_names"])
    if "WRITABLE" in names:
        permissions.add("write")
    if "EXECUTABLE" in names:
        permissions.add("execute")
    return frozenset(permissions)


def _resolve_imports(
    info: dict[str, Any],
    arena: XbeMemoryArena,
    resolver: ImportResolver,
    allow_unresolved_imports: bool,
    trace: TraceLog,
) -> list[ImportResolution]:
    resolutions: list[ImportResolution] = []
    kernel_imports = info["kernel_imports"]["imports"]
    trace.add("resolve_kernel_imports", "resolving kernel imports", count=len(kernel_imports))
    for import_info in kernel_imports:
        resolution = resolver.resolve_kernel(import_info)
        _apply_resolution(arena, resolution, allow_unresolved_imports)
        resolutions.append(resolution)
        trace.add(
            "resolve_kernel_import",
            "patched kernel import thunk",
            ordinal=resolution.ordinal,
            name=resolution.name,
            thunk_address=resolution.thunk_address,
            thunk_address_hex=_hex32(resolution.thunk_address),
            target_address=resolution.target_address,
            target_address_hex=_hex32(resolution.target_address),
            resolved=resolution.resolved,
            source=resolution.source,
        )

    library_imports = info["non_kernel_imports"]
    trace.add(
        "resolve_library_imports",
        "resolving non-kernel imports",
        descriptor_count=len(library_imports),
        thunk_count=sum(item["thunk_count"] for item in library_imports),
    )
    for descriptor in library_imports:
        image_name = descriptor["image_name"]
        for thunk in descriptor["thunks"]:
            resolution = resolver.resolve_library(image_name, thunk)
            _apply_resolution(arena, resolution, allow_unresolved_imports)
            resolutions.append(resolution)
            trace.add(
                "resolve_library_import",
                "patched library import thunk",
                image_name=image_name,
                ordinal=resolution.ordinal,
                thunk_address=resolution.thunk_address,
                thunk_address_hex=_hex32(resolution.thunk_address),
                target_address=resolution.target_address,
                target_address_hex=_hex32(resolution.target_address),
                resolved=resolution.resolved,
                source=resolution.source,
            )
    return resolutions


def _apply_resolution(
    arena: XbeMemoryArena, resolution: ImportResolution, allow_unresolved: bool
) -> None:
    if not resolution.resolved and not allow_unresolved:
        label = resolution.image_name or "xboxkrnl.exe"
        raise XbeImportResolutionError(
            f"unresolved import {label}!{resolution.ordinal} at "
            f"{_hex32(resolution.thunk_address)}"
        )
    arena.write_u32(
        resolution.thunk_address, resolution.target_address, loader_patch=True
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Map an Xbox XBE into the loader skeleton and emit a summary."
    )
    parser.add_argument("xbe", type=Path, help="Path to the XBE file to load.")
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    parser.add_argument(
        "--strict-digests",
        action="store_true",
        help="Fail if any section digest does not match.",
    )
    parser.add_argument(
        "--no-imports",
        action="store_true",
        help="Map memory but do not patch import thunks.",
    )
    parser.add_argument(
        "--fail-unresolved",
        action="store_true",
        help="Fail instead of assigning synthetic stubs to unresolved imports.",
    )
    args = parser.parse_args()

    resolver = ImportResolver(stub_unresolved=not args.fail_unresolved)
    loaded = load_xbe_file(
        args.xbe,
        resolver=resolver,
        resolve_imports=not args.no_imports,
        allow_unresolved_imports=not args.fail_unresolved,
        require_valid_digests=args.strict_digests,
    )
    print(json.dumps(loaded.summary(), indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
