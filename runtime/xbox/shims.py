#!/usr/bin/env python3
"""Deterministic host-side Xbox runtime shims.

The shims in this module are intentionally callable Python models, not a CPU
ABI bridge. The loader can bind import thunks to these handlers today, while a
future executor/recompiler can adapt guest calling conventions to the same
subsystem methods.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
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
    host_stub_base: int = 0xE0000000
    host_stub_stride: int = 0x10
    allocation_base: int = 0x10000000
    contiguous_allocation_base: int = 0x20000000
    performance_frequency: int = 10_000_000
    system_time_filetime: int = 116444736000000000


@dataclass
class GuestFile:
    guest_path: str
    host_path: Path
    mode: str
    position: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "guest_path": self.guest_path,
            "host_path": str(self.host_path),
            "mode": self.mode,
            "position": self.position,
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
    """Read-only guest filesystem view rooted at the extracted local disc tree."""

    _DEVICE_PREFIXES = (
        "\\Device\\Cdrom0\\",
        "\\Device\\CdRom0\\",
        "\\Device\\Harddisk0\\Partition0\\",
        "\\Device\\Harddisk0\\Partition1\\",
        "\\??\\D:\\",
        "D:\\",
        "Cdrom0:\\",
    )

    def __init__(
        self,
        trace: ShimTraceLog,
        handles: GuestHandleTable,
        extracted_disc_root: Path | None,
    ) -> None:
        self._trace = trace
        self._handles = handles
        self._root = extracted_disc_root.resolve() if extracted_disc_root else None

    @property
    def root(self) -> Path | None:
        return self._root

    def resolve_guest_path(self, guest_path: str) -> Path:
        if self._root is None:
            raise XboxPathError("no extracted disc root is configured")
        parts = self._guest_path_parts(guest_path)
        current = self._root
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
            resolved.relative_to(self._root)
        except ValueError as exc:
            raise XboxPathError(f"guest path escapes extracted root: {guest_path}") from exc
        self._trace.add(
            "filesystem",
            "resolve_path",
            guest_path=guest_path,
            host_path=resolved,
        )
        return resolved

    def query_file_information(self, guest_path: str) -> dict[str, Any]:
        try:
            host_path = self.resolve_guest_path(guest_path)
        except XboxPathError as exc:
            self._trace.add(
                "filesystem", "query_file", "error", guest_path=guest_path, error=str(exc)
            )
            return {"status": XboxStatus.OBJECT_NAME_NOT_FOUND, "error": str(exc)}

        exists = host_path.exists()
        info = {
            "status": XboxStatus.SUCCESS if exists else XboxStatus.OBJECT_NAME_NOT_FOUND,
            "guest_path": guest_path,
            "host_path": str(host_path),
            "exists": exists,
            "is_directory": host_path.is_dir() if exists else False,
            "size": host_path.stat().st_size if exists and host_path.is_file() else 0,
        }
        self._trace.add("filesystem", "query_file", **info)
        return info

    def open_file(self, guest_path: str, mode: str = "rb") -> dict[str, Any]:
        if mode not in {"r", "rb"}:
            self._trace.add(
                "filesystem", "open_file", "error", guest_path=guest_path, mode=mode
            )
            return {"status": XboxStatus.ACCESS_DENIED, "handle": None}
        try:
            host_path = self.resolve_guest_path(guest_path)
        except XboxPathError as exc:
            return {"status": XboxStatus.OBJECT_NAME_NOT_FOUND, "handle": None, "error": str(exc)}
        if not host_path.is_file():
            self._trace.add(
                "filesystem",
                "open_file",
                "error",
                guest_path=guest_path,
                host_path=host_path,
            )
            return {"status": XboxStatus.NO_SUCH_FILE, "handle": None}

        file = GuestFile(guest_path, host_path, mode)
        handle = self._handles.allocate("file", file)
        self._trace.add(
            "filesystem",
            "open_file",
            guest_path=guest_path,
            host_path=host_path,
            handle=handle,
            size=host_path.stat().st_size,
        )
        return {"status": XboxStatus.SUCCESS, "handle": handle}

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

        payload = file.host_path.read_bytes()
        start = min(file.position, len(payload))
        end = len(payload) if size is None else min(start + max(size, 0), len(payload))
        chunk = payload[start:end]
        file.position = end
        status = XboxStatus.SUCCESS if chunk or start < len(payload) else XboxStatus.END_OF_FILE
        self._trace.add(
            "filesystem",
            "read_file",
            handle=handle,
            guest_path=file.guest_path,
            offset=start,
            requested_size=size,
            bytes_read=len(chunk),
            status_hex=_hex32(status),
        )
        return {"status": status, "data": chunk, "bytes_read": len(chunk)}

    def list_directory(self, guest_path: str = "D:\\") -> dict[str, Any]:
        try:
            host_path = self.resolve_guest_path(guest_path)
        except XboxPathError as exc:
            return {"status": XboxStatus.OBJECT_NAME_NOT_FOUND, "entries": [], "error": str(exc)}
        if not host_path.is_dir():
            return {"status": XboxStatus.NO_SUCH_FILE, "entries": []}
        entries = sorted(child.name for child in host_path.iterdir())
        self._trace.add(
            "filesystem", "list_directory", guest_path=guest_path, entry_count=len(entries)
        )
        return {"status": XboxStatus.SUCCESS, "entries": entries}

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
    due_time_100ns: int | None = None
    period_ms: int = 0
    signaled: bool = False


@dataclass
class ThreadObject:
    start_address: int | None = None
    parameter: int | None = None
    suspended: bool = False
    priority: int = 8


class XboxSynchronizationShim:
    """Kernel event, semaphore, timer, and thread placeholders."""

    def __init__(
        self, trace: ShimTraceLog, handles: GuestHandleTable, clock: XboxClockShim
    ) -> None:
        self._trace = trace
        self._handles = handles
        self._clock = clock
        self._critical_sections: set[int] = set()
        self._current_thread = self._handles.allocate("thread", ThreadObject())

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

    def set_timer(self, handle: int, due_time_100ns: int, period_ms: int = 0) -> int:
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
    ) -> int:
        handle = self._handles.allocate(
            "thread", ThreadObject(start_address, parameter, suspended)
        )
        self._trace.add(
            "threading",
            "create_system_thread",
            handle=handle,
            start_address=start_address,
            parameter=parameter,
            suspended=suspended,
        )
        return handle

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


class XboxInputShim:
    def __init__(self, trace: ShimTraceLog) -> None:
        self._trace = trace
        self._controllers = {port: ControllerState() for port in range(4)}

    def set_controller_state(self, port: int, state: ControllerState) -> None:
        self._validate_port(port)
        self._controllers[port] = state
        self._trace.add("input", "set_controller_state", port=port, state=state)

    def poll_controller(self, port: int) -> ControllerState:
        self._validate_port(port)
        state = self._controllers[port]
        self._trace.add("input", "poll_controller", port=port, state=state)
        return state

    def poll_all(self) -> dict[int, ControllerState]:
        self._trace.add("input", "poll_all")
        return dict(self._controllers)

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


class XboxAudioShim:
    def __init__(self, trace: ShimTraceLog, handles: GuestHandleTable) -> None:
        self._trace = trace
        self._handles = handles
        self.initialized = False

    def initialize(self) -> int:
        self.initialized = True
        self._trace.add("audio", "initialize")
        return XboxStatus.SUCCESS

    def create_stream(self, format_tag: str = "pcm") -> int:
        self.initialized = True
        handle = self._handles.allocate("audio_stream", AudioStream(format_tag))
        self._trace.add("audio", "create_stream", handle=handle, format_tag=format_tag)
        return handle

    def submit_buffer(self, handle: int, payload: bytes) -> int:
        stream = self._handles.get(handle, "audio_stream")
        stream.queued_bytes += len(payload)
        self._trace.add(
            "audio", "submit_buffer", handle=handle, bytes=len(payload)
        )
        return XboxStatus.SUCCESS


class XboxCryptoShim:
    def __init__(self, trace: ShimTraceLog) -> None:
        self._trace = trace

    def sha1(self, payload: bytes) -> bytes:
        digest = hashlib.sha1(payload, usedforsecurity=False).digest()
        self._trace.add("crypto", "sha1", bytes=len(payload))
        return digest

    def hmac_sha1(self, key: bytes, payload: bytes) -> bytes:
        digest = hmac.new(key, payload, hashlib.sha1).digest()
        self._trace.add("crypto", "hmac_sha1", key_bytes=len(key), bytes=len(payload))
        return digest

    def rc4_crypt(self, key: bytes, payload: bytes) -> bytes:
        state = list(range(256))
        j = 0
        key_bytes = key or b"\x00"
        for i in range(256):
            j = (j + state[i] + key_bytes[i % len(key_bytes)]) & 0xFF
            state[i], state[j] = state[j], state[i]
        output = bytearray()
        i = j = 0
        for byte in payload:
            i = (i + 1) & 0xFF
            j = (j + state[i]) & 0xFF
            state[i], state[j] = state[j], state[i]
            output.append(byte ^ state[(state[i] + state[j]) & 0xFF])
        self._trace.add("crypto", "rc4_crypt", key_bytes=len(key), bytes=len(payload))
        return bytes(output)


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
            self.trace, self.handles, self.config.extracted_disc_root
        )
        self.input = XboxInputShim(self.trace)
        self.graphics = XboxGraphicsShim(self.trace, self.memory)
        self.audio = XboxAudioShim(self.trace, self.handles)
        self.crypto = XboxCryptoShim(self.trace)
        self._next_target = self.config.host_stub_base
        self._registered: dict[int, RuntimeShim] = {}
        self._irql = 0

    @property
    def registered_shims(self) -> tuple[RuntimeShim, ...]:
        return tuple(sorted(self._registered.values(), key=lambda item: item.ordinal))

    def register_kernel_imports(
        self,
        resolver: ImportResolver,
        imported_ordinals: Iterable[int] | None = None,
        *,
        include_placeholders: bool = True,
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
            if handler_info is None and not include_placeholders:
                continue
            handler_name, behavior = (
                handler_info if handler_info is not None else ("unimplemented_kernel_call", "stub")
            )
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

    def _handler_for_registered_shim(
        self, name: str, handler_name: str, subsystem: str
    ) -> Callable[..., Any]:
        if handler_name == "generic_success":
            return lambda *args, **kwargs: self.generic_success(
                name, subsystem, *args, **kwargs
            )
        return getattr(self, handler_name)

    def generic_success(
        self, name: str, subsystem: str, *args: Any, **kwargs: Any
    ) -> int:
        self.trace.add(
            subsystem,
            name,
            "modeled",
            arg_count=len(args),
            kwarg_names=sorted(kwargs),
        )
        return XboxStatus.SUCCESS

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

    def mm_allocate_contiguous_memory(self, size: int) -> int:
        return self.memory.allocate_contiguous_memory(size)

    def mm_allocate_system_memory(self, size: int) -> int:
        return self.memory.allocate_system_memory(size)

    def mm_free_system_memory(self, address: int, size: int | None = None) -> int:
        return self.memory.free(address)

    def mm_get_physical_address(self, address: int) -> int:
        return self.memory.get_physical_address(address)

    def mm_claim_gpu_instance_memory(self, size: int = 0x100000) -> int:
        return self.graphics.claim_gpu_instance_memory(size)

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

    def nt_create_file(self, guest_path: str, mode: str = "rb") -> dict[str, Any]:
        return self.filesystem.open_file(guest_path, mode)

    def nt_open_file(self, guest_path: str, mode: str = "rb") -> dict[str, Any]:
        return self.filesystem.open_file(guest_path, mode)

    def nt_read_file(
        self, handle: int, size: int | None = None, offset: int | None = None
    ) -> dict[str, Any]:
        return self.filesystem.read_file(handle, size, offset=offset)

    def nt_query_information_file(self, guest_path: str) -> dict[str, Any]:
        return self.filesystem.query_file_information(guest_path)

    def nt_flush_buffers_file(self, handle: int) -> int:
        self.trace.add("filesystem", "flush_buffers_file", handle=handle)
        return XboxStatus.SUCCESS

    def nt_device_io_control_file(self, *args: Any, **kwargs: Any) -> int:
        self.trace.add(
            "filesystem",
            "device_io_control_file",
            "stub",
            arg_count=len(args),
            kwarg_names=sorted(kwargs),
        )
        return XboxStatus.SUCCESS

    def nt_query_symbolic_link_object(self, name: str | None = None) -> dict[str, Any]:
        target = "D:\\" if not name or "Cdrom" in name or "D:" in name else None
        status = XboxStatus.SUCCESS if target else XboxStatus.OBJECT_NAME_NOT_FOUND
        self.trace.add(
            "filesystem",
            "query_symbolic_link",
            name=name,
            target=target,
            status_hex=_hex32(status),
        )
        return {"status": status, "target": target}

    def nt_close(self, handle: int) -> int:
        return self.handles.close(handle)

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

    def nt_set_timer(self, handle: int, due_time_100ns: int, period_ms: int = 0) -> int:
        return self.sync.set_timer(handle, due_time_100ns, period_ms)

    def nt_cancel_timer(self, handle: int) -> int:
        self.trace.add("threading", "cancel_timer", handle=handle)
        return XboxStatus.SUCCESS

    def nt_query_event(self, handle: int) -> dict[str, Any]:
        self.trace.add("threading", "query_event", handle=handle)
        return {"status": XboxStatus.SUCCESS, "handle": handle}

    def nt_query_timer(self, handle: int) -> dict[str, Any]:
        self.trace.add("threading", "query_timer", handle=handle)
        return {"status": XboxStatus.SUCCESS, "handle": handle}

    def ps_create_system_thread(
        self,
        start_address: int | None = None,
        parameter: int | None = None,
        suspended: bool = False,
    ) -> int:
        return self.sync.create_system_thread(
            start_address=start_address, parameter=parameter, suspended=suspended
        )

    def ke_get_current_thread(self) -> int:
        return self.sync.current_thread

    def nt_suspend_thread(self, handle: int) -> int:
        return self.sync.suspend_thread(handle)

    def nt_resume_thread(self, handle: int) -> int:
        return self.sync.resume_thread(handle)

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

    def rtl_init_ansi_string(self, text: str = "") -> dict[str, Any]:
        encoded = text.encode("ascii", errors="replace")
        result = {"length": len(encoded), "maximum_length": len(encoded) + 1, "text": text}
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

    def unimplemented_kernel_call(self, *args: Any, **kwargs: Any) -> int:
        self.trace.add(
            "loader",
            "unimplemented_kernel_call",
            "stub",
            arg_count=len(args),
            kwarg_names=sorted(kwargs),
        )
        return XboxStatus.NOT_IMPLEMENTED

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
            "allocation_count": len(self.memory.allocations),
            "allocations": [allocation.to_dict() for allocation in self.memory.allocations],
            "display_mode": self.graphics.display_mode.to_dict(),
            "audio_initialized": self.audio.initialized,
            "trace": self.trace.to_list(),
        }

    def _allocate_host_target(self) -> int:
        target = self._next_target
        self._next_target += self.config.host_stub_stride
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
    "ExAllocatePool": _handler("ex_allocate_pool"),
    "ExAllocatePoolWithTag": _handler("ex_allocate_pool_with_tag"),
    "ExFreePool": _handler("ex_free_pool"),
    "ExQueryNonVolatileSetting": _handler("generic_success"),
    "IoCreateFile": _handler("nt_create_file"),
    "IoDeleteDevice": _handler("generic_success"),
    "IoSynchronousDeviceIoControlRequest": _handler("nt_device_io_control_file", "stub"),
    "KeDelayExecutionThread": _handler("ke_delay_execution_thread"),
    "KeGetCurrentThread": _handler("ke_get_current_thread"),
    "KeInitializeApc": _handler("generic_success"),
    "KeInitializeDpc": _handler("generic_success"),
    "KeInitializeSemaphore": _handler("generic_success"),
    "KeLeaveCriticalRegion": _handler("generic_success"),
    "KeQueryInterruptTime": _handler("ke_query_interrupt_time"),
    "KeQueryPerformanceCounter": _handler("ke_query_performance_counter"),
    "KeQueryPerformanceFrequency": _handler("ke_query_performance_frequency"),
    "KeQuerySystemTime": _handler("ke_query_system_time"),
    "KeRaiseIrqlToDpcLevel": _handler("ke_raise_irql_to_dpc_level"),
    "KeRaiseIrqlToSynchLevel": _handler("ke_raise_irql_to_synch_level"),
    "KeReleaseMutant": _handler("generic_success"),
    "KeReleaseSemaphore": _handler("nt_release_semaphore"),
    "KeResetEvent": _handler("ke_reset_event"),
    "KeSetEvent": _handler("ke_set_event"),
    "KeSetDisableBoostThread": _handler("generic_success"),
    "KeSetPriorityProcess": _handler("generic_success"),
    "KeSetPriorityThread": _handler("generic_success"),
    "KeSetTimerEx": _handler("nt_set_timer"),
    "KeSaveFloatingPointState": _handler("generic_success"),
    "KeStallExecutionProcessor": _handler("ke_stall_execution_processor"),
    "KeSystemTime": _handler("ke_system_time"),
    "KeTestAlertThread": _handler("generic_success"),
    "KeTickCount": _handler("ke_tick_count"),
    "KeWaitForMultipleObjects": _handler("ke_wait_for_multiple_objects"),
    "KeWaitForSingleObject": _handler("nt_wait_for_single_object"),
    "KfLowerIrql": _handler("kf_lower_irql"),
    "KiUnlockDispatcherDatabase": _handler("generic_success"),
    "MmAllocateContiguousMemory": _handler("mm_allocate_contiguous_memory"),
    "MmAllocateContiguousMemoryEx": _handler("mm_allocate_contiguous_memory"),
    "MmAllocateSystemMemory": _handler("mm_allocate_system_memory"),
    "MmClaimGpuInstanceMemory": _handler("mm_claim_gpu_instance_memory"),
    "MmFreeContiguousMemory": _handler("ex_free_pool"),
    "MmFreeSystemMemory": _handler("mm_free_system_memory"),
    "MmGetPhysicalAddress": _handler("mm_get_physical_address"),
    "MmQueryAllocationSize": _handler("nt_query_virtual_memory"),
    "MmSetAddressProtect": _handler("nt_protect_virtual_memory"),
    "MmUnmapIoSpace": _handler("generic_success"),
    "NtAllocateVirtualMemory": _handler("nt_allocate_virtual_memory"),
    "NtCancelTimer": _handler("nt_cancel_timer"),
    "NtClose": _handler("nt_close"),
    "NtCreateDirectoryObject": _handler("generic_success"),
    "NtCreateEvent": _handler("nt_create_event"),
    "NtCreateFile": _handler("nt_create_file"),
    "NtCreateSemaphore": _handler("nt_create_semaphore"),
    "NtCreateTimer": _handler("nt_create_timer"),
    "NtDeviceIoControlFile": _handler("nt_device_io_control_file", "stub"),
    "NtFlushBuffersFile": _handler("nt_flush_buffers_file"),
    "NtFreeVirtualMemory": _handler("nt_free_virtual_memory"),
    "NtOpenFile": _handler("nt_open_file"),
    "NtProtectVirtualMemory": _handler("nt_protect_virtual_memory"),
    "NtQueryFullAttributesFile": _handler("nt_query_information_file"),
    "NtQueryEvent": _handler("nt_query_event"),
    "NtQueryInformationFile": _handler("nt_query_information_file"),
    "NtQuerySymbolicLinkObject": _handler("nt_query_symbolic_link_object"),
    "NtQueryTimer": _handler("nt_query_timer"),
    "NtQueryVirtualMemory": _handler("nt_query_virtual_memory"),
    "NtQueueApcThread": _handler("generic_success"),
    "NtReadFile": _handler("nt_read_file"),
    "NtReleaseSemaphore": _handler("nt_release_semaphore"),
    "NtRemoveIoCompletion": _handler("generic_success"),
    "NtSetEvent": _handler("ke_set_event"),
    "NtSetTimerEx": _handler("nt_set_timer"),
    "NtSuspendThread": _handler("nt_suspend_thread"),
    "NtWaitForSingleObject": _handler("nt_wait_for_single_object"),
    "NtWaitForSingleObjectEx": _handler("nt_wait_for_single_object"),
    "NtWaitForMultipleObjectsEx": _handler("ke_wait_for_multiple_objects"),
    "ObMakeTemporaryObject": _handler("generic_success"),
    "ObOpenObjectByName": _handler("generic_success"),
    "ObReferenceObjectByHandle": _handler("generic_success"),
    "ObReferenceObjectByPointer": _handler("generic_success"),
    "ObSymbolicLinkObjectType": _handler("generic_success"),
    "ObfReferenceObject": _handler("generic_success"),
    "PsCreateSystemThread": _handler("ps_create_system_thread"),
    "PsCreateSystemThreadEx": _handler("ps_create_system_thread"),
    "PsQueryStatistics": _handler("ps_query_statistics"),
    "RtlEnterCriticalSection": _handler("rtl_enter_critical_section"),
    "RtlEqualString": _handler("rtl_equal_string"),
    "RtlFillMemory": _handler("rtl_fill_memory"),
    "RtlInitAnsiString": _handler("rtl_init_ansi_string"),
    "RtlInitializeCriticalSection": _handler("rtl_initialize_critical_section"),
    "RtlLeaveCriticalSection": _handler("rtl_leave_critical_section"),
    "RtlMoveMemory": _handler("rtl_move_memory"),
    "RtlNtStatusToDosError": _handler("rtl_nt_status_to_dos_error"),
    "RtlUnicodeStringToAnsiString": _handler("rtl_unicode_string_to_ansi_string"),
    "RtlUnwind": _handler("generic_success", "stub"),
    "RtlZeroMemory": _handler("rtl_zero_memory"),
    "XboxHDKey": _handler("xbox_zero_key"),
    "XboxHardwareInfo": _handler("xbox_hardware_info"),
    "XboxKrnlVersion": _handler("xbox_kernel_version"),
    "XboxLANKey": _handler("xbox_zero_key"),
    "XboxSignatureKey": _handler("xbox_zero_key"),
    "XboxAlternateSignatureKeys": _handler("xbox_zero_key"),
    "XeImageFileName": _handler("xe_image_file_name"),
    "XeLoadSection": _handler("xe_load_section"),
    "XePublicKeyData": _handler("xbox_zero_key"),
    "XeUnloadSection": _handler("xe_unload_section"),
    "HalIsResetOrShutdownPending": _handler("hal_is_reset_or_shutdown_pending"),
    "HalInitiateShutdown": _handler("hal_initiate_shutdown"),
}


def _kernel_import_subsystem(name: str) -> str:
    if name.startswith(("ExAllocate", "ExFree", "MmAllocate", "MmFree")):
        return "allocator"
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
