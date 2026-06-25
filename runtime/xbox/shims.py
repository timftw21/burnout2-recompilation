#!/usr/bin/env python3
"""Deterministic host-side Xbox runtime shims.

The shims in this module are intentionally callable Python models, not a CPU
ABI bridge. The loader can bind import thunks to these handlers today, while a
future executor/recompiler can adapt guest calling conventions to the same
subsystem methods.
"""

from __future__ import annotations

import hashlib
import itertools
import datetime as dt
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any

from tools.loader.xbe_loader import ImportResolver
from tools.xbe.xbe_info import KERNEL_EXPORT_NAMES


class XboxRuntimeError(RuntimeError):
    """Base error raised by runtime shim components."""


class XboxPathError(XboxRuntimeError):
    """Raised when a guest path cannot be resolved safely."""


class XboxStatus:
    """NTSTATUS-style status values used by shim return models."""

    SUCCESS = 0x00000000
    WAIT_0 = 0x00000000
    WAIT_TIMEOUT = 0x00000102
    PENDING = 0x00000103
    NOT_IMPLEMENTED = 0xC0000002
    INVALID_HANDLE = 0xC0000008
    INVALID_PARAMETER = 0xC000000D
    NO_SUCH_FILE = 0xC000000F
    ACCESS_DENIED = 0xC0000022
    OBJECT_NAME_COLLISION = 0xC0000035
    OBJECT_NAME_NOT_FOUND = 0xC0000034
    END_OF_FILE = 0xC0000011
    TIMEOUT = 0x00000102


def _hex32(value: int) -> str:
    return f"0x{value & 0xFFFFFFFF:08X}"


def _align_up(value: int, alignment: int) -> int:
    if alignment <= 0:
        raise ValueError("alignment must be positive")
    return (value + alignment - 1) & ~(alignment - 1)


def _i64_from_u32_parts(low: int, high: int) -> int:
    value = ((high & 0xFFFFFFFF) << 32) | (low & 0xFFFFFFFF)
    return value - 0x1_0000_0000_0000_0000 if value & 0x8000_0000_0000_0000 else value


@dataclass(frozen=True)
class ShimTraceEvent:
    sequence: int
    subsystem: str
    operation: str
    status: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "subsystem": self.subsystem,
            "operation": self.operation,
            "status": self.status,
            "details": self.details,
        }


class ShimTraceLog:
    """Structured runtime trace shared by every shim subsystem."""

    def __init__(self) -> None:
        self._events: list[ShimTraceEvent] = []

    def add(
        self, subsystem: str, operation: str, status: str = "ok", **details: Any
    ) -> ShimTraceEvent:
        event = ShimTraceEvent(
            len(self._events), subsystem, operation, status, _json_safe(details)
        )
        self._events.append(event)
        return event

    def to_list(self) -> list[dict[str, Any]]:
        return [event.to_dict() for event in self._events]

    def __iter__(self):
        return iter(self._events)


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, bytes):
        return {"bytes": len(value), "hex": value.hex().upper()}
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


@dataclass(frozen=True)
class RuntimeShim:
    ordinal: int
    name: str
    subsystem: str
    target_address: int
    behavior: str
    handler: Callable[..., Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "name": self.name,
            "subsystem": self.subsystem,
            "target_address": self.target_address,
            "target_address_hex": _hex32(self.target_address),
            "behavior": self.behavior,
        }


@dataclass(frozen=True)
class XboxRuntimeConfig:
    extracted_disc_root: Path | None = None
    save_data_root: Path | None = None
    dashboard_data_root: Path | None = None
    cache_data_root: Path | None = None
    host_target_base: int = 0xE0000000
    host_target_stride: int = 0x10
    allocation_base: int = 0x10000000
    contiguous_allocation_base: int = 0x20000000
    performance_frequency: int = 10_000_000
    system_time_filetime: int = 116444736000000000


@dataclass
class GuestFile:
    guest_path: str
    host_path: Path
    mode: str
    root_kind: str
    position: int = 0
    size: int = 0
    read_count: int = 0
    bytes_read: int = 0
    write_count: int = 0
    bytes_written: int = 0
    writable: bool = False
    streaming: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "guest_path": self.guest_path,
            "host_path": str(self.host_path),
            "mode": self.mode,
            "root_kind": self.root_kind,
            "position": self.position,
            "size": self.size,
            "read_count": self.read_count,
            "bytes_read": self.bytes_read,
            "write_count": self.write_count,
            "bytes_written": self.bytes_written,
            "writable": self.writable,
            "streaming": self.streaming,
        }


@dataclass
class KernelVariable:
    name: str
    value: Any
    type_name: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type_name": self.type_name,
            "value": self.value,
        }


@dataclass
class DeviceObject:
    name: str | None
    device_type: int = 0
    deleted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "device_type": self.device_type,
            "deleted": self.deleted,
        }


@dataclass
class IoRequest:
    major_function: str
    target: Any = None
    status: int = XboxStatus.SUCCESS
    information: int = 0
    must_complete: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "major_function": self.major_function,
            "status": self.status,
            "status_hex": _hex32(self.status),
            "information": self.information,
            "must_complete": self.must_complete,
        }


class GuestHandleTable:
    """Deterministic handle allocator for kernel object shims."""

    def __init__(self, trace: ShimTraceLog, *, first_handle: int = 0x100) -> None:
        self._trace = trace
        self._next_handle = first_handle
        self._objects: dict[int, tuple[str, Any]] = {}

    def allocate(self, kind: str, value: Any) -> int:
        handle = self._next_handle
        self._next_handle += 4
        self._objects[handle] = (kind, value)
        self._trace.add("object_manager", "allocate_handle", kind=kind, handle=handle)
        return handle

    def get(self, handle: int, kind: str | None = None) -> Any:
        entry = self._objects.get(handle)
        if entry is None:
            raise XboxRuntimeError(f"invalid guest handle: {_hex32(handle)}")
        actual_kind, value = entry
        if kind is not None and actual_kind != kind:
            raise XboxRuntimeError(
                f"guest handle {_hex32(handle)} is {actual_kind}, expected {kind}"
            )
        return value

    def close(self, handle: int) -> int:
        if handle not in self._objects:
            self._trace.add(
                "object_manager", "close_handle", "error", handle=handle
            )
            return XboxStatus.INVALID_HANDLE
        kind, _ = self._objects.pop(handle)
        self._trace.add("object_manager", "close_handle", kind=kind, handle=handle)
        return XboxStatus.SUCCESS

    def snapshot(self) -> dict[str, Any]:
        kinds = Counter(kind for kind, _ in self._objects.values())
        return {
            "open_handle_count": len(self._objects),
            "open_handle_kinds": dict(sorted(kinds.items())),
            "next_handle": self._next_handle,
        }


class XboxFileSystemShim:
    """Guest filesystem view rooted at local disc and save-data directories."""

    _DISC_PREFIXES = (
        "\\Device\\Cdrom0\\",
        "\\Device\\CdRom0\\",
        "\\??\\D:\\",
        "D:\\",
        "Cdrom0:\\",
    )
    _SAVE_PREFIXES = (
        "\\Device\\Harddisk0\\Partition0\\",
        "\\Device\\Harddisk0\\Partition1\\",
        "\\??\\E:\\",
        "E:\\",
    )
    _DASHBOARD_PREFIXES = (
        "\\Device\\Harddisk0\\Partition2\\",
        "\\??\\C:\\",
        "C:\\",
    )
    _CACHE_PREFIXES = (
        "\\Device\\Harddisk0\\Partition3\\",
        "\\Device\\Harddisk0\\Partition4\\",
        "\\Device\\Harddisk0\\Partition5\\",
        "\\??\\X:\\",
        "\\??\\Y:\\",
        "\\??\\Z:\\",
        "X:\\",
        "Y:\\",
        "Z:\\",
    )
    _DEVICE_PREFIXES = (
        _DISC_PREFIXES
        + _SAVE_PREFIXES
        + _DASHBOARD_PREFIXES
        + _CACHE_PREFIXES
    )

    def __init__(
        self,
        trace: ShimTraceLog,
        handles: GuestHandleTable,
        extracted_disc_root: Path | None,
        save_data_root: Path | None = None,
        dashboard_data_root: Path | None = None,
        cache_data_root: Path | None = None,
        clock: "XboxClockShim | None" = None,
    ) -> None:
        self._trace = trace
        self._handles = handles
        self._root = extracted_disc_root.resolve() if extracted_disc_root else None
        self._save_root = save_data_root.resolve() if save_data_root else None
        self._dashboard_root = (
            dashboard_data_root.resolve() if dashboard_data_root else None
        )
        self._cache_root = cache_data_root.resolve() if cache_data_root else None
        self._clock = clock

    @property
    def root(self) -> Path | None:
        return self._root

    @property
    def save_root(self) -> Path | None:
        return self._save_root

    @property
    def dashboard_root(self) -> Path | None:
        return self._dashboard_root

    @property
    def cache_root(self) -> Path | None:
        return self._cache_root

    def resolve_guest_path(self, guest_path: str, *, for_write: bool = False) -> Path:
        root_kind = self._root_kind_for_guest_path(guest_path)
        root = self._root_for_guest_path(guest_path, for_write=for_write)
        if root is None:
            raise XboxPathError(f"no host root configured for {root_kind} volume")
        parts = self._guest_path_parts(guest_path)
        current = root
        for part in parts:
            if current.exists() and current.is_dir():
                matches = {
                    child.name.casefold(): child for child in current.iterdir()
                }
                current = matches.get(part.casefold(), current / part)
            else:
                current = current / part

        resolved = current.resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise XboxPathError(f"guest path escapes extracted root: {guest_path}") from exc
        self._trace.add(
            "filesystem",
            "resolve_path",
            guest_path=guest_path,
            host_path=resolved,
            root_kind=root_kind,
            for_write=for_write,
        )
        return resolved

    def query_file_information(self, guest_path: str) -> dict[str, Any]:
        root_kind = self._root_kind_for_guest_path(guest_path)
        try:
            host_path = self.resolve_guest_path(guest_path)
        except XboxPathError as exc:
            self._trace.add(
                "filesystem", "query_file", "error", guest_path=guest_path, error=str(exc)
            )
            return {
                "status": XboxStatus.NO_SUCH_FILE,
                "root_kind": root_kind,
                "error": str(exc),
            }

        exists = host_path.exists()
        info = {
            "status": XboxStatus.SUCCESS if exists else XboxStatus.OBJECT_NAME_NOT_FOUND,
            "guest_path": guest_path,
            "host_path": str(host_path),
            "root_kind": root_kind,
            "exists": exists,
            "is_directory": host_path.is_dir() if exists else False,
            "size": host_path.stat().st_size if exists and host_path.is_file() else 0,
        }
        self._trace.add("filesystem", "query_file", **info)
        return info

    def query_file_handle_information(self, handle: int) -> dict[str, Any]:
        try:
            file = self._handles.get(handle, "file")
        except XboxRuntimeError as exc:
            return {"status": XboxStatus.INVALID_HANDLE, "error": str(exc)}
        file.size = file.host_path.stat().st_size if file.host_path.exists() else 0
        info = {
            "status": XboxStatus.SUCCESS,
            "handle": handle,
            "guest_path": file.guest_path,
            "host_path": str(file.host_path),
            "exists": file.host_path.exists(),
            "is_directory": False,
            "size": file.size,
            "position": file.position,
            "writable": file.writable,
            "save_data": self._is_save_path(file.guest_path),
        }
        self._trace.add("filesystem", "query_file_handle", **info)
        return info

    def set_file_handle_information(
        self,
        handle: int,
        file_information_class: int,
        payload: bytes = b"",
    ) -> dict[str, Any]:
        try:
            file = self._handles.get(handle, "file")
        except XboxRuntimeError as exc:
            return {"status": XboxStatus.INVALID_HANDLE, "error": str(exc)}
        result: dict[str, Any] = {
            "status": XboxStatus.SUCCESS,
            "handle": handle,
            "guest_path": file.guest_path,
            "file_information_class": file_information_class,
            "bytes_consumed": 0,
        }
        if file_information_class == 14:
            if len(payload) < 8:
                result["status"] = XboxStatus.INVALID_PARAMETER
            else:
                offset = int.from_bytes(payload[:8], "little", signed=True)
                if offset < 0:
                    result["status"] = XboxStatus.INVALID_PARAMETER
                else:
                    file.position = offset
                    result["position"] = file.position
                    result["bytes_consumed"] = 8
        elif file_information_class in {19, 20}:
            result["ignored"] = True
            result["bytes_consumed"] = min(len(payload), 8)
        else:
            result["ignored"] = True
        self._trace.add(
            "filesystem",
            "set_file_handle_information",
            handle=handle,
            guest_path=file.guest_path,
            file_information_class=file_information_class,
            status_hex=_hex32(result["status"]),
            position=file.position,
            bytes_consumed=result["bytes_consumed"],
            ignored=result.get("ignored", False),
        )
        return result

    def query_volume_handle_information(self, handle: int) -> dict[str, Any]:
        try:
            file = self._handles.get(handle, "file")
        except XboxRuntimeError as exc:
            return {"status": XboxStatus.INVALID_HANDLE, "error": str(exc)}
        return self.query_volume_information(file.guest_path)

    def open_file(self, guest_path: str, mode: str = "rb") -> dict[str, Any]:
        if mode not in {"r", "rb", "w", "wb", "a", "ab"}:
            self._trace.add(
                "filesystem", "open_file", "error", guest_path=guest_path, mode=mode
            )
            return {"status": XboxStatus.ACCESS_DENIED, "handle": None}
        writable = "w" in mode or "a" in mode
        root_kind = self._root_kind_for_guest_path(guest_path)
        if writable and root_kind not in {"save", "cache"}:
            self._trace.add(
                "filesystem",
                "open_file",
                "error",
                guest_path=guest_path,
                mode=mode,
                root_kind=root_kind,
                reason="root_is_read_only",
            )
            return {"status": XboxStatus.ACCESS_DENIED, "handle": None, "root_kind": root_kind}
        try:
            host_path = self.resolve_guest_path(guest_path, for_write=writable)
        except XboxPathError as exc:
            self._trace.add(
                "filesystem",
                "open_file",
                "error",
                guest_path=guest_path,
                mode=mode,
                root_kind=root_kind,
                error=str(exc),
            )
            return {
                "status": XboxStatus.NO_SUCH_FILE,
                "handle": None,
                "root_kind": root_kind,
                "error": str(exc),
            }
        if writable:
            host_path.parent.mkdir(parents=True, exist_ok=True)
            if "w" in mode:
                host_path.write_bytes(b"")
            elif not host_path.exists():
                host_path.write_bytes(b"")
        if not host_path.is_file():
            self._trace.add(
                "filesystem",
                "open_file",
                "error",
                guest_path=guest_path,
                host_path=host_path,
                root_kind=root_kind,
            )
            return {"status": XboxStatus.NO_SUCH_FILE, "handle": None, "root_kind": root_kind}

        size = host_path.stat().st_size
        file = GuestFile(
            guest_path,
            host_path,
            mode,
            root_kind,
            size=size,
            writable=writable,
            streaming=not writable,
        )
        handle = self._handles.allocate("file", file)
        self._trace.add(
            "filesystem",
            "open_file",
            guest_path=guest_path,
            host_path=host_path,
            handle=handle,
            size=size,
            root_kind=root_kind,
            writable=writable,
            save_data=self._is_save_path(guest_path),
            cache_data=root_kind == "cache",
        )
        return {"status": XboxStatus.SUCCESS, "handle": handle, "root_kind": root_kind}

    def read_file(
        self, handle: int, size: int | None = None, *, offset: int | None = None
    ) -> dict[str, Any]:
        try:
            file = self._handles.get(handle, "file")
        except XboxRuntimeError as exc:
            return {"status": XboxStatus.INVALID_HANDLE, "data": b"", "error": str(exc)}
        if offset is not None:
            if offset < 0:
                return {"status": XboxStatus.INVALID_PARAMETER, "data": b""}
            file.position = offset

        file.size = file.host_path.stat().st_size if file.host_path.exists() else 0
        start = min(file.position, file.size)
        requested = file.size - start if size is None else max(size, 0)
        with file.host_path.open("rb") as host_file:
            host_file.seek(start)
            chunk = host_file.read(requested)
        end = start + len(chunk)
        file.position = end
        file.read_count += 1
        file.bytes_read += len(chunk)
        latency_100ns = self._stream_latency_100ns(len(chunk), requested)
        if self._clock is not None:
            self._clock.advance_100ns(latency_100ns)
        status = XboxStatus.SUCCESS if chunk or start < file.size else XboxStatus.END_OF_FILE
        self._trace.add(
            "filesystem",
            "read_file",
            handle=handle,
            guest_path=file.guest_path,
            offset=start,
            requested_size=size,
            bytes_read=len(chunk),
            next_offset=file.position,
            file_size=file.size,
            stream_read_index=file.read_count,
            deterministic_latency_100ns=latency_100ns,
            end_of_file=status == XboxStatus.END_OF_FILE,
            status_hex=_hex32(status),
        )
        return {"status": status, "data": chunk, "bytes_read": len(chunk)}

    def write_file(
        self, handle: int, payload: bytes, *, offset: int | None = None
    ) -> dict[str, Any]:
        try:
            file = self._handles.get(handle, "file")
        except XboxRuntimeError as exc:
            return {"status": XboxStatus.INVALID_HANDLE, "bytes_written": 0, "error": str(exc)}
        if "w" not in file.mode and "a" not in file.mode:
            self._trace.add(
                "filesystem", "write_file", "error", handle=handle, mode=file.mode
            )
            return {"status": XboxStatus.ACCESS_DENIED, "bytes_written": 0}
        if offset is not None:
            if offset < 0:
                return {"status": XboxStatus.INVALID_PARAMETER, "bytes_written": 0}
            file.position = offset
        start = file.position
        current = bytearray(file.host_path.read_bytes() if file.host_path.exists() else b"")
        if start > len(current):
            current.extend(b"\x00" * (start - len(current)))
        end = start + len(payload)
        current[start:end] = payload
        file.host_path.parent.mkdir(parents=True, exist_ok=True)
        file.host_path.write_bytes(bytes(current))
        file.position = end
        file.size = len(current)
        file.write_count += 1
        file.bytes_written += len(payload)
        self._trace.add(
            "filesystem",
            "write_file",
            handle=handle,
            guest_path=file.guest_path,
            offset=start,
            bytes_written=len(payload),
            file_size=file.size,
            write_index=file.write_count,
            save_data=self._is_save_path(file.guest_path),
        )
        return {"status": XboxStatus.SUCCESS, "bytes_written": len(payload)}

    def delete_file(self, guest_path: str) -> int:
        root_kind = self._root_kind_for_guest_path(guest_path)
        if root_kind not in {"save", "cache"}:
            self._trace.add(
                "filesystem",
                "delete_file",
                "error",
                guest_path=guest_path,
                root_kind=root_kind,
                reason="root_is_read_only",
            )
            return XboxStatus.ACCESS_DENIED
        try:
            host_path = self.resolve_guest_path(guest_path, for_write=True)
        except XboxPathError as exc:
            self._trace.add(
                "filesystem", "delete_file", "error", guest_path=guest_path, error=str(exc)
            )
            return XboxStatus.OBJECT_NAME_NOT_FOUND
        if not host_path.exists():
            return XboxStatus.OBJECT_NAME_NOT_FOUND
        try:
            if host_path.is_dir():
                host_path.rmdir()
            else:
                host_path.unlink()
        except OSError:
            self._trace.add("filesystem", "delete_file", "error", guest_path=guest_path)
            return XboxStatus.ACCESS_DENIED
        self._trace.add("filesystem", "delete_file", guest_path=guest_path, host_path=host_path)
        return XboxStatus.SUCCESS

    def query_volume_information(self, guest_path: str = "D:\\") -> dict[str, Any]:
        root_kind = self._root_kind_for_guest_path(guest_path)
        try:
            host_path = self.resolve_guest_path(guest_path)
        except XboxPathError as exc:
            return {
                "status": XboxStatus.NO_SUCH_FILE,
                "root_kind": root_kind,
                "error": str(exc),
            }
        root = host_path if host_path.is_dir() else host_path.parent
        total_bytes = 0
        file_count = 0
        if root.exists():
            for child in root.rglob("*"):
                if child.is_file():
                    file_count += 1
                    total_bytes += child.stat().st_size
        result = {
            "status": XboxStatus.SUCCESS,
            "label": "BURNOUT2",
            "serial_number": 0x41430019,
            "file_count": file_count,
            "used_bytes": total_bytes,
            "root_kind": root_kind,
        }
        self._trace.add("filesystem", "query_volume", guest_path=guest_path, result=result)
        return result

    def list_directory(self, guest_path: str = "D:\\") -> dict[str, Any]:
        root_kind = self._root_kind_for_guest_path(guest_path)
        try:
            host_path = self.resolve_guest_path(guest_path)
        except XboxPathError as exc:
            return {
                "status": XboxStatus.NO_SUCH_FILE,
                "entries": [],
                "root_kind": root_kind,
                "error": str(exc),
            }
        if not host_path.is_dir():
            return {"status": XboxStatus.NO_SUCH_FILE, "entries": [], "root_kind": root_kind}
        entries = sorted(child.name for child in host_path.iterdir())
        self._trace.add(
            "filesystem",
            "list_directory",
            guest_path=guest_path,
            root_kind=root_kind,
            entry_count=len(entries),
        )
        return {"status": XboxStatus.SUCCESS, "entries": entries, "root_kind": root_kind}

    def _guest_path_parts(self, guest_path: str) -> tuple[str, ...]:
        if not guest_path:
            return ()
        normalized = guest_path.replace("/", "\\").strip()
        for prefix in self._DEVICE_PREFIXES:
            if normalized.casefold().startswith(prefix.casefold()):
                normalized = normalized[len(prefix) :]
                break
        if len(normalized) >= 2 and normalized[1] == ":":
            normalized = normalized[2:]
        normalized = normalized.lstrip("\\")
        pure = PureWindowsPath(normalized)
        parts = []
        for part in pure.parts:
            if part in {"", ".", "\\"}:
                continue
            if part == "..":
                raise XboxPathError(f"guest path traversal is not allowed: {guest_path}")
            if "\\" in part or "/" in part:
                raise XboxPathError(f"invalid guest path segment: {part}")
            parts.append(part)
        return tuple(parts)

    def _root_for_guest_path(self, guest_path: str, *, for_write: bool = False) -> Path | None:
        root_kind = self._root_kind_for_guest_path(guest_path)
        if root_kind == "save":
            return self._save_root
        if root_kind == "dashboard":
            return self._dashboard_root
        if root_kind == "cache":
            return self._cache_root
        return self._root

    def _root_kind_for_guest_path(self, guest_path: str) -> str:
        normalized = guest_path.replace("/", "\\").strip()
        folded = normalized.casefold()
        if any(folded.startswith(prefix.casefold()) for prefix in self._DISC_PREFIXES):
            return "disc"
        if any(folded.startswith(prefix.casefold()) for prefix in self._SAVE_PREFIXES):
            return "save"
        if any(folded.startswith(prefix.casefold()) for prefix in self._DASHBOARD_PREFIXES):
            return "dashboard"
        if any(folded.startswith(prefix.casefold()) for prefix in self._CACHE_PREFIXES):
            return "cache"
        if len(normalized) >= 2 and normalized[1] == ":":
            drive = normalized[0].casefold()
            if drive == "d":
                return "disc"
            if drive == "e":
                return "save"
            if drive == "c":
                return "dashboard"
            if drive in {"x", "y", "z"}:
                return "cache"
        return "save" if self._is_save_path(guest_path) else "disc"

    def _is_save_path(self, guest_path: str) -> bool:
        normalized = guest_path.replace("/", "\\").strip()
        folded = normalized.casefold()
        if any(folded.startswith(prefix.casefold()) for prefix in self._SAVE_PREFIXES):
            return True
        stripped = normalized
        for prefix in self._DISC_PREFIXES:
            if stripped.casefold().startswith(prefix.casefold()):
                stripped = stripped[len(prefix) :]
                break
        stripped = stripped.lstrip("\\")
        first = stripped.split("\\", 1)[0].casefold()
        return first in {"udata", "tdata", "saves"}

    @staticmethod
    def _stream_latency_100ns(bytes_read: int, requested: int) -> int:
        transfer = max(bytes_read, min(requested, 4096))
        return 500 + ((transfer + 4095) // 4096) * 250

    def open_file_snapshot(self) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        for handle, (kind, file) in sorted(self._handles._objects.items()):
            if kind != "file":
                continue
            data = file.to_dict()
            data["handle"] = handle
            data["handle_hex"] = _hex32(handle)
            data["save_data"] = self._is_save_path(file.guest_path)
            data["cache_data"] = file.root_kind == "cache"
            data["dashboard_data"] = file.root_kind == "dashboard"
            files.append(data)
        return files


@dataclass
class Allocation:
    address: int
    size: int
    kind: str
    tag: int | None = None
    protection: str = "rw"
    data: bytearray = field(default_factory=bytearray)

    @property
    def end_address(self) -> int:
        return self.address + self.size

    def contains(self, address: int, size: int) -> bool:
        return self.address <= address and address + size <= self.end_address

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "address_hex": _hex32(self.address),
            "size": self.size,
            "kind": self.kind,
            "tag": self.tag,
            "tag_hex": _hex32(self.tag) if self.tag is not None else None,
            "protection": self.protection,
        }


class XboxMemoryShim:
    """Deterministic virtual allocation and byte storage model."""

    def __init__(self, trace: ShimTraceLog, config: XboxRuntimeConfig) -> None:
        self._trace = trace
        self._next_pool = config.allocation_base
        self._next_contiguous = config.contiguous_allocation_base
        self._allocations: dict[int, Allocation] = {}
        self._locked_ranges: set[tuple[int, int]] = set()

    @property
    def allocations(self) -> tuple[Allocation, ...]:
        return tuple(sorted(self._allocations.values(), key=lambda item: item.address))

    def allocate_pool(self, size: int, tag: int | None = None) -> int:
        address = self._allocate(size, 0x10, "pool", tag)
        self._trace.add("allocator", "allocate_pool", address=address, size=size, tag=tag)
        return address

    def allocate_system_memory(self, size: int, protection: str = "rw") -> int:
        address = self._allocate(size, 0x1000, "system", None, protection=protection)
        self._trace.add(
            "allocator", "allocate_system_memory", address=address, size=size
        )
        return address

    def allocate_contiguous_memory(
        self,
        size: int,
        *,
        alignment: int = 0x1000,
        kind: str = "contiguous",
        protection: str = "rw",
    ) -> int:
        if size <= 0:
            raise XboxRuntimeError("allocation size must be positive")
        address = _align_up(self._next_contiguous, alignment)
        self._next_contiguous = _align_up(address + size, alignment)
        self._allocations[address] = Allocation(
            address, size, kind, None, protection, bytearray(size)
        )
        self._trace.add(
            "allocator",
            "allocate_contiguous",
            address=address,
            size=size,
            alignment=alignment,
            kind=kind,
        )
        return address

    def free(self, address: int) -> int:
        allocation = self._allocations.pop(address, None)
        if allocation is None:
            self._trace.add("allocator", "free", "error", address=address)
            return XboxStatus.INVALID_PARAMETER
        self._trace.add(
            "allocator",
            "free",
            address=allocation.address,
            size=allocation.size,
            kind=allocation.kind,
        )
        return XboxStatus.SUCCESS

    def read(self, address: int, size: int) -> bytes:
        allocation = self._find_allocation(address, size)
        offset = address - allocation.address
        return bytes(allocation.data[offset : offset + size])

    def write(self, address: int, payload: bytes) -> None:
        allocation = self._find_allocation(address, len(payload))
        if "w" not in allocation.protection:
            raise XboxRuntimeError(f"allocation at {_hex32(address)} is not writable")
        offset = address - allocation.address
        allocation.data[offset : offset + len(payload)] = payload
        self._trace.add(
            "allocator",
            "write",
            address=address,
            size=len(payload),
            allocation=allocation.address,
        )

    def fill(self, address: int, value: int, size: int) -> None:
        self.write(address, bytes([value & 0xFF]) * size)
        self._trace.add("runtime", "fill_memory", address=address, value=value, size=size)

    def zero(self, address: int, size: int) -> None:
        self.fill(address, 0, size)
        self._trace.add("runtime", "zero_memory", address=address, size=size)

    def move(self, destination: int, source: int, size: int) -> None:
        payload = self.read(source, size)
        self.write(destination, payload)
        self._trace.add(
            "runtime", "move_memory", destination=destination, source=source, size=size
        )

    def protect(self, address: int, size: int, protection: str) -> dict[str, Any]:
        allocation = self._find_allocation(address, size)
        old = allocation.protection
        allocation.protection = protection
        self._trace.add(
            "allocator",
            "protect",
            address=address,
            size=size,
            old_protection=old,
            new_protection=protection,
        )
        return {"status": XboxStatus.SUCCESS, "old_protection": old}

    def query_protection(self, address: int) -> dict[str, Any]:
        allocation = self._find_allocation(address, 1)
        result = {
            "status": XboxStatus.SUCCESS,
            "protection": allocation.protection,
            "address": allocation.address,
            "size": allocation.size,
        }
        self._trace.add("allocator", "query_protection", query_address=address, result=result)
        return result

    def query_allocation_size(self, address: int) -> dict[str, Any]:
        allocation = self._find_allocation(address, 1)
        result = {"status": XboxStatus.SUCCESS, "size": allocation.size}
        self._trace.add("allocator", "query_allocation_size", address=address, result=result)
        return result

    def query_statistics(self) -> dict[str, Any]:
        by_kind = Counter(allocation.kind for allocation in self._allocations.values())
        committed = sum(allocation.size for allocation in self._allocations.values())
        result = {
            "status": XboxStatus.SUCCESS,
            "allocation_count": len(self._allocations),
            "committed_bytes": committed,
            "allocation_kinds": dict(sorted(by_kind.items())),
            "locked_range_count": len(self._locked_ranges),
        }
        self._trace.add("allocator", "query_statistics", result=result)
        return result

    def map_io_space(self, physical_address: int, size: int, protection: str = "rw") -> int:
        address = self.allocate_contiguous_memory(size, kind="io", protection=protection)
        self._trace.add(
            "allocator",
            "map_io_space",
            physical_address=physical_address,
            address=address,
            size=size,
            protection=protection,
        )
        return address

    def unmap_io_space(self, address: int, size: int | None = None) -> int:
        self._trace.add("allocator", "unmap_io_space", address=address, size=size)
        return self.free(address)

    def lock_unlock_buffer_pages(self, address: int, size: int, lock: bool = True) -> int:
        self._find_allocation(address, size)
        item = (address, size)
        if lock:
            self._locked_ranges.add(item)
        else:
            self._locked_ranges.discard(item)
        self._trace.add(
            "allocator",
            "lock_unlock_buffer_pages",
            address=address,
            size=size,
            lock=lock,
        )
        return XboxStatus.SUCCESS

    def persist_contiguous_memory(self, address: int, size: int, persist: bool = True) -> int:
        self._find_allocation(address, size)
        self._trace.add(
            "allocator",
            "persist_contiguous_memory",
            address=address,
            size=size,
            persist=persist,
        )
        return XboxStatus.SUCCESS

    def query(self, address: int) -> dict[str, Any]:
        for allocation in self._allocations.values():
            if allocation.contains(address, 1):
                result = {"status": XboxStatus.SUCCESS, **allocation.to_dict()}
                self._trace.add("allocator", "query", address=address, result=result)
                return result
        self._trace.add("allocator", "query", "error", address=address)
        return {"status": XboxStatus.INVALID_PARAMETER}

    def get_physical_address(self, address: int) -> int:
        self._find_allocation(address, 1)
        self._trace.add("allocator", "get_physical_address", address=address)
        return address

    def _allocate(
        self,
        size: int,
        alignment: int,
        kind: str,
        tag: int | None,
        *,
        protection: str = "rw",
    ) -> int:
        if size <= 0:
            raise XboxRuntimeError("allocation size must be positive")
        address = _align_up(self._next_pool, alignment)
        self._next_pool = _align_up(address + size, alignment)
        self._allocations[address] = Allocation(
            address, size, kind, tag, protection, bytearray(size)
        )
        return address

    def _find_allocation(self, address: int, size: int) -> Allocation:
        if size < 0:
            raise XboxRuntimeError("negative memory access size")
        for allocation in self._allocations.values():
            if allocation.contains(address, size):
                return allocation
        raise XboxRuntimeError(
            f"address range {_hex32(address)}..{_hex32(address + size)} is not allocated"
        )


class XboxClockShim:
    """Deterministic clock and wait-time model."""

    def __init__(self, trace: ShimTraceLog, config: XboxRuntimeConfig) -> None:
        self._trace = trace
        self._frequency = config.performance_frequency
        self._filetime = config.system_time_filetime
        self._counter = 0

    def query_performance_frequency(self) -> int:
        self._trace.add("threading", "query_performance_frequency", frequency=self._frequency)
        return self._frequency

    def query_performance_counter(self) -> int:
        self._trace.add("threading", "query_performance_counter", counter=self._counter)
        return self._counter

    def query_system_time(self) -> int:
        self._trace.add("threading", "query_system_time", filetime=self._filetime)
        return self._filetime

    def set_system_time(self, filetime: int) -> int:
        previous = self._filetime
        self._filetime = filetime
        self._trace.add(
            "threading", "set_system_time", previous_filetime=previous, filetime=filetime
        )
        return previous

    def query_interrupt_time(self) -> int:
        interrupt_time = self._counter * 10_000_000 // self._frequency
        self._trace.add(
            "threading", "query_interrupt_time", interrupt_time=interrupt_time
        )
        return interrupt_time

    def advance_100ns(self, ticks_100ns: int) -> None:
        if ticks_100ns < 0:
            raise XboxRuntimeError("cannot advance time by a negative interval")
        counter_delta = ticks_100ns * self._frequency // 10_000_000
        self._counter += counter_delta
        self._filetime += ticks_100ns
        self._trace.add(
            "threading",
            "advance_time",
            ticks_100ns=ticks_100ns,
            counter_delta=counter_delta,
        )

    def stall_execution_processor(self, microseconds: int) -> int:
        self.advance_100ns(max(microseconds, 0) * 10)
        self._trace.add("threading", "stall_processor", microseconds=microseconds)
        return XboxStatus.SUCCESS

    def delay_execution_thread(self, interval_100ns: int) -> int:
        self.advance_100ns(abs(interval_100ns))
        self._trace.add(
            "threading", "delay_thread", interval_100ns=interval_100ns
        )
        return XboxStatus.SUCCESS

    def snapshot(self) -> dict[str, Any]:
        return {
            "performance_frequency": self._frequency,
            "performance_counter": self._counter,
            "system_time_filetime": self._filetime,
            "interrupt_time_100ns": self._counter * 10_000_000 // self._frequency,
        }


@dataclass
class EventObject:
    manual_reset: bool
    signaled: bool


@dataclass
class SemaphoreObject:
    count: int
    limit: int


@dataclass
class TimerObject:
    address: int | None = None
    timer_type: int = 0
    due_time_100ns: int | None = None
    period_ms: int = 0
    signaled: bool = False


@dataclass
class ThreadObject:
    start_address: int | None = None
    parameter: int | None = None
    suspended: bool = False
    start_context1: int | None = None
    start_context2: int | None = None
    thread_extra_size: int = 0
    kernel_stack_size: int = 0
    tls_data_size: int = 0
    thread_handle_address: int | None = None
    thread_id_address: int | None = None
    debug_stack: bool = False
    priority: int = 8
    base_priority: int = 8
    disable_boost: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_address": self.start_address,
            "start_address_hex": _hex32(self.start_address)
            if self.start_address is not None
            else None,
            "parameter": self.parameter,
            "parameter_hex": _hex32(self.parameter)
            if self.parameter is not None
            else None,
            "suspended": self.suspended,
            "start_context1": self.start_context1,
            "start_context1_hex": _hex32(self.start_context1)
            if self.start_context1 is not None
            else None,
            "start_context2": self.start_context2,
            "start_context2_hex": _hex32(self.start_context2)
            if self.start_context2 is not None
            else None,
            "thread_extra_size": self.thread_extra_size,
            "kernel_stack_size": self.kernel_stack_size,
            "tls_data_size": self.tls_data_size,
            "thread_handle_address": self.thread_handle_address,
            "thread_handle_address_hex": _hex32(self.thread_handle_address)
            if self.thread_handle_address is not None
            else None,
            "thread_id_address": self.thread_id_address,
            "thread_id_address_hex": _hex32(self.thread_id_address)
            if self.thread_id_address is not None
            else None,
            "debug_stack": self.debug_stack,
            "priority": self.priority,
            "base_priority": self.base_priority,
            "disable_boost": self.disable_boost,
        }


@dataclass
class DpcObject:
    routine: int | None = None
    context: int | None = None
    queued: bool = False


@dataclass
class InterruptObject:
    vector: int
    service_routine: int | None = None
    connected: bool = False


class XboxSynchronizationShim:
    """Kernel event, semaphore, timer, DPC, interrupt, and thread models."""

    def __init__(
        self, trace: ShimTraceLog, handles: GuestHandleTable, clock: XboxClockShim
    ) -> None:
        self._trace = trace
        self._handles = handles
        self._clock = clock
        self._critical_sections: set[int] = set()
        self._current_thread = self._handles.allocate("thread", ThreadObject())
        self._dpcs: dict[int, DpcObject] = {}
        self._kernel_timers: dict[int, TimerObject] = {}
        self._interrupts: dict[int, InterruptObject] = {}
        self._bugcheck: dict[str, Any] | None = None

    @property
    def current_thread(self) -> int:
        return self._current_thread

    def create_event(self, *, manual_reset: bool = True, initial_state: bool = False) -> int:
        handle = self._handles.allocate("event", EventObject(manual_reset, initial_state))
        self._trace.add(
            "threading",
            "create_event",
            handle=handle,
            manual_reset=manual_reset,
            initial_state=initial_state,
        )
        return handle

    def initialize_event(
        self, manual_reset: bool = True, initial_state: bool = False
    ) -> EventObject:
        event = EventObject(manual_reset, initial_state)
        self._trace.add(
            "threading",
            "initialize_event",
            manual_reset=manual_reset,
            initial_state=initial_state,
        )
        return event

    def set_event(self, handle: int) -> int:
        event = self._handles.get(handle, "event")
        previous = event.signaled
        event.signaled = True
        self._trace.add("threading", "set_event", handle=handle, previous=previous)
        return XboxStatus.SUCCESS

    def reset_event(self, handle: int) -> int:
        event = self._handles.get(handle, "event")
        event.signaled = False
        self._trace.add("threading", "reset_event", handle=handle)
        return XboxStatus.SUCCESS

    def create_semaphore(self, initial_count: int, limit: int) -> int:
        if initial_count < 0 or limit <= 0 or initial_count > limit:
            raise XboxRuntimeError("invalid semaphore counts")
        handle = self._handles.allocate("semaphore", SemaphoreObject(initial_count, limit))
        self._trace.add(
            "threading",
            "create_semaphore",
            handle=handle,
            initial_count=initial_count,
            limit=limit,
        )
        return handle

    def initialize_semaphore(self, initial_count: int, limit: int) -> SemaphoreObject:
        if initial_count < 0 or limit <= 0 or initial_count > limit:
            raise XboxRuntimeError("invalid semaphore counts")
        semaphore = SemaphoreObject(initial_count, limit)
        self._trace.add(
            "threading",
            "initialize_semaphore",
            initial_count=initial_count,
            limit=limit,
        )
        return semaphore

    def release_semaphore(self, handle: int, release_count: int = 1) -> dict[str, Any]:
        semaphore = self._handles.get(handle, "semaphore")
        previous = semaphore.count
        semaphore.count = min(semaphore.limit, semaphore.count + release_count)
        self._trace.add(
            "threading",
            "release_semaphore",
            handle=handle,
            previous=previous,
            count=semaphore.count,
        )
        return {"status": XboxStatus.SUCCESS, "previous_count": previous}

    def create_timer(self) -> int:
        handle = self._handles.allocate("timer", TimerObject())
        self._trace.add("threading", "create_timer", handle=handle)
        return handle

    def initialize_timer(
        self, timer_address: int | None = None, timer_type: int = 0
    ) -> TimerObject:
        timer = TimerObject(address=timer_address, timer_type=timer_type)
        if timer_address is not None:
            self._kernel_timers[timer_address] = timer
        self._trace.add(
            "threading",
            "initialize_timer",
            timer_address=timer_address,
            timer_type=timer_type,
        )
        return timer

    def set_timer_handle(
        self, handle: int, due_time_100ns: int, period_ms: int = 0
    ) -> int:
        timer = self._handles.get(handle, "timer")
        timer.due_time_100ns = due_time_100ns
        timer.period_ms = period_ms
        timer.signaled = False
        self._trace.add(
            "threading",
            "set_timer",
            handle=handle,
            due_time_100ns=due_time_100ns,
            period_ms=period_ms,
        )
        return XboxStatus.SUCCESS

    def set_kernel_timer(
        self,
        timer_address: int,
        due_time_low: int,
        due_time_high: int = 0,
        dpc_address: int | None = None,
        *,
        period_ms: int = 0,
    ) -> bool:
        timer = self._kernel_timers.setdefault(
            timer_address, TimerObject(address=timer_address)
        )
        previous = timer.signaled
        due_time_100ns = _i64_from_u32_parts(due_time_low, due_time_high)
        timer.due_time_100ns = due_time_100ns
        timer.period_ms = period_ms
        timer.signaled = False
        self._trace.add(
            "threading",
            "set_kernel_timer",
            timer_address=timer_address,
            due_time_100ns=due_time_100ns,
            period_ms=period_ms,
            dpc_address=dpc_address,
            previous_signaled=previous,
        )
        return previous

    def cancel_timer(self, handle: int) -> int:
        timer = self._handles.get(handle, "timer")
        timer.signaled = False
        self._trace.add("threading", "cancel_timer", handle=handle)
        return XboxStatus.SUCCESS

    def wait_for_single_object(
        self, handle: int, *, timeout_100ns: int | None = None
    ) -> int:
        kind, obj = self._handles._objects.get(handle, (None, None))
        if kind is None:
            return XboxStatus.INVALID_HANDLE
        ready = False
        if kind == "event":
            ready = obj.signaled
            if ready and not obj.manual_reset:
                obj.signaled = False
        elif kind == "semaphore":
            ready = obj.count > 0
            if ready:
                obj.count -= 1
        elif kind == "timer":
            ready = obj.signaled
        elif kind == "thread":
            ready = not obj.suspended
        else:
            ready = True

        if ready:
            status = XboxStatus.WAIT_0
        elif timeout_100ns == 0:
            status = XboxStatus.WAIT_TIMEOUT
        else:
            if timeout_100ns:
                self._clock.advance_100ns(abs(timeout_100ns))
            status = XboxStatus.WAIT_TIMEOUT
        self._trace.add(
            "threading",
            "wait_single",
            handle=handle,
            kind=kind,
            timeout_100ns=timeout_100ns,
            status_hex=_hex32(status),
        )
        return status

    def create_system_thread(
        self,
        *,
        start_address: int | None = None,
        parameter: int | None = None,
        suspended: bool = False,
        start_context1: int | None = None,
        start_context2: int | None = None,
        thread_extra_size: int = 0,
        kernel_stack_size: int = 0,
        tls_data_size: int = 0,
        thread_handle_address: int | None = None,
        thread_id_address: int | None = None,
        debug_stack: bool = False,
    ) -> int:
        handle = self._handles.allocate(
            "thread",
            ThreadObject(
                start_address=start_address,
                parameter=parameter,
                suspended=suspended,
                start_context1=start_context1,
                start_context2=start_context2,
                thread_extra_size=thread_extra_size,
                kernel_stack_size=kernel_stack_size,
                tls_data_size=tls_data_size,
                thread_handle_address=thread_handle_address,
                thread_id_address=thread_id_address,
                debug_stack=debug_stack,
            ),
        )
        self._trace.add(
            "threading",
            "create_system_thread",
            handle=handle,
            start_address=start_address,
            parameter=parameter,
            suspended=suspended,
            start_context1=start_context1,
            start_context2=start_context2,
            thread_extra_size=thread_extra_size,
            kernel_stack_size=kernel_stack_size,
            tls_data_size=tls_data_size,
            thread_handle_address=thread_handle_address,
            thread_id_address=thread_id_address,
            debug_stack=debug_stack,
        )
        return handle

    def thread_snapshot(self) -> list[dict[str, Any]]:
        threads: list[dict[str, Any]] = []
        for handle, (kind, thread) in sorted(self._handles._objects.items()):
            if kind != "thread":
                continue
            data = thread.to_dict()
            data["handle"] = handle
            data["handle_hex"] = _hex32(handle)
            data["current"] = handle == self._current_thread
            threads.append(data)
        return threads

    def terminate_thread(self, handle: int | None = None, status: int = XboxStatus.SUCCESS) -> int:
        target = handle or self._current_thread
        try:
            thread = self._handles.get(target, "thread")
            thread.suspended = True
        except XboxRuntimeError:
            return XboxStatus.INVALID_HANDLE
        self._trace.add("threading", "terminate_thread", handle=target, status_hex=_hex32(status))
        return status

    def suspend_thread(self, handle: int) -> int:
        thread = self._handles.get(handle, "thread")
        thread.suspended = True
        self._trace.add("threading", "suspend_thread", handle=handle)
        return XboxStatus.SUCCESS

    def resume_thread(self, handle: int) -> int:
        thread = self._handles.get(handle, "thread")
        thread.suspended = False
        self._trace.add("threading", "resume_thread", handle=handle)
        return XboxStatus.SUCCESS

    def set_thread_priority(self, handle: int, priority: int, *, base: bool = False) -> int:
        thread = self._handles.get(handle, "thread")
        previous = thread.base_priority if base else thread.priority
        if base:
            thread.base_priority = priority
        else:
            thread.priority = priority
        self._trace.add(
            "threading",
            "set_thread_priority",
            handle=handle,
            previous=previous,
            priority=priority,
            base=base,
        )
        return previous

    def set_thread_disable_boost(self, handle: int, disable: bool) -> int:
        thread = self._handles.get(handle, "thread")
        previous = thread.disable_boost
        thread.disable_boost = disable
        self._trace.add(
            "threading",
            "set_thread_disable_boost",
            handle=handle,
            previous=previous,
            disable=disable,
        )
        return int(previous)

    def initialize_dpc(self, dpc_id: int, routine: int | None = None, context: int | None = None) -> DpcObject:
        dpc = DpcObject(routine, context, False)
        self._dpcs[dpc_id] = dpc
        self._trace.add("threading", "initialize_dpc", dpc_id=dpc_id, routine=routine)
        return dpc

    def insert_queue_dpc(self, dpc_id: int) -> bool:
        dpc = self._dpcs.setdefault(dpc_id, DpcObject())
        previous = dpc.queued
        dpc.queued = True
        self._trace.add("threading", "insert_queue_dpc", dpc_id=dpc_id, previous=previous)
        return not previous

    def remove_queue_dpc(self, dpc_id: int) -> bool:
        dpc = self._dpcs.setdefault(dpc_id, DpcObject())
        previous = dpc.queued
        dpc.queued = False
        self._trace.add("threading", "remove_queue_dpc", dpc_id=dpc_id, previous=previous)
        return previous

    def initialize_interrupt(
        self, interrupt_id: int, vector: int, service_routine: int | None = None
    ) -> InterruptObject:
        interrupt = InterruptObject(vector, service_routine, False)
        self._interrupts[interrupt_id] = interrupt
        self._trace.add(
            "threading",
            "initialize_interrupt",
            interrupt_id=interrupt_id,
            vector=vector,
            service_routine=service_routine,
        )
        return interrupt

    def connect_interrupt(self, interrupt_id: int) -> bool:
        interrupt = self._interrupts.setdefault(interrupt_id, InterruptObject(interrupt_id))
        previous = interrupt.connected
        interrupt.connected = True
        self._trace.add("threading", "connect_interrupt", interrupt_id=interrupt_id)
        return not previous

    def disconnect_interrupt(self, interrupt_id: int) -> bool:
        interrupt = self._interrupts.setdefault(interrupt_id, InterruptObject(interrupt_id))
        previous = interrupt.connected
        interrupt.connected = False
        self._trace.add("threading", "disconnect_interrupt", interrupt_id=interrupt_id)
        return previous

    def synchronize_execution(self, interrupt_id: int, routine: Callable[..., Any] | None = None) -> Any:
        self._trace.add("threading", "synchronize_execution", interrupt_id=interrupt_id)
        if routine is None:
            return True
        return routine()

    def bugcheck(self, code: int, *parameters: int) -> int:
        self._bugcheck = {"code": code, "parameters": list(parameters)}
        self._trace.add("threading", "bugcheck", "fatal", code=code, parameters=list(parameters))
        return code

    def enter_critical_section(self, address: int) -> int:
        self._critical_sections.add(address)
        self._trace.add("runtime", "enter_critical_section", address=address)
        return XboxStatus.SUCCESS

    def leave_critical_section(self, address: int) -> int:
        self._critical_sections.discard(address)
        self._trace.add("runtime", "leave_critical_section", address=address)
        return XboxStatus.SUCCESS


@dataclass(frozen=True)
class ControllerState:
    connected: bool = False
    buttons: int = 0
    left_trigger: int = 0
    right_trigger: int = 0
    thumb_lx: int = 0
    thumb_ly: int = 0
    thumb_rx: int = 0
    thumb_ry: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "buttons": self.buttons,
            "left_trigger": self.left_trigger,
            "right_trigger": self.right_trigger,
            "thumb_lx": self.thumb_lx,
            "thumb_ly": self.thumb_ly,
            "thumb_rx": self.thumb_rx,
            "thumb_ry": self.thumb_ry,
        }


@dataclass
class ControllerSlot:
    state: ControllerState = field(default_factory=ControllerState)
    update_sequence: int = 0
    poll_count: int = 0
    last_latency_samples: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.to_dict(),
            "update_sequence": self.update_sequence,
            "poll_count": self.poll_count,
            "last_latency_samples": self.last_latency_samples,
        }


class XboxInputShim:
    def __init__(self, trace: ShimTraceLog) -> None:
        self._trace = trace
        self._controllers = {port: ControllerSlot() for port in range(4)}
        self._sequence = 0

    def set_controller_state(self, port: int, state: ControllerState) -> None:
        self._validate_port(port)
        self._sequence += 1
        slot = self._controllers[port]
        slot.state = state
        slot.update_sequence = self._sequence
        slot.last_latency_samples = 0
        self._trace.add(
            "input",
            "set_controller_state",
            port=port,
            state=state,
            update_sequence=slot.update_sequence,
        )

    def poll_controller(self, port: int) -> ControllerState:
        self._validate_port(port)
        self._sequence += 1
        slot = self._controllers[port]
        slot.poll_count += 1
        latency_samples = max(0, self._sequence - slot.update_sequence)
        slot.last_latency_samples = latency_samples
        self._trace.add(
            "input",
            "poll_controller",
            port=port,
            state=slot.state,
            poll_sequence=self._sequence,
            update_sequence=slot.update_sequence,
            latency_samples=latency_samples,
        )
        return slot.state

    def poll_all(self) -> dict[int, ControllerState]:
        self._sequence += 1
        for slot in self._controllers.values():
            slot.poll_count += 1
            slot.last_latency_samples = max(0, self._sequence - slot.update_sequence)
        self._trace.add("input", "poll_all", poll_sequence=self._sequence)
        return {port: slot.state for port, slot in self._controllers.items()}

    def snapshot(self) -> dict[str, Any]:
        return {
            "sequence": self._sequence,
            "ports": {
                str(port): slot.to_dict()
                for port, slot in sorted(self._controllers.items())
            },
        }

    @staticmethod
    def _validate_port(port: int) -> None:
        if port not in range(4):
            raise XboxRuntimeError(f"invalid controller port: {port}")


@dataclass
class DisplayMode:
    width: int = 640
    height: int = 480
    bpp: int = 32
    refresh_rate: int = 60
    flags: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "bpp": self.bpp,
            "refresh_rate": self.refresh_rate,
            "flags": self.flags,
        }


class XboxGraphicsShim:
    def __init__(self, trace: ShimTraceLog, memory: XboxMemoryShim) -> None:
        self._trace = trace
        self._memory = memory
        self.display_mode = DisplayMode()
        self.saved_data_address = 0
        self.encoder_options: list[dict[str, Any]] = []

    def set_display_mode(
        self,
        width: int = 640,
        height: int = 480,
        bpp: int = 32,
        refresh_rate: int = 60,
        flags: int = 0,
    ) -> int:
        self.display_mode = DisplayMode(width, height, bpp, refresh_rate, flags)
        self._trace.add("rendering", "set_display_mode", mode=self.display_mode)
        return XboxStatus.SUCCESS

    def send_tv_encoder_option(self, option: int, value: int = 0) -> int:
        self.encoder_options.append({"option": option, "value": value})
        self._trace.add("rendering", "send_tv_encoder_option", option=option, value=value)
        return XboxStatus.SUCCESS

    def set_saved_data_address(self, address: int) -> int:
        self.saved_data_address = address
        self._trace.add("rendering", "set_saved_data_address", address=address)
        return XboxStatus.SUCCESS

    def get_saved_data_address(self) -> int:
        self._trace.add(
            "rendering", "get_saved_data_address", address=self.saved_data_address
        )
        return self.saved_data_address

    def claim_gpu_instance_memory(self, size: int = 0x100000) -> int:
        address = self._memory.allocate_contiguous_memory(size, kind="gpu")
        self._trace.add("rendering", "claim_gpu_memory", address=address, size=size)
        return address


@dataclass
class AudioStream:
    format_tag: str
    queued_bytes: int = 0
    submitted_buffer_count: int = 0
    played_bytes: int = 0
    sample_rate: int = 48000
    channels: int = 2
    bits_per_sample: int = 16

    @property
    def bytes_per_second(self) -> int:
        return self.sample_rate * self.channels * max(1, self.bits_per_sample // 8)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_tag": self.format_tag,
            "queued_bytes": self.queued_bytes,
            "submitted_buffer_count": self.submitted_buffer_count,
            "played_bytes": self.played_bytes,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "bits_per_sample": self.bits_per_sample,
        }


class XboxAudioShim:
    def __init__(
        self,
        trace: ShimTraceLog,
        handles: GuestHandleTable,
        clock: XboxClockShim,
    ) -> None:
        self._trace = trace
        self._handles = handles
        self._clock = clock
        self.initialized = False

    def initialize(self) -> int:
        self.initialized = True
        self._trace.add("audio", "initialize")
        return XboxStatus.SUCCESS

    def create_stream(
        self,
        format_tag: str = "pcm",
        *,
        sample_rate: int = 48000,
        channels: int = 2,
        bits_per_sample: int = 16,
    ) -> int:
        self.initialized = True
        stream = AudioStream(
            format_tag,
            sample_rate=sample_rate,
            channels=channels,
            bits_per_sample=bits_per_sample,
        )
        handle = self._handles.allocate("audio_stream", stream)
        self._trace.add(
            "audio",
            "create_stream",
            handle=handle,
            format_tag=format_tag,
            sample_rate=sample_rate,
            channels=channels,
            bits_per_sample=bits_per_sample,
        )
        return handle

    def submit_buffer(self, handle: int, payload: bytes) -> int:
        stream = self._handles.get(handle, "audio_stream")
        stream.queued_bytes += len(payload)
        stream.submitted_buffer_count += 1
        self._trace.add(
            "audio",
            "submit_buffer",
            handle=handle,
            bytes=len(payload),
            queued_bytes=stream.queued_bytes,
            submitted_buffer_count=stream.submitted_buffer_count,
        )
        return XboxStatus.SUCCESS

    def advance_playback(self, handle: int, milliseconds: int) -> dict[str, Any]:
        stream = self._handles.get(handle, "audio_stream")
        duration_ms = max(milliseconds, 0)
        playable_bytes = stream.bytes_per_second * duration_ms // 1000
        consumed = min(stream.queued_bytes, playable_bytes)
        stream.queued_bytes -= consumed
        stream.played_bytes += consumed
        self._clock.advance_100ns(duration_ms * 10_000)
        self._trace.add(
            "audio",
            "advance_playback",
            handle=handle,
            milliseconds=duration_ms,
            consumed_bytes=consumed,
            queued_bytes=stream.queued_bytes,
            played_bytes=stream.played_bytes,
        )
        return {
            "status": XboxStatus.SUCCESS,
            "consumed_bytes": consumed,
            "queued_bytes": stream.queued_bytes,
            "played_bytes": stream.played_bytes,
        }

    def stream_snapshot(self) -> list[dict[str, Any]]:
        streams: list[dict[str, Any]] = []
        for handle, (kind, stream) in sorted(self._handles._objects.items()):
            if kind != "audio_stream":
                continue
            data = stream.to_dict()
            data["handle"] = handle
            data["handle_hex"] = _hex32(handle)
            streams.append(data)
        return streams


@dataclass
class ShaContext:
    hasher: Any = field(default_factory=lambda: hashlib.sha1(usedforsecurity=False))


@dataclass
class Rc4Key:
    state: list[int]
    i: int = 0
    j: int = 0

    @classmethod
    def from_key(cls, key: bytes) -> "Rc4Key":
        state = list(range(256))
        j = 0
        key_bytes = key or b"\x00"
        for i in range(256):
            j = (j + state[i] + key_bytes[i % len(key_bytes)]) & 0xFF
            state[i], state[j] = state[j], state[i]
        return cls(state)


_DES_IP = (
    58, 50, 42, 34, 26, 18, 10, 2,
    60, 52, 44, 36, 28, 20, 12, 4,
    62, 54, 46, 38, 30, 22, 14, 6,
    64, 56, 48, 40, 32, 24, 16, 8,
    57, 49, 41, 33, 25, 17, 9, 1,
    59, 51, 43, 35, 27, 19, 11, 3,
    61, 53, 45, 37, 29, 21, 13, 5,
    63, 55, 47, 39, 31, 23, 15, 7,
)

_DES_FP = (
    40, 8, 48, 16, 56, 24, 64, 32,
    39, 7, 47, 15, 55, 23, 63, 31,
    38, 6, 46, 14, 54, 22, 62, 30,
    37, 5, 45, 13, 53, 21, 61, 29,
    36, 4, 44, 12, 52, 20, 60, 28,
    35, 3, 43, 11, 51, 19, 59, 27,
    34, 2, 42, 10, 50, 18, 58, 26,
    33, 1, 41, 9, 49, 17, 57, 25,
)

_DES_E = (
    32, 1, 2, 3, 4, 5,
    4, 5, 6, 7, 8, 9,
    8, 9, 10, 11, 12, 13,
    12, 13, 14, 15, 16, 17,
    16, 17, 18, 19, 20, 21,
    20, 21, 22, 23, 24, 25,
    24, 25, 26, 27, 28, 29,
    28, 29, 30, 31, 32, 1,
)

_DES_P = (
    16, 7, 20, 21,
    29, 12, 28, 17,
    1, 15, 23, 26,
    5, 18, 31, 10,
    2, 8, 24, 14,
    32, 27, 3, 9,
    19, 13, 30, 6,
    22, 11, 4, 25,
)

_DES_PC1 = (
    57, 49, 41, 33, 25, 17, 9,
    1, 58, 50, 42, 34, 26, 18,
    10, 2, 59, 51, 43, 35, 27,
    19, 11, 3, 60, 52, 44, 36,
    63, 55, 47, 39, 31, 23, 15,
    7, 62, 54, 46, 38, 30, 22,
    14, 6, 61, 53, 45, 37, 29,
    21, 13, 5, 28, 20, 12, 4,
)

_DES_PC2 = (
    14, 17, 11, 24, 1, 5,
    3, 28, 15, 6, 21, 10,
    23, 19, 12, 4, 26, 8,
    16, 7, 27, 20, 13, 2,
    41, 52, 31, 37, 47, 55,
    30, 40, 51, 45, 33, 48,
    44, 49, 39, 56, 34, 53,
    46, 42, 50, 36, 29, 32,
)

_DES_SHIFTS = (1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1)

_DES_SBOXES = (
    (
        (14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7),
        (0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8),
        (4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0),
        (15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13),
    ),
    (
        (15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10),
        (3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5),
        (0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15),
        (13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9),
    ),
    (
        (10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8),
        (13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1),
        (13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7),
        (1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12),
    ),
    (
        (7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15),
        (13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9),
        (10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4),
        (3, 15, 0, 6, 10, 1, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14),
    ),
    (
        (2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9),
        (14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6),
        (4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14),
        (11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3),
    ),
    (
        (12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11),
        (10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8),
        (9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6),
        (4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13),
    ),
    (
        (4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1),
        (13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6),
        (1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2),
        (6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12),
    ),
    (
        (13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7),
        (1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2),
        (7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8),
        (2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11),
    ),
)

_SHA1_DIGEST_INFO_PREFIX = bytes.fromhex("3021300906052B0E03021A05000414")
_XBOX_SHA1_DIGEST_INFO_PREFIXES = (
    bytes.fromhex("140400051A02030E2B050609302130"),
    bytes.fromhex("14041A02030E2B050607301F30"),
)


def _des_permute(value: int, table: tuple[int, ...], input_bits: int) -> int:
    result = 0
    for position in table:
        result = (result << 1) | ((value >> (input_bits - position)) & 1)
    return result


def _des_rotate_left28(value: int, count: int) -> int:
    return ((value << count) | (value >> (28 - count))) & 0x0FFFFFFF


def _des_subkeys(key: bytes) -> tuple[int, ...]:
    if len(key) < 8:
        raise XboxRuntimeError("DES key must be at least 8 bytes")
    key_bits = int.from_bytes(key[:8], "big")
    permuted = _des_permute(key_bits, _DES_PC1, 64)
    left = (permuted >> 28) & 0x0FFFFFFF
    right = permuted & 0x0FFFFFFF
    subkeys = []
    for shift in _DES_SHIFTS:
        left = _des_rotate_left28(left, shift)
        right = _des_rotate_left28(right, shift)
        subkeys.append(_des_permute((left << 28) | right, _DES_PC2, 56))
    return tuple(subkeys)


def _des_round_function(right: int, subkey: int) -> int:
    expanded = _des_permute(right, _DES_E, 32) ^ subkey
    substituted = 0
    for index, sbox in enumerate(_DES_SBOXES):
        chunk = (expanded >> (42 - (index * 6))) & 0x3F
        row = ((chunk & 0x20) >> 4) | (chunk & 0x01)
        column = (chunk >> 1) & 0x0F
        substituted = (substituted << 4) | sbox[row][column]
    return _des_permute(substituted, _DES_P, 32)


def _des_crypt_block(key: bytes, block: bytes, *, decrypt: bool = False) -> bytes:
    if len(block) != 8:
        raise XboxRuntimeError("DES block must be exactly 8 bytes")
    value = int.from_bytes(block, "big")
    permuted = _des_permute(value, _DES_IP, 64)
    left = (permuted >> 32) & 0xFFFFFFFF
    right = permuted & 0xFFFFFFFF
    subkeys = _des_subkeys(key)
    if decrypt:
        subkeys = tuple(reversed(subkeys))
    for subkey in subkeys:
        left, right = right, left ^ _des_round_function(right, subkey)
    final = _des_permute((right << 32) | left, _DES_FP, 64)
    return final.to_bytes(8, "big")


def _triple_des_crypt_block(keys: tuple[bytes, bytes, bytes], block: bytes, *, decrypt: bool = False) -> bytes:
    first, second, third = keys
    if decrypt:
        step1 = _des_crypt_block(third, block, decrypt=True)
        step2 = _des_crypt_block(second, step1, decrypt=False)
        return _des_crypt_block(first, step2, decrypt=True)
    return _des_crypt_block(
        third,
        _des_crypt_block(
            second,
            _des_crypt_block(first, block, decrypt=False),
            decrypt=True,
        ),
        decrypt=False,
    )


def _des_cbc_crypt(
    payload: bytes,
    keys: tuple[bytes, ...],
    iv: bytes,
    *,
    decrypt: bool = False,
) -> tuple[bytes, bytes, bool]:
    feedback = (iv or b"\x00" * 8)[:8].ljust(8, b"\x00")
    invalid_length = len(payload) % 8 != 0
    output = bytearray()
    block_count = (len(payload) + 7) // 8
    for index in range(block_count):
        source = payload[index * 8 : index * 8 + 8]
        original_size = len(source)
        block = source.ljust(8, b"\x00")
        if decrypt:
            crypted = _des_crypt_block(keys[0], block, decrypt=True) if len(keys) == 1 else _triple_des_crypt_block(keys, block, decrypt=True)
            plain = bytes(left ^ right for left, right in zip(crypted, feedback))
            feedback = block
            output.extend(plain[:original_size])
        else:
            mixed = bytes(left ^ right for left, right in zip(block, feedback))
            crypted = _des_crypt_block(keys[0], mixed, decrypt=False) if len(keys) == 1 else _triple_des_crypt_block(keys, mixed, decrypt=False)
            feedback = crypted
            output.extend(crypted[:original_size])
    return bytes(output), feedback, invalid_length


def _parse_xbox_rsa_public_key(public_key: bytes) -> dict[str, Any]:
    if len(public_key) < 284 or public_key[:4] != b"RSA1":
        raise XboxRuntimeError("not an Xbox RSA1 public key blob")
    blob_length = int.from_bytes(public_key[4:8], "little")
    bit_length = int.from_bytes(public_key[8:12], "little")
    modulus_size = int.from_bytes(public_key[12:16], "little")
    exponent = int.from_bytes(public_key[16:20], "little")
    modulus = int.from_bytes(public_key[20:276], "little")
    if exponent <= 0 or modulus <= 0:
        raise XboxRuntimeError("invalid Xbox RSA public key")
    return {
        "format": "xbox-rsa1",
        "blob_length": blob_length,
        "bit_length": bit_length,
        "modulus_size": modulus_size,
        "exponent": exponent,
        "modulus": modulus,
        "size": 256,
    }


def _parse_rsa_public_key(public_key: Any, signature_size: int) -> dict[str, Any]:
    if isinstance(public_key, dict):
        modulus = int(public_key["modulus"])
        exponent = int(public_key.get("exponent", 65537))
        size = int(public_key.get("size", max(signature_size, (modulus.bit_length() + 7) // 8)))
        return {
            "format": "generic",
            "modulus": modulus,
            "exponent": exponent,
            "size": size,
            "modulus_size": size - 1,
        }
    if isinstance(public_key, tuple) and len(public_key) >= 2:
        modulus, exponent = int(public_key[0]), int(public_key[1])
        size = max(signature_size, (modulus.bit_length() + 7) // 8)
        return {
            "format": "generic",
            "modulus": modulus,
            "exponent": exponent,
            "size": size,
            "modulus_size": size - 1,
        }
    if isinstance(public_key, bytes):
        if public_key.startswith(b"RSA1"):
            return _parse_xbox_rsa_public_key(public_key)
        if len(public_key) >= 8:
            exponent = int.from_bytes(public_key[:4], "big")
            modulus = int.from_bytes(public_key[4:], "big")
            size = max(signature_size, len(public_key) - 4)
            return {
                "format": "raw-exponent-modulus",
                "modulus": modulus,
                "exponent": exponent,
                "size": size,
                "modulus_size": size - 1,
            }
    raise XboxRuntimeError("unsupported RSA public key format")


def _verify_xbox_pkcs1_sha1(decrypted_little: bytes, digest: bytes, modulus_size: int) -> bool:
    if len(digest) != 20 or len(decrypted_little) <= modulus_size:
        return False
    if decrypted_little[:20] != digest[::-1]:
        return False
    zero_position = 20
    for prefix in _XBOX_SHA1_DIGEST_INFO_PREFIXES:
        if decrypted_little[20 : 20 + len(prefix)] == prefix:
            zero_position = 20 + len(prefix)
            break
    else:
        return False
    if decrypted_little[zero_position] != 0:
        return False
    if decrypted_little[modulus_size] != 0:
        return False
    if decrypted_little[modulus_size - 1] != 1:
        return False
    return all(
        value == 0xFF
        for value in decrypted_little[zero_position + 1 : modulus_size - 1]
    )


def _verify_pkcs1_v15_sha1(decrypted_big: bytes, digest: bytes) -> bool:
    expected_tail = _SHA1_DIGEST_INFO_PREFIX + digest
    if len(digest) != 20 or len(decrypted_big) < len(expected_tail) + 11:
        return False
    if not decrypted_big.startswith(b"\x00\x01"):
        return False
    separator = decrypted_big.find(b"\x00", 2)
    if separator < 10:
        return False
    if any(value != 0xFF for value in decrypted_big[2:separator]):
        return False
    return decrypted_big[separator + 1 :] == expected_tail


class XboxCryptoShim:
    def __init__(self, trace: ShimTraceLog) -> None:
        self._trace = trace

    def sha_init(self) -> ShaContext:
        self._trace.add("crypto", "sha_init")
        return ShaContext()

    def sha_update(self, context: ShaContext, payload: bytes) -> int:
        context.hasher.update(payload)
        self._trace.add("crypto", "sha_update", bytes=len(payload))
        return XboxStatus.SUCCESS

    def sha_final(self, context: ShaContext) -> bytes:
        digest = context.hasher.digest()
        self._trace.add("crypto", "sha_final")
        return digest

    def sha1(self, payload: bytes) -> bytes:
        digest = hashlib.sha1(payload, usedforsecurity=False).digest()
        self._trace.add("crypto", "sha1", bytes=len(payload))
        return digest

    def hmac_sha1(self, key: bytes, payload: bytes, payload2: bytes = b"") -> bytes:
        key_material = key[:64]
        pad1 = bytearray(64)
        pad2 = bytearray(64)
        pad1[: len(key_material)] = key_material
        pad2[: len(key_material)] = key_material
        for index in range(64):
            pad1[index] ^= 0x36
            pad2[index] ^= 0x5C

        inner = hashlib.sha1(usedforsecurity=False)
        inner.update(pad1)
        inner.update(payload)
        inner.update(payload2)
        inner_digest = inner.digest()

        outer = hashlib.sha1(usedforsecurity=False)
        outer.update(pad2)
        outer.update(inner_digest)
        digest = outer.digest()
        self._trace.add(
            "crypto",
            "hmac_sha1",
            key_bytes=len(key),
            bytes=len(payload),
            bytes2=len(payload2),
        )
        return digest

    def rc4_key(self, key: bytes) -> Rc4Key:
        self._trace.add("crypto", "rc4_key", key_bytes=len(key))
        return Rc4Key.from_key(key)

    def rc4_crypt(self, key: Rc4Key | bytes, payload: bytes) -> bytes:
        if isinstance(key, Rc4Key):
            context = key
        else:
            context = Rc4Key.from_key(key)
        output = bytearray()
        for byte in payload:
            context.i = (context.i + 1) & 0xFF
            context.j = (context.j + context.state[context.i]) & 0xFF
            context.state[context.i], context.state[context.j] = (
                context.state[context.j],
                context.state[context.i],
            )
            output.append(
                byte ^ context.state[
                    (context.state[context.i] + context.state[context.j]) & 0xFF
                ]
            )
        self._trace.add("crypto", "rc4_crypt", bytes=len(payload))
        return bytes(output)

    def des_key_parity(self, key: bytes) -> bytes:
        adjusted = bytearray()
        for byte in key:
            upper = byte & 0xFE
            parity = 1 if bin(upper).count("1") % 2 == 0 else 0
            adjusted.append(upper | parity)
        self._trace.add("crypto", "des_key_parity", key_bytes=len(key))
        return bytes(adjusted)

    def mod_exp(
        self,
        base: int | bytes,
        exponent: int | bytes,
        modulus: int | bytes,
        size: int | None = None,
    ) -> int | bytes:
        if isinstance(base, bytes) or isinstance(exponent, bytes) or isinstance(modulus, bytes):
            base_bytes = base if isinstance(base, bytes) else int(base).to_bytes(size or 4, "little")
            exponent_bytes = exponent if isinstance(exponent, bytes) else int(exponent).to_bytes(size or 4, "little")
            modulus_bytes = modulus if isinstance(modulus, bytes) else int(modulus).to_bytes(size or 4, "little")
            output_size = size or max(len(base_bytes), len(modulus_bytes))
            result = pow(
                int.from_bytes(base_bytes, "little"),
                int.from_bytes(exponent_bytes, "little"),
                int.from_bytes(modulus_bytes, "little"),
            )
            self._trace.add("crypto", "mod_exp", size=output_size, representation="little-endian")
            return result.to_bytes(output_size, "little")
        result = pow(base, exponent, modulus)
        self._trace.add("crypto", "mod_exp", representation="integer")
        return result

    def verify_pkcs1_signature(
        self, signature: bytes, public_key: Any, digest: bytes
    ) -> bool:
        key = _parse_rsa_public_key(public_key, len(signature))
        if key["format"] == "xbox-rsa1":
            signature_int = int.from_bytes(signature[: key["size"]], "little")
            decrypted_int = pow(signature_int, key["exponent"], key["modulus"])
            decrypted = decrypted_int.to_bytes(key["size"], "little")
            result = _verify_xbox_pkcs1_sha1(decrypted, digest, key["modulus_size"])
        else:
            signature_int = int.from_bytes(signature, "big")
            decrypted_int = pow(signature_int, key["exponent"], key["modulus"])
            decrypted = decrypted_int.to_bytes(key["size"], "big")
            result = _verify_pkcs1_v15_sha1(decrypted, digest)
        self._trace.add(
            "crypto",
            "verify_pkcs1_signature",
            digest_bytes=len(digest),
            signature_bytes=len(signature),
            public_key_format=key["format"],
            result=result,
        )
        return result

    def key_table(self, cipher: int, key: bytes) -> dict[str, Any]:
        if cipher:
            if len(key) < 16:
                raise XboxRuntimeError("3DES key table needs at least 16 key bytes")
            keys = (key[:8], key[8:16], key[:8])
            algorithm = "3des-ede2"
        else:
            if len(key) < 8:
                raise XboxRuntimeError("DES key table needs at least 8 key bytes")
            keys = (key[:8],)
            algorithm = "des"
        table = {
            "cipher": cipher,
            "algorithm": algorithm,
            "keys": keys,
            "key_bytes": len(key),
        }
        self._trace.add(
            "crypto", "key_table", cipher=cipher, algorithm=algorithm, key_bytes=len(key)
        )
        return table

    def block_crypt_cbc(
        self,
        key_table: dict[str, Any],
        iv: bytes,
        payload: bytes,
        operation: int = 1,
    ) -> bytes:
        keys = key_table["keys"]
        output, feedback, invalid_length = _des_cbc_crypt(
            payload, keys, iv, decrypt=operation == 0
        )
        key_table["feedback"] = feedback
        self._trace.add(
            "crypto",
            "block_crypt_cbc",
            bytes=len(payload),
            algorithm=key_table.get("algorithm"),
            crypto_operation=operation,
            invalid_length=invalid_length,
        )
        return output


class XboxRuntimeShims:
    """Aggregate runtime shim state and kernel import bindings."""

    def __init__(self, config: XboxRuntimeConfig | None = None) -> None:
        self.config = config or XboxRuntimeConfig()
        self.trace = ShimTraceLog()
        self.handles = GuestHandleTable(self.trace)
        self.memory = XboxMemoryShim(self.trace, self.config)
        self.clock = XboxClockShim(self.trace, self.config)
        self.sync = XboxSynchronizationShim(self.trace, self.handles, self.clock)
        self.filesystem = XboxFileSystemShim(
            self.trace,
            self.handles,
            self.config.extracted_disc_root,
            self.config.save_data_root,
            self.config.dashboard_data_root,
            self.config.cache_data_root,
            self.clock,
        )
        self.input = XboxInputShim(self.trace)
        self.graphics = XboxGraphicsShim(self.trace, self.memory)
        self.audio = XboxAudioShim(self.trace, self.handles, self.clock)
        self.crypto = XboxCryptoShim(self.trace)
        self._next_target = self.config.host_target_base
        self._registered: dict[int, RuntimeShim] = {}
        self._irql = 0
        self._devices: dict[str, DeviceObject] = {}
        self._symbolic_links: dict[str, str] = {
            "\\??\\D:": "\\Device\\Cdrom0",
            "\\Device\\Cdrom0": str(self.config.extracted_disc_root or "D:\\"),
        }
        self._nonvolatile_settings: dict[int, Any] = {
            0x00000001: 0,
            0x00000002: 0,
        }
        self._kernel_variables = self._build_kernel_variables()

    @property
    def registered_shims(self) -> tuple[RuntimeShim, ...]:
        return tuple(sorted(self._registered.values(), key=lambda item: item.ordinal))

    def register_kernel_imports(
        self,
        resolver: ImportResolver,
        imported_ordinals: Iterable[int] | None = None,
    ) -> tuple[RuntimeShim, ...]:
        ordinals = (
            sorted(set(imported_ordinals))
            if imported_ordinals is not None
            else sorted(_IMPLEMENTED_KERNEL_HANDLERS)
        )
        registered = []
        for ordinal in ordinals:
            name = KERNEL_EXPORT_NAMES.get(ordinal, f"ordinal_{ordinal:04d}")
            handler_info = _IMPLEMENTED_KERNEL_HANDLERS.get(name)
            if handler_info is None:
                raise XboxRuntimeError(
                    f"no behavior model registered for kernel import {ordinal} ({name})"
                )
            handler_name, behavior = handler_info
            subsystem = _kernel_import_subsystem(name)
            handler = self._handler_for_registered_shim(name, handler_name, subsystem)
            target = self._allocate_host_target()
            resolver.register_kernel(
                ordinal=ordinal,
                name=name,
                target_address=target,
                handler=handler,
            )
            shim = RuntimeShim(
                ordinal,
                name,
                subsystem,
                target,
                behavior,
                handler,
            )
            self._registered[ordinal] = shim
            registered.append(shim)
            self.trace.add(
                "loader",
                "register_kernel_shim",
                ordinal=ordinal,
                name=name,
                target_address=target,
                behavior=behavior,
            )
        return tuple(registered)

    def imported_ordinals_from_xbe_info(self, info: dict[str, Any]) -> tuple[int, ...]:
        return tuple(
            import_info["ordinal"] for import_info in info["kernel_imports"]["imports"]
        )

    def _build_kernel_variables(self) -> dict[str, KernelVariable]:
        return {
            "ExEventObjectType": KernelVariable("ExEventObjectType", "Event", "OBJECT_TYPE"),
            "IoFileObjectType": KernelVariable("IoFileObjectType", "File", "OBJECT_TYPE"),
            "PsThreadObjectType": KernelVariable("PsThreadObjectType", "Thread", "OBJECT_TYPE"),
            "HalDiskCachePartitionCount": KernelVariable("HalDiskCachePartitionCount", 0, "ULONG"),
            "HalDiskModelNumber": KernelVariable("HalDiskModelNumber", "B2_RECOMP_DISC", "STRING"),
            "HalDiskSerialNumber": KernelVariable("HalDiskSerialNumber", "B2RECOMP0001", "STRING"),
            "HalBootSMCVideoMode": KernelVariable("HalBootSMCVideoMode", 0, "ULONG"),
            "LaunchDataPage": KernelVariable("LaunchDataPage", {"launch_type": "cold"}, "PLAUNCH_DATA_PAGE"),
            "IdexChannelObject": KernelVariable("IdexChannelObject", {"channel": "dvd"}, "IDE_CHANNEL_OBJECT"),
            "XePublicKeyData": KernelVariable("XePublicKeyData", b"\x00" * 284, "UCHAR[]"),
            "XboxHDKey": KernelVariable("XboxHDKey", b"\x00" * 16, "XBOX_KEY_DATA"),
            "XboxLANKey": KernelVariable("XboxLANKey", b"\x00" * 16, "XBOX_KEY_DATA"),
            "XboxSignatureKey": KernelVariable("XboxSignatureKey", b"\x00" * 16, "XBOX_KEY_DATA"),
            "XboxAlternateSignatureKeys": KernelVariable(
                "XboxAlternateSignatureKeys", [b"\x00" * 16 for _ in range(16)], "XBOX_KEY_DATA[]"
            ),
            "XboxHardwareInfo": KernelVariable(
                "XboxHardwareInfo",
                {"flags": 0, "gpu_revision": 0, "memory_megabytes": 64, "emulated": True},
                "XBOX_HARDWARE_INFO",
            ),
            "XboxKrnlVersion": KernelVariable(
                "XboxKrnlVersion", {"major": 1, "minor": 0, "build": 5838, "qfe": 0}, "XBOX_KRNL_VERSION"
            ),
        }

    def _handler_for_registered_shim(
        self, name: str, handler_name: str, subsystem: str
    ) -> Callable[..., Any]:
        if handler_name == "kernel_variable":
            return lambda *args, **kwargs: self.kernel_variable(name)
        return getattr(self, handler_name)

    def kernel_variable(self, name: str) -> Any:
        variable = self._kernel_variables.get(name)
        if variable is None:
            raise XboxRuntimeError(f"unknown kernel variable export: {name}")
        self.trace.add("loader", "kernel_variable", name=name, variable=variable)
        return variable.value

    def dbg_print(self, message: str = "", *args: Any) -> int:
        formatted = message % args if args else message
        self.trace.add("diagnostics", "dbg_print", message=formatted)
        return XboxStatus.SUCCESS

    def ex_allocate_pool(self, size: int) -> int:
        return self.memory.allocate_pool(size)

    def ex_allocate_pool_with_tag(self, size: int, tag: int = 0) -> int:
        return self.memory.allocate_pool(size, tag)

    def ex_free_pool(self, address: int) -> int:
        return self.memory.free(address)

    def ex_query_pool_block_size(self, address: int) -> dict[str, Any]:
        return self.memory.query_allocation_size(address)

    def ex_query_nonvolatile_setting(
        self,
        setting_id: int,
        type_address: int = 0,
        value_address: int = 0,
        value_length: int = 0,
        result_length_address: int = 0,
    ) -> dict[str, Any]:
        if setting_id not in self._nonvolatile_settings:
            self.trace.add(
                "hardware",
                "query_nonvolatile_setting",
                "not_found",
                setting_id=setting_id,
                type_address=type_address,
                value_address=value_address,
                value_length=value_length,
                result_length_address=result_length_address,
            )
            return {
                "status": XboxStatus.OBJECT_NAME_NOT_FOUND,
                "value": None,
                "required_length": 0,
                "setting_type": None,
            }
        value = self._nonvolatile_settings[setting_id]
        required_length = 4 if isinstance(value, int) else len(bytes(value))
        self.trace.add(
            "hardware",
            "query_nonvolatile_setting",
            setting_id=setting_id,
            type_address=type_address,
            value_address=value_address,
            value_length=value_length,
            result_length_address=result_length_address,
            value=value,
            required_length=required_length,
        )
        return {
            "status": XboxStatus.SUCCESS,
            "value": value,
            "required_length": required_length,
            "setting_type": 4 if isinstance(value, int) else 3,
        }

    def mm_allocate_contiguous_memory(self, size: int) -> int:
        return self.memory.allocate_contiguous_memory(size)

    def mm_allocate_contiguous_memory_ex(
        self,
        size: int,
        lowest_acceptable_address: int = 0,
        highest_acceptable_address: int = 0xFFFFFFFF,
        boundary_address_multiple: int = 0,
        protect: int = 0,
    ) -> int:
        alignment = boundary_address_multiple if boundary_address_multiple else 0x1000
        address = self.memory.allocate_contiguous_memory(
            size,
            alignment=alignment,
            protection="rw",
        )
        self.trace.add(
            "allocator",
            "allocate_contiguous_ex",
            address=address,
            size=size,
            lowest_acceptable_address=lowest_acceptable_address,
            highest_acceptable_address=highest_acceptable_address,
            boundary_address_multiple=boundary_address_multiple,
            protect=protect,
        )
        return address

    def mm_allocate_system_memory(self, size: int) -> int:
        return self.memory.allocate_system_memory(size)

    def mm_free_system_memory(self, address: int, size: int | None = None) -> int:
        return self.memory.free(address)

    def mm_get_physical_address(self, address: int) -> int:
        return self.memory.get_physical_address(address)

    def mm_claim_gpu_instance_memory(self, size: int = 0x100000) -> int:
        return self.graphics.claim_gpu_instance_memory(size)

    def mm_query_allocation_size(self, address: int) -> dict[str, Any]:
        return self.memory.query_allocation_size(address)

    def mm_query_address_protect(self, address: int) -> dict[str, Any]:
        return self.memory.query_protection(address)

    def mm_query_statistics(self) -> dict[str, Any]:
        return self.memory.query_statistics()

    def mm_lock_unlock_buffer_pages(
        self, address: int, size: int, lock: bool = True
    ) -> int:
        return self.memory.lock_unlock_buffer_pages(address, size, lock)

    def mm_persist_contiguous_memory(
        self, address: int, size: int, persist: bool = True
    ) -> int:
        return self.memory.persist_contiguous_memory(address, size, persist)

    def mm_map_io_space(
        self, physical_address: int, size: int, protection: str = "rw"
    ) -> int:
        return self.memory.map_io_space(physical_address, size, protection)

    def mm_unmap_io_space(self, address: int, size: int | None = None) -> int:
        return self.memory.unmap_io_space(address, size)

    def nt_allocate_virtual_memory(self, size: int, protection: str = "rw") -> int:
        return self.memory.allocate_system_memory(size, protection)

    def nt_free_virtual_memory(self, address: int) -> int:
        return self.memory.free(address)

    def nt_query_virtual_memory(self, address: int) -> dict[str, Any]:
        return self.memory.query(address)

    def nt_protect_virtual_memory(
        self, address: int, size: int, protection: str
    ) -> dict[str, Any]:
        return self.memory.protect(address, size, protection)

    def io_create_device(
        self, name: str | None = None, device_type: int = 0
    ) -> dict[str, Any]:
        device = DeviceObject(name, device_type)
        handle = self.handles.allocate("device", device)
        if name:
            self._devices[name] = device
        self.trace.add(
            "filesystem",
            "create_device",
            handle=handle,
            name=name,
            device_type=device_type,
        )
        return {"status": XboxStatus.SUCCESS, "handle": handle}

    def io_delete_device(self, handle_or_name: int | str) -> int:
        device: DeviceObject | None = None
        if isinstance(handle_or_name, int):
            try:
                device = self.handles.get(handle_or_name, "device")
            except XboxRuntimeError:
                return XboxStatus.INVALID_HANDLE
        else:
            device = self._devices.get(handle_or_name)
        if device is None:
            return XboxStatus.OBJECT_NAME_NOT_FOUND
        device.deleted = True
        self.trace.add("filesystem", "delete_device", device=device)
        return XboxStatus.SUCCESS

    def io_create_symbolic_link(self, link_name: str, target_name: str) -> int:
        self._symbolic_links[link_name] = target_name
        self.trace.add(
            "filesystem",
            "create_symbolic_link",
            link_name=link_name,
            target_name=target_name,
        )
        return XboxStatus.SUCCESS

    def io_delete_symbolic_link(self, link_name: str) -> int:
        existed = link_name in self._symbolic_links
        self._symbolic_links.pop(link_name, None)
        self.trace.add(
            "filesystem", "delete_symbolic_link", link_name=link_name, existed=existed
        )
        return XboxStatus.SUCCESS if existed else XboxStatus.OBJECT_NAME_NOT_FOUND

    def io_build_synchronous_fsd_request(
        self, major_function: str, target: Any = None
    ) -> IoRequest:
        request = IoRequest(major_function, target)
        self.trace.add("filesystem", "build_synchronous_fsd_request", request=request)
        return request

    def io_invalid_device_request(self, request: IoRequest | None = None) -> int:
        if request:
            request.status = XboxStatus.INVALID_PARAMETER
        self.trace.add("filesystem", "invalid_device_request", request=request)
        return XboxStatus.INVALID_PARAMETER

    def io_start_packet(self, device: Any = None, request: IoRequest | None = None) -> int:
        self.trace.add("filesystem", "start_packet", device=device, request=request)
        return XboxStatus.SUCCESS

    def io_start_next_packet(self, device: Any = None) -> int:
        self.trace.add("filesystem", "start_next_packet", device=device)
        return XboxStatus.SUCCESS

    def io_synchronous_fsd_request(
        self, major_function: str, target: Any = None
    ) -> IoRequest:
        request = self.io_build_synchronous_fsd_request(major_function, target)
        request.status = XboxStatus.SUCCESS
        self.trace.add("filesystem", "synchronous_fsd_request", request=request)
        return request

    def io_synchronous_device_io_control_request(self, *args: Any, **kwargs: Any) -> int:
        self.trace.add(
            "filesystem",
            "synchronous_device_io_control",
            arg_count=len(args),
            kwarg_names=sorted(kwargs),
        )
        return XboxStatus.SUCCESS

    def iof_call_driver(self, device: Any = None, request: IoRequest | None = None) -> int:
        if request:
            request.status = XboxStatus.SUCCESS
        self.trace.add("filesystem", "call_driver", device=device, request=request)
        return XboxStatus.SUCCESS

    def iof_complete_request(self, request: IoRequest | None = None, priority_boost: int = 0) -> int:
        if request:
            request.status = XboxStatus.SUCCESS
        self.trace.add(
            "filesystem",
            "complete_request",
            request=request,
            priority_boost=priority_boost,
        )
        return XboxStatus.SUCCESS

    def io_mark_irp_must_complete(self, request: IoRequest | None = None) -> int:
        if request:
            request.must_complete = True
        self.trace.add("filesystem", "mark_irp_must_complete", request=request)
        return XboxStatus.SUCCESS

    def nt_create_file(self, guest_path: str, mode: str = "rb") -> dict[str, Any]:
        return self.filesystem.open_file(guest_path, mode)

    def nt_open_file(self, guest_path: str, mode: str = "rb") -> dict[str, Any]:
        return self.filesystem.open_file(guest_path, mode)

    def nt_read_file(
        self, handle: int, size: int | None = None, offset: int | None = None
    ) -> dict[str, Any]:
        return self.filesystem.read_file(handle, size, offset=offset)

    def nt_query_information_file(self, guest_path_or_handle: str | int) -> dict[str, Any]:
        if isinstance(guest_path_or_handle, int):
            return self.filesystem.query_file_handle_information(guest_path_or_handle)
        return self.filesystem.query_file_information(guest_path_or_handle)

    def nt_query_volume_information_file(self, guest_path: str = "D:\\") -> dict[str, Any]:
        return self.filesystem.query_volume_information(guest_path)

    def nt_query_directory_file(self, guest_path: str = "D:\\") -> dict[str, Any]:
        return self.filesystem.list_directory(guest_path)

    def nt_set_information_file(self, handle: int, information: dict[str, Any] | None = None) -> int:
        if information and "file_information_class" in information:
            result = self.filesystem.set_file_handle_information(
                handle,
                int(information.get("file_information_class", 0)),
                bytes(information.get("payload", b"")),
            )
            return result["status"]
        self.trace.add(
            "filesystem",
            "set_information_file",
            handle=handle,
            information=information or {},
        )
        return XboxStatus.SUCCESS

    def nt_write_file(
        self, handle: int, payload: bytes, offset: int | None = None
    ) -> dict[str, Any]:
        return self.filesystem.write_file(handle, payload, offset=offset)

    def nt_delete_file(self, guest_path: str) -> int:
        return self.filesystem.delete_file(guest_path)

    def nt_fs_control_file(self, *args: Any, **kwargs: Any) -> int:
        self.trace.add(
            "filesystem",
            "fs_control_file",
            arg_count=len(args),
            kwarg_names=sorted(kwargs),
        )
        return XboxStatus.SUCCESS

    def nt_flush_buffers_file(self, handle: int) -> int:
        self.trace.add("filesystem", "flush_buffers_file", handle=handle)
        return XboxStatus.SUCCESS

    def nt_device_io_control_file(self, *args: Any, **kwargs: Any) -> int:
        self.trace.add(
            "filesystem",
            "device_io_control_file",
            "modeled",
            arg_count=len(args),
            kwarg_names=sorted(kwargs),
        )
        return XboxStatus.SUCCESS

    def nt_open_symbolic_link_object(self, name: str) -> dict[str, Any]:
        if name not in self._symbolic_links:
            self.trace.add("filesystem", "open_symbolic_link", "not_found", name=name)
            return {"status": XboxStatus.OBJECT_NAME_NOT_FOUND, "handle": None}
        handle = self.handles.allocate(
            "symbolic_link", {"name": name, "target": self._symbolic_links[name]}
        )
        self.trace.add(
            "filesystem",
            "open_symbolic_link",
            name=name,
            target=self._symbolic_links[name],
            handle=handle,
        )
        return {
            "status": XboxStatus.SUCCESS,
            "handle": handle,
            "name": name,
            "target": self._symbolic_links[name],
        }

    def nt_query_symbolic_link_object(self, name_or_handle: str | int | None = None) -> dict[str, Any]:
        name: str | None
        if isinstance(name_or_handle, int):
            try:
                link = self.handles.get(name_or_handle, "symbolic_link")
            except XboxRuntimeError as exc:
                return {"status": XboxStatus.INVALID_HANDLE, "target": None, "error": str(exc)}
            name = str(link.get("name", ""))
            target = str(link.get("target", ""))
        elif name_or_handle in self._symbolic_links:
            name = str(name_or_handle)
            target = self._symbolic_links[name]
        else:
            name = name_or_handle
            target = "D:\\" if not name or "Cdrom" in name or "D:" in name else None
        status = XboxStatus.SUCCESS if target else XboxStatus.OBJECT_NAME_NOT_FOUND
        self.trace.add(
            "filesystem",
            "query_symbolic_link",
            name=name,
            target=target,
            status_hex=_hex32(status),
        )
        return {"status": status, "name": name, "target": target}

    def nt_close(self, handle: int) -> int:
        return self.handles.close(handle)

    def nt_yield_execution(self) -> int:
        self.clock.advance_100ns(0)
        self.trace.add("threading", "yield_execution")
        return XboxStatus.SUCCESS

    def ob_reference_object_by_handle(self, handle: int) -> dict[str, Any]:
        try:
            obj = self.handles.get(handle)
        except XboxRuntimeError as exc:
            return {"status": XboxStatus.INVALID_HANDLE, "object": None, "error": str(exc)}
        self.trace.add("object_manager", "reference_by_handle", handle=handle)
        return {"status": XboxStatus.SUCCESS, "object": obj}

    def ob_reference_object_by_name(self, name: str) -> dict[str, Any]:
        if name in self._devices:
            obj: Any = self._devices[name]
        elif name in self._symbolic_links:
            obj = {"name": name, "target": self._symbolic_links[name]}
        else:
            self.trace.add("object_manager", "reference_by_name", "not_found", name=name)
            return {"status": XboxStatus.OBJECT_NAME_NOT_FOUND, "object": None}
        self.trace.add("object_manager", "reference_by_name", name=name, object=obj)
        return {"status": XboxStatus.SUCCESS, "object": obj}

    def obf_reference_object(self, obj: Any) -> Any:
        self.trace.add("object_manager", "reference_object", object=obj)
        return obj

    def obf_dereference_object(self, obj: Any) -> int:
        self.trace.add("object_manager", "dereference_object", object=obj)
        return XboxStatus.SUCCESS

    def ob_open_object_by_name(self, name: str) -> dict[str, Any]:
        referenced = self.ob_reference_object_by_name(name)
        if referenced["status"] != XboxStatus.SUCCESS:
            return {"status": referenced["status"], "handle": None}
        handle = self.handles.allocate("object", referenced["object"])
        self.trace.add("object_manager", "open_object_by_name", name=name, handle=handle)
        return {"status": XboxStatus.SUCCESS, "handle": handle}

    def ob_make_temporary_object(self, obj: Any) -> int:
        self.trace.add("object_manager", "make_temporary_object", object=obj)
        return XboxStatus.SUCCESS

    def ke_query_performance_counter(self) -> int:
        return self.clock.query_performance_counter()

    def ke_query_performance_frequency(self) -> int:
        return self.clock.query_performance_frequency()

    def ke_query_system_time(self) -> int:
        return self.clock.query_system_time()

    def ke_query_interrupt_time(self) -> int:
        return self.clock.query_interrupt_time()

    def ke_system_time(self) -> int:
        return self.clock.query_system_time()

    def ke_tick_count(self) -> int:
        tick_count = self.clock.query_interrupt_time() // 100_000
        self.trace.add("threading", "ke_tick_count", tick_count=tick_count)
        return tick_count

    def ke_delay_execution_thread(self, interval_100ns: int) -> int:
        return self.clock.delay_execution_thread(interval_100ns)

    def ke_stall_execution_processor(self, microseconds: int) -> int:
        return self.clock.stall_execution_processor(microseconds)

    def ke_raise_irql_to_dpc_level(self) -> int:
        previous = self._irql
        self._irql = max(self._irql, 2)
        self.trace.add("threading", "raise_irql_to_dpc", previous=previous, irql=self._irql)
        return previous

    def ke_raise_irql_to_synch_level(self) -> int:
        previous = self._irql
        self._irql = max(self._irql, 3)
        self.trace.add(
            "threading", "raise_irql_to_synch", previous=previous, irql=self._irql
        )
        return previous

    def kf_lower_irql(self, irql: int = 0) -> int:
        previous = self._irql
        self._irql = max(irql, 0)
        self.trace.add("threading", "lower_irql", previous=previous, irql=self._irql)
        return XboxStatus.SUCCESS

    def kf_raise_irql(self, irql: int) -> int:
        previous = self._irql
        self._irql = max(irql, self._irql)
        self.trace.add("threading", "raise_irql", previous=previous, irql=self._irql)
        return previous

    def ke_get_current_irql(self) -> int:
        self.trace.add("threading", "get_current_irql", irql=self._irql)
        return self._irql

    def nt_create_event(
        self, manual_reset: bool = True, initial_state: bool = False
    ) -> int:
        return self.sync.create_event(
            manual_reset=manual_reset, initial_state=initial_state
        )

    def ke_set_event(self, handle: int) -> int:
        return self.sync.set_event(handle)

    def ke_reset_event(self, handle: int) -> int:
        return self.sync.reset_event(handle)

    def nt_wait_for_single_object(
        self, handle: int, timeout_100ns: int | None = None
    ) -> int:
        return self.sync.wait_for_single_object(handle, timeout_100ns=timeout_100ns)

    def ke_wait_for_multiple_objects(
        self,
        handles: Iterable[int],
        *,
        wait_all: bool = False,
        timeout_100ns: int | None = None,
    ) -> int:
        statuses = [
            self.sync.wait_for_single_object(handle, timeout_100ns=0)
            for handle in handles
        ]
        ready = all(status == XboxStatus.WAIT_0 for status in statuses) if wait_all else any(
            status == XboxStatus.WAIT_0 for status in statuses
        )
        if ready:
            status = XboxStatus.WAIT_0
        else:
            if timeout_100ns:
                self.clock.advance_100ns(abs(timeout_100ns))
            status = XboxStatus.WAIT_TIMEOUT
        self.trace.add(
            "threading",
            "wait_multiple",
            wait_all=wait_all,
            status_hex=_hex32(status),
            object_count=len(statuses),
        )
        return status

    def nt_create_semaphore(self, initial_count: int, limit: int) -> int:
        return self.sync.create_semaphore(initial_count, limit)

    def nt_release_semaphore(self, handle: int, release_count: int = 1) -> dict[str, Any]:
        return self.sync.release_semaphore(handle, release_count)

    def nt_create_timer(self) -> int:
        return self.sync.create_timer()

    def ke_initialize_timer_ex(self, timer_address: int, timer_type: int = 0) -> None:
        self.sync.initialize_timer(timer_address, timer_type)
        return None

    def nt_set_timer(self, handle: int, due_time_100ns: int, period_ms: int = 0) -> int:
        return self.sync.set_timer_handle(handle, due_time_100ns, period_ms)

    def ke_set_timer(
        self,
        timer_address: int,
        due_time_low: int,
        due_time_high: int = 0,
        dpc_address: int = 0,
    ) -> bool:
        return self.sync.set_kernel_timer(
            timer_address,
            due_time_low,
            due_time_high,
            dpc_address or None,
        )

    def ke_set_timer_ex(
        self,
        timer_address: int,
        due_time_low: int,
        due_time_high: int = 0,
        period_ms: int = 0,
        dpc_address: int = 0,
    ) -> bool:
        return self.sync.set_kernel_timer(
            timer_address,
            due_time_low,
            due_time_high,
            dpc_address or None,
            period_ms=period_ms,
        )

    def nt_cancel_timer(self, handle: int) -> int:
        return self.sync.cancel_timer(handle)

    def ke_cancel_timer(self, handle: int) -> int:
        return self.sync.cancel_timer(handle)

    def nt_query_event(self, handle: int) -> dict[str, Any]:
        self.trace.add("threading", "query_event", handle=handle)
        return {"status": XboxStatus.SUCCESS, "handle": handle}

    def nt_query_timer(self, handle: int) -> dict[str, Any]:
        self.trace.add("threading", "query_timer", handle=handle)
        return {"status": XboxStatus.SUCCESS, "handle": handle}

    def nt_set_system_time(self, filetime: int) -> int:
        return self.clock.set_system_time(filetime)

    def ps_create_system_thread(
        self,
        start_address: int | None = None,
        parameter: int | None = None,
        suspended: bool = False,
    ) -> int:
        return self.sync.create_system_thread(
            start_address=start_address, parameter=parameter, suspended=suspended
        )

    def ps_create_system_thread_ex(
        self,
        thread_handle_address: int,
        thread_extra_size: int,
        kernel_stack_size: int,
        tls_data_size: int,
        thread_id_address: int,
        start_context1: int,
        start_context2: int,
        create_suspended: int,
        debug_stack: int,
        start_routine: int,
    ) -> dict[str, Any]:
        suspended = bool(create_suspended)
        handle = self.sync.create_system_thread(
            start_address=start_routine or None,
            parameter=start_context1,
            suspended=suspended,
            start_context1=start_context1,
            start_context2=start_context2,
            thread_extra_size=thread_extra_size,
            kernel_stack_size=kernel_stack_size,
            tls_data_size=tls_data_size,
            thread_handle_address=thread_handle_address or None,
            thread_id_address=thread_id_address or None,
            debug_stack=bool(debug_stack),
        )
        return {
            "status": XboxStatus.SUCCESS,
            "handle": handle,
            "thread_id": handle,
            "thread_handle_address": thread_handle_address,
            "thread_id_address": thread_id_address,
            "start_address": start_routine,
            "start_context1": start_context1,
            "start_context2": start_context2,
            "suspended": suspended,
            "debug_stack": bool(debug_stack),
        }

    def ke_get_current_thread(self) -> int:
        return self.sync.current_thread

    def nt_suspend_thread(self, handle: int) -> int:
        return self.sync.suspend_thread(handle)

    def nt_resume_thread(self, handle: int) -> int:
        return self.sync.resume_thread(handle)

    def ps_terminate_system_thread(self, status: int = XboxStatus.SUCCESS) -> int:
        return self.sync.terminate_thread(status=status)

    def ke_set_base_priority_thread(self, handle: int, priority: int) -> int:
        return self.sync.set_thread_priority(handle, priority, base=True)

    def ke_set_priority_thread(self, handle: int, priority: int) -> int:
        return self.sync.set_thread_priority(handle, priority, base=False)

    def ke_set_disable_boost_thread(self, handle: int, disable: bool) -> int:
        return self.sync.set_thread_disable_boost(handle, disable)

    def ke_initialize_dpc(
        self, dpc_id: int, routine: int | None = None, context: int | None = None
    ) -> DpcObject:
        return self.sync.initialize_dpc(dpc_id, routine, context)

    def ke_insert_queue_dpc(self, dpc_id: int) -> bool:
        return self.sync.insert_queue_dpc(dpc_id)

    def ke_remove_queue_dpc(self, dpc_id: int) -> bool:
        return self.sync.remove_queue_dpc(dpc_id)

    def ke_initialize_interrupt(
        self, interrupt_id: int, vector: int, service_routine: int | None = None
    ) -> InterruptObject:
        return self.sync.initialize_interrupt(interrupt_id, vector, service_routine)

    def ke_connect_interrupt(self, interrupt_id: int) -> bool:
        return self.sync.connect_interrupt(interrupt_id)

    def ke_disconnect_interrupt(self, interrupt_id: int) -> bool:
        return self.sync.disconnect_interrupt(interrupt_id)

    def ke_synchronize_execution(
        self, interrupt_id: int, routine: Callable[..., Any] | None = None
    ) -> Any:
        return self.sync.synchronize_execution(interrupt_id, routine)

    def ke_bug_check(self, code: int = 0, *parameters: int) -> int:
        return self.sync.bugcheck(code, *parameters)

    def ke_restore_floating_point_state(self, state: dict[str, Any] | None = None) -> int:
        self.trace.add("threading", "restore_floating_point_state", state=state or {})
        return XboxStatus.SUCCESS

    def ke_save_floating_point_state(self) -> dict[str, Any]:
        state = {"control_word": 0x037F, "status_word": 0}
        self.trace.add("threading", "save_floating_point_state", state=state)
        return state

    def ke_test_alert_thread(self, mode: int = 0) -> bool:
        self.trace.add("threading", "test_alert_thread", mode=mode, alert=False)
        return False

    def ps_query_statistics(self) -> dict[str, Any]:
        snapshot = self.handles.snapshot()
        self.trace.add("threading", "query_process_statistics", snapshot=snapshot)
        return {"status": XboxStatus.SUCCESS, **snapshot}

    def rtl_enter_critical_section(self, address: int) -> int:
        return self.sync.enter_critical_section(address)

    def rtl_leave_critical_section(self, address: int) -> int:
        return self.sync.leave_critical_section(address)

    def rtl_initialize_critical_section(self, address: int) -> int:
        self.trace.add("runtime", "initialize_critical_section", address=address)
        return XboxStatus.SUCCESS

    def rtl_init_ansi_string(
        self, destination_address: int = 0, source_address: int = 0
    ) -> dict[str, Any]:
        result = {
            "destination_address": destination_address,
            "source_address": source_address,
        }
        self.trace.add("runtime", "init_ansi_string", result=result)
        return result

    def rtl_equal_string(self, left: str, right: str, case_insensitive: bool = False) -> bool:
        if case_insensitive:
            result = left.casefold() == right.casefold()
        else:
            result = left == right
        self.trace.add(
            "runtime",
            "equal_string",
            left_length=len(left),
            right_length=len(right),
            case_insensitive=case_insensitive,
            result=result,
        )
        return result

    def rtl_unicode_string_to_ansi_string(self, text: str) -> bytes:
        encoded = text.encode("ascii", errors="replace")
        self.trace.add(
            "runtime",
            "unicode_to_ansi",
            input_length=len(text),
            output_length=len(encoded),
        )
        return encoded

    def rtl_ansi_string_to_unicode_string(self, data: bytes | str) -> str:
        if isinstance(data, bytes):
            text = data.decode("ascii", errors="replace")
        else:
            text = data
        self.trace.add("runtime", "ansi_to_unicode", input_length=len(data), text=text)
        return text

    def rtl_compare_memory_ulong(self, payload: bytes, pattern: int) -> int:
        pattern_bytes = (pattern & 0xFFFFFFFF).to_bytes(4, "little")
        matched = 0
        for offset in range(0, len(payload) - 3, 4):
            if payload[offset : offset + 4] != pattern_bytes:
                break
            matched += 4
        self.trace.add(
            "runtime",
            "compare_memory_ulong",
            bytes=len(payload),
            pattern=pattern,
            matched=matched,
        )
        return matched

    def rtl_time_fields_to_time(
        self,
        year: int,
        month: int,
        day: int,
        hour: int = 0,
        minute: int = 0,
        second: int = 0,
        milliseconds: int = 0,
    ) -> int:
        value = dt.datetime(
            year, month, day, hour, minute, second, milliseconds * 1000, tzinfo=dt.UTC
        )
        unix_100ns = int(value.timestamp() * 10_000_000)
        filetime = unix_100ns + 116444736000000000
        self.trace.add("runtime", "time_fields_to_time", filetime=filetime)
        return filetime

    def rtl_time_to_time_fields(self, filetime: int) -> dict[str, int]:
        timestamp = (filetime - 116444736000000000) / 10_000_000
        value = dt.datetime.fromtimestamp(timestamp, tz=dt.UTC)
        fields = {
            "year": value.year,
            "month": value.month,
            "day": value.day,
            "hour": value.hour,
            "minute": value.minute,
            "second": value.second,
            "milliseconds": value.microsecond // 1000,
        }
        self.trace.add("runtime", "time_to_time_fields", filetime=filetime, fields=fields)
        return fields

    def rtl_raise_exception(self, exception: Any = None) -> int:
        self.trace.add("runtime", "raise_exception", "exception", exception=exception)
        return XboxStatus.SUCCESS

    def rtl_unwind(self, target_frame: int | None = None, target_ip: int | None = None) -> int:
        self.trace.add("runtime", "unwind", target_frame=target_frame, target_ip=target_ip)
        return XboxStatus.SUCCESS

    def av_set_display_mode(
        self,
        width: int = 640,
        height: int = 480,
        bpp: int = 32,
        refresh_rate: int = 60,
        flags: int = 0,
    ) -> int:
        return self.graphics.set_display_mode(width, height, bpp, refresh_rate, flags)

    def av_send_tv_encoder_option(self, option: int, value: int = 0) -> int:
        return self.graphics.send_tv_encoder_option(option, value)

    def av_set_saved_data_address(self, address: int) -> int:
        return self.graphics.set_saved_data_address(address)

    def av_get_saved_data_address(self) -> int:
        return self.graphics.get_saved_data_address()

    def xbox_hardware_info(self) -> dict[str, Any]:
        info = {
            "flags": 0,
            "gpu_revision": 0,
            "memory_megabytes": 64,
            "emulated": True,
        }
        self.trace.add("hardware", "xbox_hardware_info", info=info)
        return info

    def xbox_kernel_version(self) -> dict[str, int]:
        version = {"major": 1, "minor": 0, "build": 5838, "qfe": 0}
        self.trace.add("hardware", "xbox_kernel_version", version=version)
        return version

    def xbox_zero_key(self) -> bytes:
        self.trace.add("hardware", "xbox_zero_key")
        return b"\x00" * 16

    def xc_sha_init(self) -> ShaContext:
        return self.crypto.sha_init()

    def xc_sha_update(self, context: ShaContext, payload: bytes) -> int:
        return self.crypto.sha_update(context, payload)

    def xc_sha_final(self, context: ShaContext) -> bytes:
        return self.crypto.sha_final(context)

    def xc_rc4_key(self, key: bytes) -> Rc4Key:
        return self.crypto.rc4_key(key)

    def xc_rc4_crypt(self, key: Rc4Key | bytes, payload: bytes) -> bytes:
        return self.crypto.rc4_crypt(key, payload)

    def xc_hmac(self, key: bytes, payload: bytes, payload2: bytes = b"") -> bytes:
        return self.crypto.hmac_sha1(key, payload, payload2)

    def xc_des_key_parity(self, key: bytes) -> bytes:
        return self.crypto.des_key_parity(key)

    def xc_key_table(
        self, cipher: int | bytes, key: bytes | None = None
    ) -> dict[str, Any]:
        if isinstance(cipher, bytes) and key is None:
            return self.crypto.key_table(0, cipher)
        if not isinstance(cipher, int) or key is None:
            raise XboxRuntimeError("XcKeyTable requires a cipher id and key bytes")
        return self.crypto.key_table(cipher, key)

    def xc_block_crypt_cbc(
        self,
        key_table: dict[str, Any],
        iv: bytes,
        payload: bytes,
        operation: int = 1,
    ) -> bytes:
        return self.crypto.block_crypt_cbc(key_table, iv, payload, operation)

    def xc_mod_exp(
        self,
        base: int | bytes,
        exponent: int | bytes,
        modulus: int | bytes,
        size: int | None = None,
    ) -> int | bytes:
        return self.crypto.mod_exp(base, exponent, modulus, size)

    def xc_verify_pkcs1_signature(
        self, signature: bytes, public_key: Any, digest: bytes
    ) -> bool:
        return self.crypto.verify_pkcs1_signature(signature, public_key, digest)

    def hal_disk_cache_partition_count(self) -> int:
        value = self._kernel_variables["HalDiskCachePartitionCount"].value
        self.trace.add("hardware", "disk_cache_partition_count", value=value)
        return value

    def hal_disk_model_number(self) -> str:
        value = self._kernel_variables["HalDiskModelNumber"].value
        self.trace.add("hardware", "disk_model_number", value=value)
        return value

    def hal_disk_serial_number(self) -> str:
        value = self._kernel_variables["HalDiskSerialNumber"].value
        self.trace.add("hardware", "disk_serial_number", value=value)
        return value

    def hal_get_interrupt_vector(self, bus_interrupt_level: int = 0, bus_interrupt_vector: int = 0) -> int:
        vector = 0x20 + ((bus_interrupt_level or bus_interrupt_vector) & 0x1F)
        self.trace.add(
            "hardware",
            "get_interrupt_vector",
            bus_interrupt_level=bus_interrupt_level,
            bus_interrupt_vector=bus_interrupt_vector,
            vector=vector,
        )
        return vector

    def hal_read_write_pci_space(
        self, bus: int, slot: int, offset: int, payload: bytes | None = None
    ) -> bytes:
        result = payload if payload is not None else b"\x00" * 4
        self.trace.add(
            "hardware",
            "read_write_pci_space",
            bus=bus,
            slot=slot,
            offset=offset,
            bytes=len(result),
            write=payload is not None,
        )
        return result

    def hal_register_shutdown_notification(self, callback: int | None = None) -> int:
        self.trace.add("hardware", "register_shutdown_notification", callback=callback)
        return XboxStatus.SUCCESS

    def hal_return_to_firmware(self, routine: int = 0) -> int:
        self.trace.add("hardware", "return_to_firmware", routine=routine)
        return XboxStatus.SUCCESS

    def phy_get_link_state(self) -> dict[str, Any]:
        state = {"status": XboxStatus.SUCCESS, "link_up": False, "speed_mbps": 0}
        self.trace.add("hardware", "phy_get_link_state", state=state)
        return state

    def phy_initialize(self) -> int:
        self.trace.add("hardware", "phy_initialize")
        return XboxStatus.SUCCESS

    def rtl_fill_memory(self, address: int, size: int, value: int) -> int:
        self.memory.fill(address, value, size)
        return XboxStatus.SUCCESS

    def rtl_zero_memory(self, address: int, size: int) -> int:
        self.memory.zero(address, size)
        return XboxStatus.SUCCESS

    def rtl_move_memory(self, destination: int, source: int, size: int) -> int:
        self.memory.move(destination, source, size)
        return XboxStatus.SUCCESS

    def rtl_nt_status_to_dos_error(self, status: int) -> int:
        mapping = {
            XboxStatus.SUCCESS: 0,
            XboxStatus.NO_SUCH_FILE: 2,
            XboxStatus.ACCESS_DENIED: 5,
            XboxStatus.INVALID_HANDLE: 6,
            XboxStatus.INVALID_PARAMETER: 87,
            XboxStatus.NOT_IMPLEMENTED: 120,
        }
        result = mapping.get(status, 1)
        self.trace.add("runtime", "nt_status_to_dos_error", status=status, dos=result)
        return result

    def xe_load_section(self, section_name: str | None = None) -> int:
        self.trace.add("loader", "xe_load_section", section_name=section_name)
        return XboxStatus.SUCCESS

    def xe_unload_section(self, section_name: str | None = None) -> int:
        self.trace.add("loader", "xe_unload_section", section_name=section_name)
        return XboxStatus.SUCCESS

    def xe_image_file_name(self) -> str:
        image = "D:\\default.xbe"
        self.trace.add("loader", "xe_image_file_name", image=image)
        return image

    def hal_is_reset_or_shutdown_pending(self) -> bool:
        self.trace.add("hardware", "is_reset_or_shutdown_pending", pending=False)
        return False

    def hal_initiate_shutdown(self) -> int:
        self.trace.add("hardware", "initiate_shutdown")
        return XboxStatus.SUCCESS

    def summary(self) -> dict[str, Any]:
        behavior_counts = Counter(shim.behavior for shim in self._registered.values())
        subsystem_counts = Counter(shim.subsystem for shim in self._registered.values())
        return {
            "registered_kernel_shim_count": len(self._registered),
            "registered_kernel_shims": [
                shim.to_dict() for shim in self.registered_shims
            ],
            "registered_behavior_counts": dict(sorted(behavior_counts.items())),
            "registered_subsystem_counts": dict(sorted(subsystem_counts.items())),
            "open_handles": self.handles.snapshot(),
            "open_files": self.filesystem.open_file_snapshot(),
            "threads": self.sync.thread_snapshot(),
            "allocation_count": len(self.memory.allocations),
            "allocations": [allocation.to_dict() for allocation in self.memory.allocations],
            "display_mode": self.graphics.display_mode.to_dict(),
            "input": self.input.snapshot(),
            "audio_initialized": self.audio.initialized,
            "audio_streams": self.audio.stream_snapshot(),
            "clock": self.clock.snapshot(),
            "determinism": {
                "clock": "manual_100ns_counter",
                "handles": "monotonic_4_byte_stride",
                "filesystem_reads": "seeked_stream_reads_with_fixed_latency",
                "input": "sequenced_snapshots",
                "audio": "queued_pcm_byte_budget",
            },
            "trace": self.trace.to_list(),
        }

    def _allocate_host_target(self) -> int:
        target = self._next_target
        self._next_target += self.config.host_target_stride
        if target > 0xFFFFFFFF:
            raise XboxRuntimeError("host shim target address space exhausted")
        return target


def _handler(name: str, behavior: str = "implemented") -> tuple[str, str]:
    return name, behavior


_IMPLEMENTED_KERNEL_HANDLERS: dict[str, tuple[str, str]] = {
    "AvGetSavedDataAddress": _handler("av_get_saved_data_address"),
    "AvSendTVEncoderOption": _handler("av_send_tv_encoder_option"),
    "AvSetDisplayMode": _handler("av_set_display_mode"),
    "AvSetSavedDataAddress": _handler("av_set_saved_data_address"),
    "DbgPrint": _handler("dbg_print"),
    "ExAllocatePoolWithTag": _handler("ex_allocate_pool_with_tag"),
    "ExEventObjectType": _handler("kernel_variable", "data"),
    "ExFreePool": _handler("ex_free_pool"),
    "ExQueryNonVolatileSetting": _handler("ex_query_nonvolatile_setting"),
    "ExQueryPoolBlockSize": _handler("ex_query_pool_block_size"),
    "HalBootSMCVideoMode": _handler("kernel_variable", "data"),
    "HalDiskCachePartitionCount": _handler("kernel_variable", "data"),
    "HalDiskModelNumber": _handler("kernel_variable", "data"),
    "HalDiskSerialNumber": _handler("kernel_variable", "data"),
    "HalGetInterruptVector": _handler("hal_get_interrupt_vector"),
    "HalInitiateShutdown": _handler("hal_initiate_shutdown"),
    "HalIsResetOrShutdownPending": _handler("hal_is_reset_or_shutdown_pending"),
    "HalReadWritePCISpace": _handler("hal_read_write_pci_space"),
    "HalRegisterShutdownNotification": _handler("hal_register_shutdown_notification"),
    "HalReturnToFirmware": _handler("hal_return_to_firmware"),
    "IdexChannelObject": _handler("kernel_variable", "data"),
    "IoBuildSynchronousFsdRequest": _handler("io_build_synchronous_fsd_request"),
    "IoCreateDevice": _handler("io_create_device"),
    "IoCreateSymbolicLink": _handler("io_create_symbolic_link"),
    "IoDeleteSymbolicLink": _handler("io_delete_symbolic_link"),
    "IoFileObjectType": _handler("kernel_variable", "data"),
    "IoInvalidDeviceRequest": _handler("io_invalid_device_request"),
    "IoMarkIrpMustComplete": _handler("io_mark_irp_must_complete"),
    "IoStartNextPacket": _handler("io_start_next_packet"),
    "IoStartPacket": _handler("io_start_packet"),
    "IoSynchronousDeviceIoControlRequest": _handler("io_synchronous_device_io_control_request"),
    "IoSynchronousFsdRequest": _handler("io_synchronous_fsd_request"),
    "IofCallDriver": _handler("iof_call_driver"),
    "IofCompleteRequest": _handler("iof_complete_request"),
    "KeBugCheck": _handler("ke_bug_check"),
    "KeCancelTimer": _handler("ke_cancel_timer"),
    "KeConnectInterrupt": _handler("ke_connect_interrupt"),
    "KeDelayExecutionThread": _handler("ke_delay_execution_thread"),
    "KeDisconnectInterrupt": _handler("ke_disconnect_interrupt"),
    "KeInitializeDpc": _handler("ke_initialize_dpc"),
    "KeInitializeInterrupt": _handler("ke_initialize_interrupt"),
    "KeInitializeTimerEx": _handler("ke_initialize_timer_ex"),
    "KeInsertQueueDpc": _handler("ke_insert_queue_dpc"),
    "KeQueryPerformanceCounter": _handler("ke_query_performance_counter"),
    "KeQueryPerformanceFrequency": _handler("ke_query_performance_frequency"),
    "KeQuerySystemTime": _handler("ke_query_system_time"),
    "KeRaiseIrqlToDpcLevel": _handler("ke_raise_irql_to_dpc_level"),
    "KeRemoveQueueDpc": _handler("ke_remove_queue_dpc"),
    "KeRestoreFloatingPointState": _handler("ke_restore_floating_point_state"),
    "KeSaveFloatingPointState": _handler("ke_save_floating_point_state"),
    "KeSetBasePriorityThread": _handler("ke_set_base_priority_thread"),
    "KeSetEvent": _handler("ke_set_event"),
    "KeSetTimer": _handler("ke_set_timer"),
    "KeSetTimerEx": _handler("ke_set_timer_ex"),
    "KeStallExecutionProcessor": _handler("ke_stall_execution_processor"),
    "KeSynchronizeExecution": _handler("ke_synchronize_execution"),
    "KeTickCount": _handler("ke_tick_count"),
    "KeWaitForMultipleObjects": _handler("ke_wait_for_multiple_objects"),
    "KeWaitForSingleObject": _handler("nt_wait_for_single_object"),
    "KfLowerIrql": _handler("kf_lower_irql"),
    "KfRaiseIrql": _handler("kf_raise_irql"),
    "LaunchDataPage": _handler("kernel_variable", "data"),
    "MmAllocateContiguousMemory": _handler("mm_allocate_contiguous_memory"),
    "MmAllocateContiguousMemoryEx": _handler("mm_allocate_contiguous_memory_ex"),
    "MmClaimGpuInstanceMemory": _handler("mm_claim_gpu_instance_memory"),
    "MmFreeContiguousMemory": _handler("ex_free_pool"),
    "MmGetPhysicalAddress": _handler("mm_get_physical_address"),
    "MmLockUnlockBufferPages": _handler("mm_lock_unlock_buffer_pages"),
    "MmLockUnlockPhysicalPage": _handler("mm_lock_unlock_buffer_pages"),
    "MmPersistContiguousMemory": _handler("mm_persist_contiguous_memory"),
    "MmQueryAddressProtect": _handler("mm_query_address_protect"),
    "MmQueryAllocationSize": _handler("mm_query_allocation_size"),
    "MmQueryStatistics": _handler("mm_query_statistics"),
    "MmSetAddressProtect": _handler("nt_protect_virtual_memory"),
    "NtAllocateVirtualMemory": _handler("nt_allocate_virtual_memory"),
    "NtClose": _handler("nt_close"),
    "NtCreateEvent": _handler("nt_create_event"),
    "NtCreateFile": _handler("nt_create_file"),
    "NtCreateSemaphore": _handler("nt_create_semaphore"),
    "NtDeleteFile": _handler("nt_delete_file"),
    "NtDeviceIoControlFile": _handler("nt_device_io_control_file"),
    "NtFlushBuffersFile": _handler("nt_flush_buffers_file"),
    "NtFreeVirtualMemory": _handler("nt_free_virtual_memory"),
    "NtFsControlFile": _handler("nt_fs_control_file"),
    "NtOpenFile": _handler("nt_open_file"),
    "NtOpenSymbolicLinkObject": _handler("nt_open_symbolic_link_object"),
    "NtQueryDirectoryFile": _handler("nt_query_directory_file"),
    "NtQueryInformationFile": _handler("nt_query_information_file"),
    "NtQuerySymbolicLinkObject": _handler("nt_query_symbolic_link_object"),
    "NtQueryVirtualMemory": _handler("nt_query_virtual_memory"),
    "NtQueryVolumeInformationFile": _handler("nt_query_volume_information_file"),
    "NtReadFile": _handler("nt_read_file"),
    "NtReleaseSemaphore": _handler("nt_release_semaphore"),
    "NtSetEvent": _handler("ke_set_event"),
    "NtSetInformationFile": _handler("nt_set_information_file"),
    "NtSetSystemTime": _handler("nt_set_system_time"),
    "NtSetTimerEx": _handler("nt_set_timer"),
    "NtWaitForSingleObject": _handler("nt_wait_for_single_object"),
    "NtWaitForSingleObjectEx": _handler("nt_wait_for_single_object"),
    "NtWriteFile": _handler("nt_write_file"),
    "NtYieldExecution": _handler("nt_yield_execution"),
    "ObReferenceObjectByHandle": _handler("ob_reference_object_by_handle"),
    "ObReferenceObjectByName": _handler("ob_reference_object_by_name"),
    "ObfDereferenceObject": _handler("obf_dereference_object"),
    "PhyGetLinkState": _handler("phy_get_link_state"),
    "PhyInitialize": _handler("phy_initialize"),
    "PsCreateSystemThreadEx": _handler("ps_create_system_thread_ex"),
    "PsTerminateSystemThread": _handler("ps_terminate_system_thread"),
    "PsThreadObjectType": _handler("kernel_variable", "data"),
    "RtlAnsiStringToUnicodeString": _handler("rtl_ansi_string_to_unicode_string"),
    "RtlCompareMemoryUlong": _handler("rtl_compare_memory_ulong"),
    "RtlEnterCriticalSection": _handler("rtl_enter_critical_section"),
    "RtlEqualString": _handler("rtl_equal_string"),
    "RtlInitAnsiString": _handler("rtl_init_ansi_string"),
    "RtlInitializeCriticalSection": _handler("rtl_initialize_critical_section"),
    "RtlLeaveCriticalSection": _handler("rtl_leave_critical_section"),
    "RtlNtStatusToDosError": _handler("rtl_nt_status_to_dos_error"),
    "RtlRaiseException": _handler("rtl_raise_exception"),
    "RtlTimeFieldsToTime": _handler("rtl_time_fields_to_time"),
    "RtlTimeToTimeFields": _handler("rtl_time_to_time_fields"),
    "RtlUnicodeStringToAnsiString": _handler("rtl_unicode_string_to_ansi_string"),
    "RtlUnwind": _handler("rtl_unwind"),
    "XboxAlternateSignatureKeys": _handler("kernel_variable", "data"),
    "XboxHDKey": _handler("kernel_variable", "data"),
    "XboxHardwareInfo": _handler("kernel_variable", "data"),
    "XboxKrnlVersion": _handler("kernel_variable", "data"),
    "XboxLANKey": _handler("kernel_variable", "data"),
    "XboxSignatureKey": _handler("kernel_variable", "data"),
    "XcBlockCryptCBC": _handler("xc_block_crypt_cbc"),
    "XcDESKeyParity": _handler("xc_des_key_parity"),
    "XcHMAC": _handler("xc_hmac"),
    "XcKeyTable": _handler("xc_key_table"),
    "XcModExp": _handler("xc_mod_exp"),
    "XcRC4Crypt": _handler("xc_rc4_crypt"),
    "XcRC4Key": _handler("xc_rc4_key"),
    "XcSHAFinal": _handler("xc_sha_final"),
    "XcSHAInit": _handler("xc_sha_init"),
    "XcSHAUpdate": _handler("xc_sha_update"),
    "XcVerifyPKCS1Signature": _handler("xc_verify_pkcs1_signature"),
    "XeImageFileName": _handler("xe_image_file_name"),
    "XeLoadSection": _handler("xe_load_section"),
    "XePublicKeyData": _handler("kernel_variable", "data"),
    "XeUnloadSection": _handler("xe_unload_section"),
}


def _kernel_import_subsystem(name: str) -> str:
    if name in {"LaunchDataPage", "XeImageFileName"}:
        return "loader"
    if name in {"IdexChannelObject", "HalDiskCachePartitionCount", "HalDiskModelNumber", "HalDiskSerialNumber"}:
        return "hardware"
    if name.startswith("Phy"):
        return "hardware"
    if name.startswith(("ExAllocate", "ExFree", "ExQueryPool", "MmAllocate", "MmFree")):
        return "allocator"
    if name.startswith("Ex") and "ObjectType" in name:
        return "object_manager"
    if name.startswith("ExQueryNonVolatile"):
        return "hardware"
    if name.startswith("Nt") and any(
        token in name
        for token in (
            "AllocateVirtualMemory",
            "FreeVirtualMemory",
            "ProtectVirtualMemory",
            "QueryVirtualMemory",
        )
    ):
        return "allocator"
    if name in {"NtClose"}:
        return "object_manager"
    if name.startswith(("Io", "Iof")) or (
        name.startswith("Nt")
        and any(
            token in name
            for token in (
                "File",
                "Directory",
                "Volume",
                "IoCompletion",
                "SymbolicLink",
            )
        )
    ):
        return "filesystem"
    if name.startswith("Av") or name in {"MmClaimGpuInstanceMemory"}:
        return "rendering"
    if name.startswith(("Ke", "Kf")) or (
        name.startswith("Nt")
        and any(
            token in name
            for token in (
                "Event",
                "Semaphore",
                "Timer",
                "Thread",
                "Mutant",
                "Wait",
                "Apc",
                "Yield",
                "SystemTime",
            )
        )
    ):
        return "threading"
    if name.startswith("Mm"):
        return "allocator"
    if name.startswith("Rtl") or name.startswith("Interlocked"):
        return "runtime"
    if name.startswith("Dbg"):
        return "diagnostics"
    if name.startswith("Xc"):
        return "crypto"
    if name.startswith("Hal") or name.startswith("Xbox"):
        return "hardware"
    if name.startswith("Ob"):
        return "object_manager"
    if name.startswith("Ps"):
        return "threading"
    if name.startswith("Xe"):
        return "loader"
    return "unknown"


def imported_ordinals_from_loader_summary(summary: dict[str, Any]) -> tuple[int, ...]:
    return tuple(
        resolution["ordinal"]
        for resolution in summary.get("unresolved_imports", [])
        if resolution.get("namespace") == "kernel"
    )


def all_known_kernel_ordinals() -> tuple[int, ...]:
    return tuple(sorted(KERNEL_EXPORT_NAMES))


def implemented_kernel_names() -> tuple[str, ...]:
    return tuple(sorted(_IMPLEMENTED_KERNEL_HANDLERS))


def implemented_kernel_ordinals() -> tuple[int, ...]:
    names = set(_IMPLEMENTED_KERNEL_HANDLERS)
    return tuple(
        ordinal
        for ordinal, name in sorted(KERNEL_EXPORT_NAMES.items())
        if name in names
    )


def unique_ordinals(*groups: Iterable[int]) -> tuple[int, ...]:
    return tuple(sorted(set(itertools.chain.from_iterable(groups))))
