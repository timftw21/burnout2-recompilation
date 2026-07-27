#!/usr/bin/env python3
"""Small IA-32 lifter, executor, and C++ emitter for the first recomp prototype."""

from __future__ import annotations

import hashlib
import json
import math
import struct
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from tools.loader.xbe_loader import load_xbe_file
except ModuleNotFoundError:  # pragma: no cover - direct script execution fallback
    import sys

    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from tools.loader.xbe_loader import load_xbe_file


REGISTER_NAMES = ("eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi")
REGISTER_BY_INDEX = {index: name for index, name in enumerate(REGISTER_NAMES)}
XMM_REGISTER_NAMES = tuple(f"xmm{index}" for index in range(8))
XMM_REGISTER_BY_INDEX = {index: name for index, name in enumerate(XMM_REGISTER_NAMES)}
MMX_REGISTER_NAMES = tuple(f"mm{index}" for index in range(8))
MMX_REGISTER_BY_INDEX = {index: name for index, name in enumerate(MMX_REGISTER_NAMES)}
BYTE_REGISTER_BY_INDEX = {
    0: "al",
    1: "cl",
    2: "dl",
    3: "bl",
    4: "ah",
    5: "ch",
    6: "dh",
    7: "bh",
}
SUPPORTED_TARGET_PLATFORM = "windows"
GENERATED_LANGUAGE = "c++17"
FIRST_RENDERER_BACKEND = "vulkan"
BLOCK_TERMINATORS = frozenset({"ret", "jmp", "jmp_far", "jcc", "int3", "int"})
DETERMINISTIC_TSC_STEP = 733_000
RESUMABLE_REPEAT_CHUNK_ITERATIONS = 65_536
DEFAULT_MXCSR = 0x00001F80
DEFAULT_X87_CONTROL_WORD = 0x037F
X87_SAVE_IMAGE_SIZE = 108
X87_STATUS_C0 = 0x0100
X87_STATUS_C1 = 0x0200
X87_STATUS_C2 = 0x0400
X87_STATUS_C3 = 0x4000
X87_STATUS_CONDITION_MASK = X87_STATUS_C0 | X87_STATUS_C1 | X87_STATUS_C2 | X87_STATUS_C3


class RecompilationError(RuntimeError):
    """Base error for recompilation prototype failures."""


class X86DecodeError(RecompilationError):
    """Raised when the prototype decoder reaches an unsupported instruction."""


@dataclass(frozen=True)
class NativeFastPath:
    """Guarded native C++ replacement for a recovered guest function entry."""

    name: str
    guard: str
    body: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "guard": self.guard,
            "body": list(self.body),
        }


class X86ExecutionError(RecompilationError):
    """Raised when lifted code cannot be executed by the trace executor."""

    def __init__(
        self,
        message: str,
        *,
        state: "CpuState | None" = None,
        trace: "ExecutionTrace | None" = None,
        steps: int = 0,
    ) -> None:
        super().__init__(message)
        self.state = state
        self.trace = trace
        self.steps = steps


def _u32(value: int) -> int:
    return value & 0xFFFFFFFF


def _i8(value: int) -> int:
    return value - 0x100 if value & 0x80 else value


def _i32(value: int) -> int:
    return value - 0x1_0000_0000 if value & 0x80000000 else value


def _read_u16(data: bytes, offset: int) -> int:
    if offset + 2 > len(data):
        raise X86DecodeError("truncated u16 immediate")
    return struct.unpack_from("<H", data, offset)[0]


def _read_u32(data: bytes, offset: int) -> int:
    if offset + 4 > len(data):
        raise X86DecodeError("truncated u32 immediate")
    return struct.unpack_from("<I", data, offset)[0]


def _x87_stack_register_index(operand: Operand) -> int:
    if operand.kind != "reg" or operand.reg not in REGISTER_NAMES:
        raise X86DecodeError("invalid x87 stack register encoding")
    return REGISTER_NAMES.index(operand.reg)


def _hex32(value: int) -> str:
    return f"0x{value & 0xFFFFFFFF:08X}"


def _hex64(value: int) -> str:
    return f"0x{value & 0xFFFFFFFFFFFFFFFF:016X}"


def _parity8(value: int) -> bool:
    return bin(value & 0xFF).count("1") % 2 == 0


@dataclass
class CpuFlags:
    cf: bool = False
    pf: bool = False
    af: bool = False
    zf: bool = False
    sf: bool = False
    of: bool = False
    df: bool = False
    interrupt_enabled: bool = True

    def to_dict(self) -> dict[str, bool]:
        return {
            "cf": self.cf,
            "pf": self.pf,
            "af": self.af,
            "zf": self.zf,
            "sf": self.sf,
            "of": self.of,
            "df": self.df,
            "interrupt_enabled": self.interrupt_enabled,
        }


@dataclass
class CpuState:
    registers: dict[str, int] = field(
        default_factory=lambda: {name: 0 for name in REGISTER_NAMES}
    )
    flags: CpuFlags = field(default_factory=CpuFlags)
    eip: int = 0
    fs_base: int = 0
    cs_selector: int = 0x0008
    gdtr_base: int = 0
    gdtr_limit: int = 0xFFFF
    timestamp_counter: int = 0
    mxcsr: int = DEFAULT_MXCSR
    fpu_control_word: int = DEFAULT_X87_CONTROL_WORD
    fpu_status_word: int = 0
    fpu_stack: list[float] = field(default_factory=list)
    xmm_registers: dict[str, tuple[float, float, float, float]] = field(
        default_factory=lambda: {
            name: (0.0, 0.0, 0.0, 0.0) for name in XMM_REGISTER_NAMES
        }
    )
    mmx_registers: dict[str, int] = field(
        default_factory=lambda: {name: 0 for name in MMX_REGISTER_NAMES}
    )

    @classmethod
    def with_registers(cls, **registers: int) -> "CpuState":
        state = cls()
        for name, value in registers.items():
            normalized = name.casefold()
            if normalized == "fs_base":
                state.fs_base = _u32(value)
            elif normalized == "cs_selector":
                state.cs_selector = value & 0xFFFF
            elif normalized == "gdtr_base":
                state.gdtr_base = _u32(value)
            elif normalized == "gdtr_limit":
                state.gdtr_limit = value & 0xFFFF
            elif normalized == "timestamp_counter":
                state.timestamp_counter = value & 0xFFFFFFFFFFFFFFFF
            elif normalized == "mxcsr":
                state.mxcsr = _u32(value)
            elif normalized == "fpu_control_word":
                state.fpu_control_word = value & 0xFFFF
            elif normalized == "fpu_status_word":
                state.fpu_status_word = value & 0xFFFF
            else:
                state.set_register(name, value)
        return state

    def get_register(self, name: str) -> int:
        normalized = name.casefold()
        if normalized not in REGISTER_NAMES:
            raise X86ExecutionError(f"unknown register: {name}")
        return self.registers[normalized]

    def set_register(self, name: str, value: int) -> None:
        normalized = name.casefold()
        if normalized not in REGISTER_NAMES:
            raise X86ExecutionError(f"unknown register: {name}")
        self.registers[normalized] = _u32(value)

    def get_xmm_register(self, name: str) -> tuple[float, float, float, float]:
        normalized = name.casefold()
        if normalized not in XMM_REGISTER_NAMES:
            raise X86ExecutionError(f"unknown XMM register: {name}")
        return self.xmm_registers[normalized]

    def set_xmm_register(self, name: str, value: Iterable[float]) -> None:
        normalized = name.casefold()
        if normalized not in XMM_REGISTER_NAMES:
            raise X86ExecutionError(f"unknown XMM register: {name}")
        lanes = tuple(float(lane) for lane in value)
        if len(lanes) != 4:
            raise X86ExecutionError("XMM register writes require four lanes")
        self.xmm_registers[normalized] = (lanes[0], lanes[1], lanes[2], lanes[3])

    def get_mmx_register(self, name: str) -> int:
        normalized = name.casefold()
        if normalized not in MMX_REGISTER_NAMES:
            raise X86ExecutionError(f"unknown MMX register: {name}")
        return self.mmx_registers[normalized]

    def set_mmx_register(self, name: str, value: int) -> None:
        normalized = name.casefold()
        if normalized not in MMX_REGISTER_NAMES:
            raise X86ExecutionError(f"unknown MMX register: {name}")
        self.mmx_registers[normalized] = value & 0xFFFFFFFFFFFFFFFF

    def to_dict(self) -> dict[str, Any]:
        return {
            "registers": {name: _hex32(self.registers[name]) for name in REGISTER_NAMES},
            "segments": {
                "fs_base": _hex32(self.fs_base),
                "cs_selector": f"0x{self.cs_selector & 0xFFFF:04X}",
                "gdtr_base": _hex32(self.gdtr_base),
                "gdtr_limit": f"0x{self.gdtr_limit & 0xFFFF:04X}",
            },
            "flags": self.flags.to_dict(),
            "eip": self.eip,
            "eip_hex": _hex32(self.eip),
            "timestamp_counter": self.timestamp_counter,
            "timestamp_counter_hex": f"0x{self.timestamp_counter & 0xFFFFFFFFFFFFFFFF:016X}",
            "mxcsr": self.mxcsr,
            "mxcsr_hex": _hex32(self.mxcsr),
            "fpu_control_word": self.fpu_control_word,
            "fpu_control_word_hex": f"0x{self.fpu_control_word & 0xFFFF:04X}",
            "fpu_status_word": self.fpu_status_word,
            "fpu_status_word_hex": f"0x{self.fpu_status_word & 0xFFFF:04X}",
            "fpu_stack_depth": len(self.fpu_stack),
            "xmm_registers": {
                name: list(self.xmm_registers[name]) for name in XMM_REGISTER_NAMES
            },
            "mmx_registers": {
                name: _hex64(self.mmx_registers[name]) for name in MMX_REGISTER_NAMES
            },
        }


@dataclass(frozen=True)
class Operand:
    kind: str
    size: int = 32
    reg: str | None = None
    immediate: int | None = None
    base: str | None = None
    index: str | None = None
    scale: int = 1
    displacement: int = 0
    absolute: int | None = None
    segment: str | None = None

    @classmethod
    def register(cls, reg: str, *, size: int = 32) -> "Operand":
        return cls("reg", size=size, reg=reg)

    @classmethod
    def immediate_u32(cls, value: int) -> "Operand":
        return cls("imm", immediate=_u32(value))

    @classmethod
    def memory(
        cls,
        *,
        base: str | None = None,
        index: str | None = None,
        scale: int = 1,
        displacement: int = 0,
        absolute: int | None = None,
        size: int = 32,
        segment: str | None = None,
    ) -> "Operand":
        return cls(
            "mem",
            size=size,
            base=base,
            index=index,
            scale=scale,
            displacement=displacement,
            absolute=absolute,
            segment=segment,
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind, "size": self.size}
        if self.reg is not None:
            data["reg"] = self.reg
        if self.immediate is not None:
            data["immediate"] = self.immediate
            data["immediate_hex"] = _hex32(self.immediate)
        if self.kind == "mem":
            data.update(
                {
                    "base": self.base,
                    "index": self.index,
                    "scale": self.scale,
                    "displacement": self.displacement,
                    "absolute": self.absolute,
                    "absolute_hex": _hex32(self.absolute)
                    if self.absolute is not None
                    else None,
                    "segment": self.segment,
                }
            )
        return data

    def asm(self) -> str:
        if self.kind == "reg":
            return self.reg or "?"
        if self.kind == "imm":
            return _hex32(self.immediate or 0)
        if self.kind != "mem":
            return "?"
        prefix = f"{self.segment}:" if self.segment else ""
        if self.absolute is not None:
            return f"{prefix}[{_hex32(self.absolute)}]"
        parts: list[str] = []
        if self.base:
            parts.append(self.base)
        if self.index:
            parts.append(f"{self.index}*{self.scale}" if self.scale != 1 else self.index)
        if self.displacement:
            sign = "+" if self.displacement >= 0 else "-"
            magnitude = abs(self.displacement)
            parts.append(f"{sign} 0x{magnitude:X}")
        if not parts:
            parts.append("0")
        text = " ".join(parts)
        return f"{prefix}[{text}]"


@dataclass(frozen=True)
class X86Instruction:
    address: int
    size: int
    mnemonic: str
    operands: tuple[Operand, ...] = ()
    bytes_hex: str = ""
    target: int | None = None
    condition: str | None = None
    ret_stack_adjust: int = 0

    @property
    def next_address(self) -> int:
        return self.address + self.size

    def text(self) -> str:
        if not self.operands:
            return self.mnemonic
        return f"{self.mnemonic} " + ", ".join(operand.asm() for operand in self.operands)

    def to_dict(self, *, include_bytes: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "address": self.address,
            "address_hex": _hex32(self.address),
            "size": self.size,
            "mnemonic": self.mnemonic,
            "operands": [operand.to_dict() for operand in self.operands],
            "text": self.text(),
        }
        if include_bytes:
            data["bytes_hex"] = self.bytes_hex
        if self.target is not None:
            data["target"] = self.target
            data["target_hex"] = _hex32(self.target)
        if self.condition is not None:
            data["condition"] = self.condition
        if self.ret_stack_adjust:
            data["ret_stack_adjust"] = self.ret_stack_adjust
        return data


@dataclass(frozen=True)
class LiftedFunction:
    symbol: str
    base_address: int
    code_size: int
    instructions: tuple[X86Instruction, ...]
    target_platform: str = SUPPORTED_TARGET_PLATFORM
    generated_language: str = GENERATED_LANGUAGE
    renderer_backend: str = FIRST_RENDERER_BACKEND

    @property
    def instruction_count(self) -> int:
        return len(self.instructions)

    @property
    def call_targets(self) -> tuple[int, ...]:
        return tuple(
            instruction.target
            for instruction in self.instructions
            if instruction.mnemonic == "call" and instruction.target is not None
        )

    @property
    def branch_targets(self) -> tuple[int, ...]:
        return tuple(
            instruction.target
            for instruction in self.instructions
            if instruction.mnemonic in {"jmp", "jcc"} and instruction.target is not None
        )

    def to_dict(self, *, include_bytes: bool = False) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "base_address": self.base_address,
            "base_address_hex": _hex32(self.base_address),
            "code_size": self.code_size,
            "instruction_count": self.instruction_count,
            "target_platform": self.target_platform,
            "generated_language": self.generated_language,
            "renderer_backend": self.renderer_backend,
            "call_targets": [_hex32(target) for target in self.call_targets],
            "branch_targets": [_hex32(target) for target in self.branch_targets],
            "instructions": [
                instruction.to_dict(include_bytes=include_bytes)
                for instruction in self.instructions
            ],
        }


@dataclass(frozen=True)
class ExecutionEvent:
    sequence: int
    address: int | None
    operation: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "address": self.address,
            "address_hex": _hex32(self.address) if self.address is not None else None,
            "operation": self.operation,
            "details": self.details,
        }


class ExecutionTrace:
    def __init__(self, *, enabled: bool = True, max_events: int | None = None) -> None:
        self._events: deque[ExecutionEvent] = deque()
        self._enabled = enabled
        self._max_events = max_events if max_events is None else max(1, max_events)
        self._event_count = 0

    @property
    def enabled(self) -> bool:
        """Expose whether callers should construct expensive trace details."""
        return self._enabled

    def add(self, address: int | None, operation: str, **details: Any) -> None:
        if not self._enabled:
            return
        if self._max_events is not None and len(self._events) >= self._max_events:
            self._events.popleft()
        self._events.append(
            ExecutionEvent(
                self._event_count,
                address,
                operation,
                _json_safe(details),
            )
        )
        self._event_count += 1

    def to_list(self) -> list[dict[str, Any]]:
        return [event.to_dict() for event in self._events]

    def last_event(self) -> ExecutionEvent | None:
        return self._events[-1] if self._events else None

    def __iter__(self):
        return iter(self._events)


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, bytes):
        return value.hex().upper()
    if isinstance(value, int):
        return value
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    return str(value)


class SparseMemory:
    """Little-endian sparse u32 memory used by the trace executor."""

    _PAGE_BITS = 12
    _PAGE_SIZE = 1 << _PAGE_BITS
    _PAGE_MASK = _PAGE_SIZE - 1

    def __init__(self, initial: dict[int, bytes | int] | None = None) -> None:
        self._pages: dict[int, bytearray] = {}
        self._written_pages: dict[int, bytearray] = {}
        self._page_generations: dict[int, int] = {}
        # Native execution only needs to revalidate pages changed through the
        # Python memory model since its last host boundary.  Keep the exact set
        # here so that cache coherence does not require scanning every cached
        # page after each HLE call or observer callback.
        self._changed_pages: set[int] = set()
        self._changed_page_ranges: dict[int, tuple[int, int]] = {}
        for address, value in (initial or {}).items():
            if isinstance(value, int):
                self.write_u32(address, value)
            else:
                self.write(address, value)

    def read(self, address: int, size: int) -> bytes:
        if size < 0:
            raise X86ExecutionError("cannot read a negative size")
        payload = bytearray(size)
        cursor = 0
        while cursor < size:
            current = _u32(address + cursor)
            page_number = current >> self._PAGE_BITS
            page_offset = current & self._PAGE_MASK
            chunk_size = min(size - cursor, self._PAGE_SIZE - page_offset)
            page = self._pages.get(page_number)
            if page is not None:
                payload[cursor : cursor + chunk_size] = page[
                    page_offset : page_offset + chunk_size
                ]
            cursor += chunk_size
        return bytes(payload)

    def read_u32(self, address: int) -> int:
        current = _u32(address)
        page_offset = current & self._PAGE_MASK
        if page_offset <= self._PAGE_SIZE - 4:
            page = self._pages.get(current >> self._PAGE_BITS)
            if page is None:
                return 0
            return struct.unpack_from("<I", page, page_offset)[0]
        return struct.unpack("<I", self.read(address, 4))[0]

    def write(self, address: int, payload: bytes) -> None:
        cursor = 0
        while cursor < len(payload):
            current = _u32(address + cursor)
            page_number = current >> self._PAGE_BITS
            page_offset = current & self._PAGE_MASK
            chunk_size = min(len(payload) - cursor, self._PAGE_SIZE - page_offset)
            page = self._pages.setdefault(page_number, bytearray(self._PAGE_SIZE))
            written = self._written_pages.setdefault(
                page_number, bytearray(self._PAGE_SIZE)
            )
            page[page_offset : page_offset + chunk_size] = payload[
                cursor : cursor + chunk_size
            ]
            written[page_offset : page_offset + chunk_size] = b"\x01" * chunk_size
            self._page_generations[page_number] = (
                self._page_generations.get(page_number, 0) + 1
            )
            self._changed_pages.add(page_number)
            changed_range = self._changed_page_ranges.get(page_number)
            if changed_range is None:
                self._changed_page_ranges[page_number] = (
                    page_offset,
                    page_offset + chunk_size,
                )
            else:
                self._changed_page_ranges[page_number] = (
                    min(changed_range[0], page_offset),
                    max(changed_range[1], page_offset + chunk_size),
                )
            cursor += chunk_size

    def _has_byte(self, address: int) -> bool:
        current = _u32(address)
        written = self._written_pages.get(current >> self._PAGE_BITS)
        return bool(written and written[current & self._PAGE_MASK])

    def _read_byte(self, address: int, default: int = 0) -> int:
        current = _u32(address)
        page = self._pages.get(current >> self._PAGE_BITS)
        if page is None or not self._has_byte(current):
            return default
        return page[current & self._PAGE_MASK]

    def write_u32(self, address: int, value: int) -> None:
        current = _u32(address)
        page_offset = current & self._PAGE_MASK
        if page_offset > self._PAGE_SIZE - 4:
            self.write(current, struct.pack("<I", _u32(value)))
            return
        page_number = current >> self._PAGE_BITS
        page = self._pages.setdefault(page_number, bytearray(self._PAGE_SIZE))
        written = self._written_pages.setdefault(
            page_number, bytearray(self._PAGE_SIZE)
        )
        struct.pack_into("<I", page, page_offset, _u32(value))
        written[page_offset : page_offset + 4] = b"\x01\x01\x01\x01"
        self._page_generations[page_number] = (
            self._page_generations.get(page_number, 0) + 1
        )
        self._changed_pages.add(page_number)
        changed_range = self._changed_page_ranges.get(page_number)
        if changed_range is None:
            self._changed_page_ranges[page_number] = (page_offset, page_offset + 4)
        else:
            self._changed_page_ranges[page_number] = (
                min(changed_range[0], page_offset),
                max(changed_range[1], page_offset + 4),
            )

    def snapshot_u32(self, addresses: Iterable[int]) -> dict[int, int]:
        return {address: self.read_u32(address) for address in addresses}

    def storage_summary(self) -> dict[str, int]:
        return {
            "page_count": len(self._pages),
            "written_byte_count": sum(mask.count(1) for mask in self._written_pages.values()),
            "allocated_page_bytes": len(self._pages) * self._PAGE_SIZE * 2,
        }

    @property
    def allocated_page_count(self) -> int:
        return len(self._pages)

    def has_allocated_page(self, address: int) -> bool:
        return (_u32(address) >> self._PAGE_BITS) in self._pages

    native_cache_physical_aliases = False

    def native_cache_address(self, address: int) -> int:
        """Return the address used to key native page-cache entries."""
        return _u32(address)

    def native_resident_page_indices(self) -> tuple[int, ...]:
        """Return materialized pages that native execution must inherit."""
        return tuple(self._pages)

    def native_page_snapshot(self, page_address: int) -> bytes:
        """Materialize one native cache page without invoking read hooks."""
        page = self._pages.get(_u32(page_address) >> self._PAGE_BITS)
        return bytes(page) if page is not None else bytes(self._PAGE_SIZE)

    def page_generation(self, address: int) -> int:
        return self._page_generations.get(_u32(address) >> self._PAGE_BITS, 0)

    def visible_page_generation(self, address: int) -> int:
        """Return the generation visible to host-side snapshot consumers."""
        return self.page_generation(address)

    def consume_changed_pages(self) -> set[int]:
        """Return and clear pages changed since the previous consumer boundary."""
        changed_pages = self._changed_pages
        self._changed_pages = set()
        for page in changed_pages:
            self._changed_page_ranges.pop(page, None)
        return changed_pages

    def consume_changed_page_ranges(self) -> dict[int, tuple[int, int]]:
        """Return exact changed byte spans and clear their page notifications."""
        changed_ranges = self._changed_page_ranges
        self._changed_page_ranges = {}
        self._changed_pages.difference_update(changed_ranges)
        return changed_ranges

    def mark_changed_pages(self, pages: Iterable[int]) -> None:
        """Publish externally changed pages to the next native cache boundary."""
        self._changed_pages.update(int(page) for page in pages)

    def native_page_range_snapshot(self, address: int, size: int) -> bytes:
        """Read a cache-coherence range without invoking subclass read hooks."""
        return SparseMemory.read(self, address, size)


@dataclass
class ExecutionResult:
    state: CpuState
    memory: SparseMemory
    trace: ExecutionTrace
    return_address: int | None = None
    steps: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.to_dict(),
            "trace": self.trace.to_list(),
            "return_address": self.return_address,
            "return_address_hex": _hex32(self.return_address)
            if self.return_address is not None
            else None,
            "steps": self.steps,
        }


CallHandler = Callable[[CpuState, SparseMemory, int, ExecutionTrace], None]
BlockLoader = Callable[[int], LiftedFunction | None]
StepObserver = Callable[[CpuState, SparseMemory, ExecutionTrace, int], None]


class X86Decoder:
    """Decoder for the Milestone 5 IA-32 subset."""

    def decode_function(
        self,
        code: bytes,
        *,
        base_address: int,
        symbol: str = "lifted_function",
        max_instructions: int = 256,
        stop_at_ret: bool = True,
    ) -> LiftedFunction:
        offset = 0
        instructions: list[X86Instruction] = []
        while offset < len(code) and len(instructions) < max_instructions:
            instruction, offset = self._decode_one(code, offset, base_address)
            instructions.append(instruction)
            if stop_at_ret and instruction.mnemonic in {"ret", "int3", "int"}:
                break
        if len(instructions) >= max_instructions and offset < len(code):
            raise X86DecodeError("instruction limit reached before function end")
        return LiftedFunction(
            symbol=symbol,
            base_address=base_address,
            code_size=offset,
            instructions=tuple(instructions),
        )

    def decode_block(
        self,
        code: bytes,
        *,
        base_address: int,
        symbol: str = "lifted_block",
        max_instructions: int = 128,
    ) -> LiftedFunction:
        offset = 0
        instructions: list[X86Instruction] = []
        terminated = False
        while offset < len(code) and len(instructions) < max_instructions:
            instruction, offset = self._decode_one(code, offset, base_address)
            instructions.append(instruction)
            if instruction.mnemonic in BLOCK_TERMINATORS:
                terminated = True
                break
        if not terminated:
            raise X86DecodeError("instruction limit reached before block end")
        return LiftedFunction(
            symbol=symbol,
            base_address=base_address,
            code_size=offset,
            instructions=tuple(instructions),
        )

    def _decode_one(
        self, code: bytes, offset: int, base_address: int
    ) -> tuple[X86Instruction, int]:
        start = offset
        address = base_address + offset
        repeat_prefix = False
        repeat_not_equal_prefix = False
        operand_size = 32
        segment_override: str | None = None
        while True:
            opcode = self._read_u8(code, offset)
            offset += 1
            if opcode == 0x64:
                segment_override = "fs"
                continue
            if opcode == 0x66:
                operand_size = 16
                continue
            if opcode == 0xF2:
                repeat_not_equal_prefix = True
                continue
            if opcode == 0xF3:
                repeat_prefix = True
                continue
            if opcode == 0xF0:
                continue
            break
        self._segment_override = segment_override

        def inst(
            mnemonic: str,
            operands: tuple[Operand, ...] = (),
            *,
            target: int | None = None,
            condition: str | None = None,
            ret_stack_adjust: int = 0,
        ) -> tuple[X86Instruction, int]:
            raw = code[start:offset]
            return (
                X86Instruction(
                    address,
                    offset - start,
                    mnemonic,
                    operands,
                    raw.hex().upper(),
                    target,
                    condition,
                    ret_stack_adjust,
                ),
                offset,
            )

        def scalar_float_operand(operand: Operand) -> Operand:
            if operand.kind != "mem":
                return operand
            return Operand.memory(
                base=operand.base,
                index=operand.index,
                scale=operand.scale,
                displacement=operand.displacement,
                absolute=operand.absolute,
                size=32,
                segment=operand.segment,
            )

        def mmx_operand(operand: Operand) -> Operand:
            if operand.kind == "reg":
                if operand.reg not in REGISTER_NAMES:
                    raise X86DecodeError("invalid MMX register encoding")
                return Operand.register(
                    MMX_REGISTER_BY_INDEX[REGISTER_NAMES.index(operand.reg)],
                    size=64,
                )
            if operand.kind == "mem":
                return Operand.memory(
                    base=operand.base,
                    index=operand.index,
                    scale=operand.scale,
                    displacement=operand.displacement,
                    absolute=operand.absolute,
                    size=64,
                    segment=operand.segment,
                )
            return operand

        if opcode == 0x90:
            return inst("nop")
        if 0x91 <= opcode <= 0x97:
            return inst(
                "xchg",
                (
                    Operand.register("eax", size=operand_size),
                    Operand.register(
                        REGISTER_BY_INDEX[opcode - 0x90],
                        size=operand_size,
                    ),
                ),
            )
        if opcode == 0x9B:
            return inst("fwait")
        if opcode == 0x9E:
            return inst("sahf")
        if opcode == 0xFA:
            return inst("cli")
        if opcode == 0xFB:
            return inst("sti")
        if opcode == 0xFC:
            return inst("cld")
        if opcode == 0xFD:
            return inst("std")
        if repeat_prefix and opcode == 0xAA:
            return inst("rep_stosb")
        if repeat_prefix and opcode == 0xAB:
            return inst("rep_stosd")
        if repeat_prefix and opcode == 0xA4:
            return inst("rep_movsb")
        if repeat_prefix and opcode == 0xA5:
            return inst("rep_movsd")
        if repeat_prefix and opcode == 0xA6:
            return inst("rep_cmpsb")
        if repeat_prefix and opcode == 0xA7:
            return inst("rep_cmpsd")
        if repeat_not_equal_prefix and opcode == 0xAE:
            return inst("repne_scasb")
        if opcode == 0xAA:
            return inst("stosb")
        if opcode == 0xAB:
            return inst("stosd")
        if opcode == 0xA4:
            return inst("movsb")
        if opcode == 0xA5:
            return inst("movsd")
        if opcode == 0xA6:
            return inst("cmpsb")
        if opcode == 0xA7:
            return inst("cmpsd")
        if opcode == 0xAE:
            return inst("scasb")
        if opcode == 0xCC:
            return inst("int3")
        if opcode == 0xCD:
            vector = self._read_u8(code, offset)
            offset += 1
            return inst("int", (Operand.immediate_u32(vector),))
        if opcode == 0xEE:
            return inst(
                "out",
                (
                    Operand.register("dx", size=16),
                    Operand.register("al", size=8),
                ),
            )
        if opcode == 0xEC:
            return inst(
                "in",
                (
                    Operand.register("al", size=8),
                    Operand.register("dx", size=16),
                ),
            )
        if opcode == 0xEA:
            if operand_size == 16:
                target = _read_u16(code, offset)
                offset += 2
            else:
                target = _read_u32(code, offset)
                offset += 4
            selector = _read_u16(code, offset)
            offset += 2
            return inst(
                "jmp_far",
                (Operand.immediate_u32(selector),),
                target=target,
            )
        if opcode == 0xDB:
            extension = self._read_u8(code, offset)
            if extension == 0xE2:
                offset += 1
                return inst("fnclex")
            reg_index, rm_operand, offset = self._decode_modrm(
                code,
                offset,
                operand_size=32,
            )
            if rm_operand.kind != "reg" and reg_index == 0:
                return inst("fild", (rm_operand,))
            if rm_operand.kind != "reg" and reg_index == 3:
                return inst("fistp", (rm_operand,))
            if rm_operand.kind != "reg" and reg_index == 5:
                return inst(
                    "fld",
                    (
                        Operand.memory(
                            base=rm_operand.base,
                            index=rm_operand.index,
                            scale=rm_operand.scale,
                            displacement=rm_operand.displacement,
                            absolute=rm_operand.absolute,
                            size=80,
                            segment=rm_operand.segment,
                        ),
                    ),
                )
            raise X86DecodeError(
                f"unsupported x87 DB {extension:02X} at {_hex32(address)}"
            )
        if opcode == 0xDF:
            reg_index, rm_operand, offset = self._decode_modrm(
                code,
                offset,
                operand_size=64,
            )
            if (
                rm_operand.kind == "reg"
                and rm_operand.reg == "eax"
                and reg_index == 4
            ):
                return inst("fnstsw", (Operand.register("eax", size=16),))
            if reg_index == 5:
                return inst("fild", (rm_operand,))
            if reg_index == 7:
                return inst("fistp", (rm_operand,))
            raise X86DecodeError(
                f"unsupported x87 DF /{reg_index} at {_hex32(address)}"
            )
        if opcode == 0xD8:
            reg_index, rm_operand, offset = self._decode_modrm(
                code,
                offset,
                operand_size=32,
            )
            if rm_operand.kind == "reg":
                st_index = _x87_stack_register_index(rm_operand)
                st_operand = Operand.immediate_u32(st_index)
                if reg_index == 0:
                    return inst("fadd", (st_operand,))
                if reg_index == 1:
                    return inst("fmul", (st_operand,))
                if reg_index == 2:
                    return inst("fcom", (st_operand,))
                if reg_index == 3:
                    return inst("fcomp", (st_operand,))
                if reg_index == 4:
                    return inst("fsub", (st_operand,))
                if reg_index == 5:
                    return inst("fsubr", (st_operand,))
                if reg_index == 6:
                    return inst("fdiv", (st_operand,))
                if reg_index == 7:
                    return inst("fdivr", (st_operand,))
            if reg_index == 0:
                return inst("fadd", (rm_operand,))
            if reg_index == 1:
                return inst("fmul", (rm_operand,))
            if reg_index == 2:
                return inst("fcom", (rm_operand,))
            if reg_index == 3:
                return inst("fcomp", (rm_operand,))
            if reg_index == 4:
                return inst("fsub", (rm_operand,))
            if reg_index == 5:
                return inst("fsubr", (rm_operand,))
            if reg_index == 6:
                return inst("fdiv", (rm_operand,))
            if reg_index == 7:
                return inst("fdivr", (rm_operand,))
            raise X86DecodeError(
                f"unsupported x87 D8 /{reg_index} at {_hex32(address)}"
            )
        if opcode == 0xDA:
            reg_index, rm_operand, offset = self._decode_modrm(
                code,
                offset,
                operand_size=32,
            )
            if rm_operand.kind != "reg":
                if reg_index == 0:
                    return inst("fiadd", (rm_operand,))
                if reg_index == 1:
                    return inst("fimul", (rm_operand,))
                if reg_index == 4:
                    return inst("fisub", (rm_operand,))
                if reg_index == 6:
                    return inst("fidiv", (rm_operand,))
            raise X86DecodeError(
                f"unsupported x87 DA /{reg_index} at {_hex32(address)}"
            )
        if opcode == 0xD9:
            reg_index, rm_operand, offset = self._decode_modrm(
                code,
                offset,
                operand_size=32,
            )
            if rm_operand.kind == "reg":
                st_index = _x87_stack_register_index(rm_operand)
                if reg_index == 0:
                    return inst("fld", (Operand.immediate_u32(st_index),))
                if reg_index == 1:
                    return inst("fxch", (Operand.immediate_u32(st_index),))
                if reg_index == 4 and st_index == 0:
                    return inst("fchs")
                if reg_index == 4 and st_index == 1:
                    return inst("fabs")
                if reg_index == 4 and st_index == 4:
                    return inst("ftst")
                if reg_index == 5:
                    constant_loads = {
                        0: "fld1",
                        1: "fldl2t",
                        2: "fldl2e",
                        3: "fldpi",
                        4: "fldlg2",
                        5: "fldln2",
                        6: "fldz",
                    }
                    constant_load = constant_loads.get(st_index)
                    if constant_load is not None:
                        return inst(constant_load)
                if reg_index == 6 and st_index == 0:
                    return inst("f2xm1")
                if reg_index == 6 and st_index == 1:
                    return inst("fyl2x")
                if reg_index == 6 and st_index == 2:
                    return inst("fptan")
                if reg_index == 6 and st_index == 3:
                    return inst("fpatan")
                if reg_index == 7 and st_index == 4:
                    return inst("frndint")
                if reg_index == 7 and st_index == 5:
                    return inst("fscale")
                if reg_index == 7 and st_index == 2:
                    return inst("fsqrt")
                if reg_index == 7 and st_index == 6:
                    return inst("fsin")
                if reg_index == 7 and st_index == 7:
                    return inst("fcos")
                raise X86DecodeError(
                    f"unsupported x87 D9 register form at {_hex32(address)}"
                )
            if reg_index == 0:
                return inst("fld", (rm_operand,))
            if reg_index == 2:
                return inst("fst", (rm_operand,))
            if reg_index == 3:
                return inst("fstp", (rm_operand,))
            if reg_index == 5:
                rm_operand = Operand.memory(
                    base=rm_operand.base,
                    index=rm_operand.index,
                    scale=rm_operand.scale,
                    displacement=rm_operand.displacement,
                    absolute=rm_operand.absolute,
                    size=16,
                    segment=rm_operand.segment,
                )
                return inst("fldcw", (rm_operand,))
            if reg_index == 7:
                rm_operand = Operand.memory(
                    base=rm_operand.base,
                    index=rm_operand.index,
                    scale=rm_operand.scale,
                    displacement=rm_operand.displacement,
                    absolute=rm_operand.absolute,
                    size=16,
                    segment=rm_operand.segment,
                )
                return inst("fnstcw", (rm_operand,))
            raise X86DecodeError(
                f"unsupported x87 D9 /{reg_index} at {_hex32(address)}"
            )
        if opcode == 0xDC:
            reg_index, rm_operand, offset = self._decode_modrm(
                code,
                offset,
                operand_size=64,
            )
            if rm_operand.kind == "reg":
                st_index = _x87_stack_register_index(rm_operand)
                stack_operations = {
                    0: "fadd_st",
                    1: "fmul_st",
                    4: "fsubr_st",
                    5: "fsub_st",
                    6: "fdivr_st",
                    7: "fdiv_st",
                }
                stack_operation = stack_operations.get(reg_index)
                if stack_operation is not None:
                    return inst(
                        stack_operation,
                        (Operand.immediate_u32(st_index),),
                    )
            else:
                memory_operations = {
                    0: "fadd",
                    1: "fmul",
                    2: "fcom",
                    3: "fcomp",
                    4: "fsub",
                    5: "fsubr",
                    6: "fdiv",
                    7: "fdivr",
                }
                return inst(memory_operations[reg_index], (rm_operand,))
            raise X86DecodeError(
                f"unsupported x87 DC /{reg_index} at {_hex32(address)}"
            )
        if opcode == 0xDD:
            reg_index, rm_operand, offset = self._decode_modrm(
                code,
                offset,
                operand_size=64,
            )
            if rm_operand.kind == "reg":
                st_index = _x87_stack_register_index(rm_operand)
                if reg_index == 0:
                    return inst("ffree", (Operand.immediate_u32(st_index),))
                if reg_index == 2:
                    return inst("fst", (Operand.immediate_u32(st_index),))
                if reg_index == 3:
                    return inst("fstp", (Operand.immediate_u32(st_index),))
            else:
                if reg_index == 0:
                    return inst("fld", (rm_operand,))
                if reg_index == 2:
                    return inst("fst", (rm_operand,))
                if reg_index == 3:
                    return inst("fstp", (rm_operand,))
                if reg_index in {4, 6}:
                    state_operand = Operand.memory(
                        base=rm_operand.base,
                        index=rm_operand.index,
                        scale=rm_operand.scale,
                        displacement=rm_operand.displacement,
                        absolute=rm_operand.absolute,
                        size=X87_SAVE_IMAGE_SIZE * 8,
                        segment=rm_operand.segment,
                    )
                    return inst(
                        "frstor" if reg_index == 4 else "fnsave",
                        (state_operand,),
                    )
                if reg_index == 7:
                    rm_operand = Operand.memory(
                        base=rm_operand.base,
                        index=rm_operand.index,
                        scale=rm_operand.scale,
                        displacement=rm_operand.displacement,
                        absolute=rm_operand.absolute,
                        size=16,
                        segment=rm_operand.segment,
                    )
                    return inst("fnstsw", (rm_operand,))
            raise X86DecodeError(
                f"unsupported x87 DD /{reg_index} at {_hex32(address)}"
            )
        if opcode == 0xDE:
            reg_index, rm_operand, offset = self._decode_modrm(
                code,
                offset,
                operand_size=32,
            )
            if rm_operand.kind == "reg":
                st_index = _x87_stack_register_index(rm_operand)
                if reg_index == 0:
                    return inst("faddp", (Operand.immediate_u32(st_index),))
                if reg_index == 1:
                    return inst("fmulp", (Operand.immediate_u32(st_index),))
                if reg_index == 3 and st_index == 1:
                    return inst("fcompp")
                if reg_index == 4:
                    return inst("fsubrp", (Operand.immediate_u32(st_index),))
                if reg_index == 5:
                    return inst("fsubp", (Operand.immediate_u32(st_index),))
                if reg_index == 6:
                    return inst("fdivrp", (Operand.immediate_u32(st_index),))
                if reg_index == 7:
                    return inst("fdivp", (Operand.immediate_u32(st_index),))
            raise X86DecodeError(
                f"unsupported x87 DE /{reg_index} at {_hex32(address)}"
            )
        if 0x50 <= opcode <= 0x57:
            return inst("push", (Operand.register(REGISTER_BY_INDEX[opcode - 0x50]),))
        if 0x58 <= opcode <= 0x5F:
            return inst("pop", (Operand.register(REGISTER_BY_INDEX[opcode - 0x58]),))
        if 0x40 <= opcode <= 0x47:
            return inst("inc", (Operand.register(REGISTER_BY_INDEX[opcode - 0x40]),))
        if 0x48 <= opcode <= 0x4F:
            return inst("dec", (Operand.register(REGISTER_BY_INDEX[opcode - 0x48]),))
        if 0xB0 <= opcode <= 0xB7:
            value = self._read_u8(code, offset)
            offset += 1
            return inst(
                "mov",
                (
                    Operand.register(BYTE_REGISTER_BY_INDEX[opcode - 0xB0], size=8),
                    Operand.immediate_u32(value),
                ),
            )
        if 0xB8 <= opcode <= 0xBF:
            if operand_size == 16:
                value = _read_u16(code, offset)
                offset += 2
            else:
                value = _read_u32(code, offset)
                offset += 4
            return inst(
                "mov",
                (
                    Operand.register(
                        REGISTER_BY_INDEX[opcode - 0xB8],
                        size=operand_size,
                    ),
                    Operand.immediate_u32(value),
                ),
            )
        if opcode == 0x68:
            value = _read_u32(code, offset)
            offset += 4
            return inst("push", (Operand.immediate_u32(value),))
        if opcode == 0x6A:
            value = _u32(_i8(self._read_u8(code, offset)))
            offset += 1
            return inst("push", (Operand.immediate_u32(value),))
        if opcode in {0x69, 0x6B}:
            reg_index, rm_operand, offset = self._decode_modrm(
                code,
                offset,
                operand_size=operand_size,
            )
            if opcode == 0x69:
                value = _read_u32(code, offset)
                offset += 4
            else:
                value = _u32(_i8(self._read_u8(code, offset)))
                offset += 1
            return inst(
                "imul",
                (
                    Operand.register(
                        REGISTER_BY_INDEX[reg_index],
                        size=operand_size,
                    ),
                    rm_operand,
                    Operand.immediate_u32(value),
                ),
            )
        if opcode in {0x86, 0x87}:
            reg_index, rm_operand, offset = self._decode_modrm(
                code,
                offset,
                operand_size=8 if opcode == 0x86 else operand_size,
            )
            return inst(
                "xchg",
                (
                    rm_operand,
                    Operand.register(
                        BYTE_REGISTER_BY_INDEX[reg_index]
                        if opcode == 0x86
                        else REGISTER_BY_INDEX[reg_index],
                        size=8 if opcode == 0x86 else operand_size,
                    ),
                ),
            )
        if opcode in {0xA0, 0xA1}:
            absolute = _read_u32(code, offset)
            offset += 4
            value_size = 8 if opcode == 0xA0 else operand_size
            return inst(
                "mov",
                (
                    Operand.register(
                        "al" if opcode == 0xA0 else "eax",
                        size=value_size,
                    ),
                    Operand.memory(
                        absolute=absolute,
                        size=value_size,
                        segment=segment_override,
                    ),
                ),
            )
        if opcode in {0xA2, 0xA3}:
            absolute = _read_u32(code, offset)
            offset += 4
            value_size = 8 if opcode == 0xA2 else operand_size
            return inst(
                "mov",
                (
                    Operand.memory(
                        absolute=absolute,
                        size=value_size,
                        segment=segment_override,
                    ),
                    Operand.register(
                        "al" if opcode == 0xA2 else "eax",
                        size=value_size,
                    ),
                ),
            )
        if opcode in {0x88, 0x8A}:
            reg_index, rm_operand, offset = self._decode_modrm(
                code, offset, operand_size=8
            )
            reg_operand = Operand.register(
                BYTE_REGISTER_BY_INDEX[reg_index],
                size=8,
            )
            operands = (
                (rm_operand, reg_operand)
                if opcode == 0x88
                else (reg_operand, rm_operand)
            )
            return inst("mov", operands)
        if opcode == 0x84:
            reg_index, rm_operand, offset = self._decode_modrm(
                code, offset, operand_size=8
            )
            return inst(
                "test",
                (
                    rm_operand,
                    Operand.register(BYTE_REGISTER_BY_INDEX[reg_index], size=8),
                ),
            )
        if opcode in {0x38, 0x3A}:
            reg_index, rm_operand, offset = self._decode_modrm(
                code, offset, operand_size=8
            )
            reg_operand = Operand.register(
                BYTE_REGISTER_BY_INDEX[reg_index],
                size=8,
            )
            operands = (
                (rm_operand, reg_operand)
                if opcode == 0x38
                else (reg_operand, rm_operand)
            )
            return inst("cmp", operands)
        if opcode == 0x24:
            value = self._read_u8(code, offset)
            offset += 1
            return inst(
                "and",
                (Operand.register("al", size=8), Operand.immediate_u32(value)),
            )
        if opcode == 0x04:
            value = self._read_u8(code, offset)
            offset += 1
            return inst(
                "add",
                (Operand.register("al", size=8), Operand.immediate_u32(value)),
            )
        if opcode == 0x0C:
            value = self._read_u8(code, offset)
            offset += 1
            return inst(
                "or",
                (Operand.register("al", size=8), Operand.immediate_u32(value)),
            )
        if opcode == 0x1C:
            value = self._read_u8(code, offset)
            offset += 1
            return inst(
                "sbb",
                (Operand.register("al", size=8), Operand.immediate_u32(value)),
            )
        if opcode == 0x2C:
            value = self._read_u8(code, offset)
            offset += 1
            return inst(
                "sub",
                (Operand.register("al", size=8), Operand.immediate_u32(value)),
            )
        if opcode == 0x34:
            value = self._read_u8(code, offset)
            offset += 1
            return inst(
                "xor",
                (Operand.register("al", size=8), Operand.immediate_u32(value)),
            )
        if opcode == 0x3C:
            value = self._read_u8(code, offset)
            offset += 1
            return inst(
                "cmp",
                (Operand.register("al", size=8), Operand.immediate_u32(value)),
            )
        if opcode == 0xA8:
            value = self._read_u8(code, offset)
            offset += 1
            return inst(
                "test",
                (Operand.register("al", size=8), Operand.immediate_u32(value)),
            )
        if opcode == 0xC3:
            return inst("ret")
        if opcode == 0xC2:
            adjust = _read_u16(code, offset)
            offset += 2
            return inst("ret", ret_stack_adjust=adjust)
        if opcode == 0xC9:
            return inst("leave")
        if opcode == 0x99:
            return inst("cdq")
        if opcode == 0xE8:
            rel = _i32(_read_u32(code, offset))
            offset += 4
            return inst("call", target=_u32(base_address + offset + rel))
        if opcode == 0xE9:
            rel = _i32(_read_u32(code, offset))
            offset += 4
            return inst("jmp", target=_u32(base_address + offset + rel))
        if opcode == 0xEB:
            rel = _i8(self._read_u8(code, offset))
            offset += 1
            return inst("jmp", target=_u32(base_address + offset + rel))
        if opcode == 0xE3:
            rel = _i8(self._read_u8(code, offset))
            offset += 1
            return inst(
                "jcc",
                target=_u32(base_address + offset + rel),
                condition="ecx_zero",
            )
        if 0x70 <= opcode <= 0x7F:
            rel = _i8(self._read_u8(code, offset))
            offset += 1
            condition = _condition_for_short_opcode(opcode)
            return inst(
                "jcc",
                target=_u32(base_address + offset + rel),
                condition=condition,
            )
        if opcode == 0x0F:
            second = self._read_u8(code, offset)
            offset += 1
            if second == 0x01:
                reg_index, rm_operand, offset = self._decode_modrm(
                    code,
                    offset,
                    operand_size=32,
                )
                if reg_index == 0 and rm_operand.kind == "mem":
                    return inst(
                        "sgdt",
                        (
                            Operand.memory(
                                base=rm_operand.base,
                                index=rm_operand.index,
                                scale=rm_operand.scale,
                                displacement=rm_operand.displacement,
                                absolute=rm_operand.absolute,
                                size=48,
                                segment=rm_operand.segment,
                            ),
                        ),
                    )
                raise X86DecodeError(
                    f"unsupported 0F 01 /{reg_index} at {_hex32(address)}"
                )
            if second == 0x09:
                return inst("wbinvd")
            if second == 0x31:
                return inst("rdtsc")
            if second == 0x77:
                return inst("emms")
            if second == 0x18:
                reg_index, rm_operand, offset = self._decode_modrm(
                    code,
                    offset,
                    operand_size=8,
                )
                mnemonic = {
                    0: "prefetchnta",
                    1: "prefetcht0",
                    2: "prefetcht1",
                    3: "prefetcht2",
                }.get(reg_index, "prefetch")
                return inst(mnemonic, (rm_operand,))
            if second in {0x6F, 0x7F, 0xE7}:
                reg_index, rm_operand, offset = self._decode_modrm(
                    code,
                    offset,
                    operand_size=32,
                )
                reg_operand = Operand.register(
                    MMX_REGISTER_BY_INDEX[reg_index],
                    size=64,
                )
                rm_operand = mmx_operand(rm_operand)
                if second == 0x6F:
                    return inst("movq", (reg_operand, rm_operand))
                if second == 0x7F:
                    return inst("movq", (rm_operand, reg_operand))
                return inst("movntq", (rm_operand, reg_operand))
            if repeat_prefix and second in {0x2C, 0x2D}:
                reg_index, rm_operand, offset = self._decode_modrm(
                    code,
                    offset,
                    operand_size=128,
                )
                rm_operand = scalar_float_operand(rm_operand)
                return inst(
                    "cvttss2si" if second == 0x2C else "cvtss2si",
                    (
                        Operand.register(REGISTER_BY_INDEX[reg_index]),
                        rm_operand,
                    ),
                )
            if repeat_prefix and second in {0x10, 0x11, 0x52}:
                reg_index, rm_operand, offset = self._decode_modrm(
                    code,
                    offset,
                    operand_size=128,
                )
                reg_operand = Operand.register(
                    XMM_REGISTER_BY_INDEX[reg_index],
                    size=128,
                )
                rm_operand = scalar_float_operand(rm_operand)
                if second == 0x10:
                    return inst("movss", (reg_operand, rm_operand))
                if second == 0x11:
                    return inst("movss", (rm_operand, reg_operand))
                return inst("rsqrtss", (reg_operand, rm_operand))
            if second in {0x10, 0x11}:
                reg_index, rm_operand, offset = self._decode_modrm(
                    code,
                    offset,
                    operand_size=128,
                )
                reg_operand = Operand.register(
                    XMM_REGISTER_BY_INDEX[reg_index],
                    size=128,
                )
                if second == 0x10:
                    return inst("movups", (reg_operand, rm_operand))
                return inst("movups", (rm_operand, reg_operand))
            if second in {0x13, 0x17}:
                reg_index, rm_operand, offset = self._decode_modrm(
                    code,
                    offset,
                    operand_size=64,
                )
                if rm_operand.kind != "mem":
                    raise X86DecodeError(
                        f"unsupported packed-half register destination at {_hex32(address)}"
                    )
                return inst(
                    "movlps_store" if second == 0x13 else "movhps_store",
                    (
                        rm_operand,
                        Operand.register(XMM_REGISTER_BY_INDEX[reg_index], size=128),
                    ),
                )
            if second in {0x12, 0x14, 0x15, 0x16}:
                reg_index, rm_operand, offset = self._decode_modrm(
                    code,
                    offset,
                    operand_size=128,
                )
                reg_operand = Operand.register(
                    XMM_REGISTER_BY_INDEX[reg_index],
                    size=128,
                )
                if second in {0x12, 0x16} and rm_operand.kind != "reg":
                    raise X86DecodeError(
                        f"unsupported packed-half memory source at {_hex32(address)}"
                    )
                mnemonic = {
                    0x12: "movhlps",
                    0x14: "unpcklps",
                    0x15: "unpckhps",
                    0x16: "movlhps",
                }[second]
                return inst(mnemonic, (reg_operand, rm_operand))
            if second in {0x28, 0x29, 0x51, 0x58, 0x59, 0x5D, 0x5F}:
                reg_index, rm_operand, offset = self._decode_modrm(
                    code,
                    offset,
                    operand_size=128,
                )
                reg_operand = Operand.register(
                    XMM_REGISTER_BY_INDEX[reg_index],
                    size=128,
                )
                if second == 0x28:
                    return inst("movaps", (reg_operand, rm_operand))
                if second == 0x29:
                    return inst("movaps", (rm_operand, reg_operand))
                if second == 0x51:
                    return inst("sqrtps", (reg_operand, rm_operand))
                if rm_operand.kind == "reg" or rm_operand.kind == "mem":
                    mnemonic = {
                        0x58: "addps",
                        0x59: "mulps",
                        0x5D: "minps",
                        0x5F: "maxps",
                    }[second]
                    return inst(mnemonic, (reg_operand, rm_operand))
            if second == 0xC6:
                reg_index, rm_operand, offset = self._decode_modrm(
                    code,
                    offset,
                    operand_size=128,
                )
                immediate = self._read_u8(code, offset)
                offset += 1
                return inst(
                    "shufps",
                    (
                        Operand.register(
                            XMM_REGISTER_BY_INDEX[reg_index],
                            size=128,
                        ),
                        rm_operand,
                        Operand.immediate_u32(immediate),
                    ),
                )
            if 0x80 <= second <= 0x8F:
                rel = _i32(_read_u32(code, offset))
                offset += 4
                condition = _condition_for_short_opcode(second - 0x10)
                return inst(
                    "jcc",
                    target=_u32(base_address + offset + rel),
                    condition=condition,
                )
            if second == 0xB6:
                reg_index, rm_operand, offset = self._decode_modrm(
                    code, offset, operand_size=8
                )
                return inst(
                    "movzx",
                    (
                        Operand.register(
                            REGISTER_BY_INDEX[reg_index],
                            size=operand_size,
                        ),
                        rm_operand,
                    ),
                )
            if second == 0xB7:
                reg_index, rm_operand, offset = self._decode_modrm(
                    code, offset, operand_size=16
                )
                return inst(
                    "movzx",
                    (
                        Operand.register(
                            REGISTER_BY_INDEX[reg_index],
                            size=operand_size,
                        ),
                        rm_operand,
                    ),
                )
            if second == 0xBE:
                reg_index, rm_operand, offset = self._decode_modrm(
                    code, offset, operand_size=8
                )
                return inst(
                    "movsx",
                    (
                        Operand.register(
                            REGISTER_BY_INDEX[reg_index],
                            size=operand_size,
                        ),
                        rm_operand,
                    ),
                )
            if second == 0xBF:
                reg_index, rm_operand, offset = self._decode_modrm(
                    code, offset, operand_size=16
                )
                return inst(
                    "movsx",
                    (
                        Operand.register(
                            REGISTER_BY_INDEX[reg_index],
                            size=operand_size,
                        ),
                        rm_operand,
                    ),
                )
            if second == 0xBC:
                reg_index, rm_operand, offset = self._decode_modrm(
                    code,
                    offset,
                    operand_size=operand_size,
                )
                return inst(
                    "bsf",
                    (
                        Operand.register(
                            REGISTER_BY_INDEX[reg_index],
                            size=operand_size,
                        ),
                        rm_operand,
                    ),
                )
            if second == 0xAF:
                reg_index, rm_operand, offset = self._decode_modrm(
                    code,
                    offset,
                    operand_size=operand_size,
                )
                return inst(
                    "imul",
                    (
                        Operand.register(
                            REGISTER_BY_INDEX[reg_index],
                            size=operand_size,
                        ),
                        rm_operand,
                    ),
                )
            if second in {0xA4, 0xA5, 0xAC, 0xAD}:
                if operand_size != 32:
                    raise X86DecodeError(
                        f"unsupported 0F {second:02X} operand size at {_hex32(address)}"
                    )
                reg_index, rm_operand, offset = self._decode_modrm(
                    code,
                    offset,
                    operand_size=operand_size,
                )
                if second in {0xA5, 0xAD}:
                    count_operand = Operand.register("cl", size=8)
                else:
                    count = self._read_u8(code, offset)
                    offset += 1
                    count_operand = Operand.immediate_u32(count)
                return inst(
                    "shld" if second in {0xA4, 0xA5} else "shrd",
                    (
                        rm_operand,
                        Operand.register(
                            REGISTER_BY_INDEX[reg_index],
                            size=operand_size,
                        ),
                        count_operand,
                    ),
                )
            if second == 0xC1:
                reg_index, rm_operand, offset = self._decode_modrm(
                    code,
                    offset,
                    operand_size=operand_size,
                )
                return inst(
                    "xadd",
                    (
                        rm_operand,
                        Operand.register(
                            REGISTER_BY_INDEX[reg_index],
                            size=operand_size,
                        ),
                    ),
                )
            if 0x90 <= second <= 0x9F:
                _, rm_operand, offset = self._decode_modrm(
                    code, offset, operand_size=8
                )
                return inst(
                    "setcc",
                    (rm_operand,),
                    condition=_condition_for_short_opcode(0x70 + (second - 0x90)),
                )
            if second == 0xAE:
                reg_index, rm_operand, offset = self._decode_modrm(
                    code,
                    offset,
                    operand_size=32,
                )
                if reg_index == 7 and rm_operand.kind == "reg":
                    return inst("sfence")
                if rm_operand.kind != "mem":
                    raise X86DecodeError(
                        f"unsupported 0F AE register form at {_hex32(address)}"
                    )
                if reg_index == 2:
                    return inst("ldmxcsr", (rm_operand,))
                if reg_index == 3:
                    return inst("stmxcsr", (rm_operand,))
                raise X86DecodeError(
                    f"unsupported 0F AE /{reg_index} at {_hex32(address)}"
                )
            raise X86DecodeError(f"unsupported two-byte opcode 0F {second:02X} at {_hex32(address)}")

        if opcode in {0x00, 0x02, 0x08, 0x0A, 0x10, 0x12, 0x18, 0x1A, 0x20, 0x22, 0x28, 0x2A, 0x30, 0x32}:
            reg_index, rm_operand, offset = self._decode_modrm(
                code,
                offset,
                operand_size=8,
            )
            reg_operand = Operand.register(
                BYTE_REGISTER_BY_INDEX[reg_index],
                size=8,
            )
            if opcode in {0x00, 0x08, 0x10, 0x18, 0x20, 0x28, 0x30}:
                operands = (rm_operand, reg_operand)
            else:
                operands = (reg_operand, rm_operand)
            mnemonic = {
                0x00: "add",
                0x02: "add",
                0x08: "or",
                0x0A: "or",
                0x10: "adc",
                0x12: "adc",
                0x18: "sbb",
                0x1A: "sbb",
                0x20: "and",
                0x22: "and",
                0x28: "sub",
                0x2A: "sub",
                0x30: "xor",
                0x32: "xor",
            }[opcode]
            return inst(mnemonic, operands)

        if opcode in {0x01, 0x03, 0x09, 0x0B, 0x11, 0x13, 0x19, 0x1B, 0x21, 0x23, 0x29, 0x2B, 0x31, 0x33, 0x39, 0x3B, 0x85, 0x89, 0x8B, 0x8D}:
            reg_index, rm_operand, offset = self._decode_modrm(
                code,
                offset,
                operand_size=operand_size,
            )
            reg_operand = Operand.register(
                REGISTER_BY_INDEX[reg_index],
                size=operand_size,
            )
            if opcode in {0x01, 0x09, 0x11, 0x19, 0x21, 0x29, 0x31, 0x39, 0x85, 0x89}:
                operands = (rm_operand, reg_operand)
            else:
                operands = (reg_operand, rm_operand)
            mnemonic = {
                0x01: "add",
                0x03: "add",
                0x09: "or",
                0x0B: "or",
                0x11: "adc",
                0x13: "adc",
                0x19: "sbb",
                0x1B: "sbb",
                0x21: "and",
                0x23: "and",
                0x29: "sub",
                0x2B: "sub",
                0x31: "xor",
                0x33: "xor",
                0x39: "cmp",
                0x3B: "cmp",
                0x85: "test",
                0x89: "mov",
                0x8B: "mov",
                0x8D: "lea",
            }[opcode]
            return inst(mnemonic, operands)

        if opcode in {0x05, 0x0D, 0x15, 0x1D, 0x25, 0x2D, 0x35, 0x3D, 0xA9}:
            if operand_size == 16:
                value = _read_u16(code, offset)
                offset += 2
            else:
                value = _read_u32(code, offset)
                offset += 4
            mnemonic = {
                0x05: "add",
                0x0D: "or",
                0x15: "adc",
                0x1D: "sbb",
                0x25: "and",
                0x2D: "sub",
                0x35: "xor",
                0x3D: "cmp",
                0xA9: "test",
            }[opcode]
            return inst(
                mnemonic,
                (
                    Operand.register("eax", size=operand_size),
                    Operand.immediate_u32(value),
                ),
            )

        if opcode in {0x81, 0x83}:
            reg_index, rm_operand, offset = self._decode_modrm(
                code,
                offset,
                operand_size=operand_size,
            )
            if opcode == 0x81:
                if operand_size == 16:
                    value = _read_u16(code, offset)
                    offset += 2
                else:
                    value = _read_u32(code, offset)
                    offset += 4
            else:
                value = _u32(_i8(self._read_u8(code, offset)))
                offset += 1
            mnemonic = {
                0: "add",
                1: "or",
                2: "adc",
                3: "sbb",
                4: "and",
                5: "sub",
                6: "xor",
                7: "cmp",
            }.get(reg_index)
            if mnemonic is None:
                raise X86DecodeError(
                    f"unsupported group1 /{reg_index} at {_hex32(address)}"
                )
            return inst(mnemonic, (rm_operand, Operand.immediate_u32(value)))

        if opcode == 0x80:
            reg_index, rm_operand, offset = self._decode_modrm(
                code, offset, operand_size=8
            )
            value = self._read_u8(code, offset)
            offset += 1
            mnemonic = {
                0: "add",
                1: "or",
                2: "adc",
                3: "sbb",
                4: "and",
                5: "sub",
                6: "xor",
                7: "cmp",
            }.get(reg_index)
            if mnemonic is None:
                raise X86DecodeError(
                    f"unsupported byte immediate group 80 /{reg_index} at {_hex32(address)}"
                )
            return inst(mnemonic, (rm_operand, Operand.immediate_u32(value)))

        if opcode in {0xC0, 0xC1}:
            reg_index, rm_operand, offset = self._decode_modrm(
                code,
                offset,
                operand_size=8 if opcode == 0xC0 else operand_size,
            )
            count = self._read_u8(code, offset)
            offset += 1
            mnemonic = {
                1: "ror",
                3: "rcr",
                4: "shl",
                5: "shr",
                7: "sar",
            }.get(reg_index)
            if mnemonic is None:
                raise X86DecodeError(
                    f"unsupported shift {opcode:02X} /{reg_index} at {_hex32(address)}"
                )
            return inst(mnemonic, (rm_operand, Operand.immediate_u32(count)))

        if opcode in {0xD0, 0xD1}:
            reg_index, rm_operand, offset = self._decode_modrm(
                code,
                offset,
                operand_size=8 if opcode == 0xD0 else operand_size,
            )
            mnemonic = {
                1: "ror",
                3: "rcr",
                4: "shl",
                5: "shr",
                7: "sar",
            }.get(reg_index)
            if mnemonic is None:
                raise X86DecodeError(
                    f"unsupported single-bit shift {opcode:02X} /{reg_index} at {_hex32(address)}"
                )
            return inst(mnemonic, (rm_operand, Operand.immediate_u32(1)))

        if opcode in {0xD2, 0xD3}:
            reg_index, rm_operand, offset = self._decode_modrm(
                code,
                offset,
                operand_size=8 if opcode == 0xD2 else operand_size,
            )
            mnemonic = {
                1: "ror",
                3: "rcr",
                4: "shl",
                5: "shr",
                7: "sar",
            }.get(reg_index)
            if mnemonic is None:
                raise X86DecodeError(
                    f"unsupported variable shift {opcode:02X} /{reg_index} at {_hex32(address)}"
                )
            return inst(mnemonic, (rm_operand, Operand.register("cl", size=8)))

        if opcode == 0xC7:
            reg_index, rm_operand, offset = self._decode_modrm(
                code,
                offset,
                operand_size=operand_size,
            )
            if reg_index != 0:
                raise X86DecodeError(f"unsupported C7 /{reg_index} at {_hex32(address)}")
            if operand_size == 16:
                value = _read_u16(code, offset)
                offset += 2
            else:
                value = _read_u32(code, offset)
                offset += 4
            return inst("mov", (rm_operand, Operand.immediate_u32(value)))

        if opcode == 0xC6:
            reg_index, rm_operand, offset = self._decode_modrm(
                code, offset, operand_size=8
            )
            if reg_index != 0:
                raise X86DecodeError(f"unsupported C6 /{reg_index} at {_hex32(address)}")
            value = self._read_u8(code, offset)
            offset += 1
            return inst("mov", (rm_operand, Operand.immediate_u32(value)))

        if opcode == 0xF7:
            reg_index, rm_operand, offset = self._decode_modrm(
                code,
                offset,
                operand_size=operand_size,
            )
            if reg_index == 0:
                if operand_size == 16:
                    value = _read_u16(code, offset)
                    offset += 2
                else:
                    value = _read_u32(code, offset)
                    offset += 4
                return inst("test", (rm_operand, Operand.immediate_u32(value)))
            if reg_index == 2:
                return inst("not", (rm_operand,))
            if reg_index == 3:
                return inst("neg", (rm_operand,))
            if reg_index == 4:
                return inst("mul", (rm_operand,))
            if reg_index == 5:
                return inst("imul", (rm_operand,))
            if reg_index == 6:
                return inst("div", (rm_operand,))
            if reg_index == 7:
                return inst("idiv", (rm_operand,))
            raise X86DecodeError(f"unsupported F7 /{reg_index} at {_hex32(address)}")

        if opcode == 0xF6:
            reg_index, rm_operand, offset = self._decode_modrm(
                code,
                offset,
                operand_size=8,
            )
            if reg_index == 0:
                value = self._read_u8(code, offset)
                offset += 1
                return inst("test", (rm_operand, Operand.immediate_u32(value)))
            if reg_index == 2:
                return inst("not", (rm_operand,))
            if reg_index == 3:
                return inst("neg", (rm_operand,))
            if reg_index == 5:
                return inst("imul", (rm_operand,))
            raise X86DecodeError(f"unsupported F6 /{reg_index} at {_hex32(address)}")

        if opcode == 0xFE:
            reg_index, rm_operand, offset = self._decode_modrm(
                code,
                offset,
                operand_size=8,
            )
            if reg_index == 0:
                return inst("inc", (rm_operand,))
            if reg_index == 1:
                return inst("dec", (rm_operand,))
            raise X86DecodeError(f"unsupported FE /{reg_index} at {_hex32(address)}")

        if opcode == 0xFF:
            reg_index, rm_operand, offset = self._decode_modrm(
                code,
                offset,
                operand_size=operand_size,
            )
            if reg_index == 0:
                return inst("inc", (rm_operand,))
            if reg_index == 1:
                return inst("dec", (rm_operand,))
            if reg_index == 2:
                return inst("call", (rm_operand,))
            if reg_index == 4:
                return inst("jmp", (rm_operand,))
            if reg_index == 6:
                return inst("push", (rm_operand,))
            raise X86DecodeError(f"unsupported FF /{reg_index} at {_hex32(address)}")

        raise X86DecodeError(f"unsupported opcode {opcode:02X} at {_hex32(address)}")

    def _decode_modrm(
        self, code: bytes, offset: int, *, operand_size: int = 32
    ) -> tuple[int, Operand, int]:
        modrm = self._read_u8(code, offset)
        offset += 1
        mod = (modrm >> 6) & 0b11
        reg = (modrm >> 3) & 0b111
        rm = modrm & 0b111

        if mod == 0b11:
            if operand_size == 8:
                register = BYTE_REGISTER_BY_INDEX[rm]
            elif operand_size == 128:
                register = XMM_REGISTER_BY_INDEX[rm]
            else:
                register = REGISTER_BY_INDEX[rm]
            return reg, Operand.register(register, size=operand_size), offset

        base: str | None = None
        index: str | None = None
        scale = 1
        displacement = 0
        absolute: int | None = None

        if rm == 4:
            sib = self._read_u8(code, offset)
            offset += 1
            scale = 1 << ((sib >> 6) & 0b11)
            index_bits = (sib >> 3) & 0b111
            base_bits = sib & 0b111
            if index_bits != 4:
                index = REGISTER_BY_INDEX[index_bits]
            if base_bits == 5 and mod == 0:
                displacement = _i32(_read_u32(code, offset))
                offset += 4
            else:
                base = REGISTER_BY_INDEX[base_bits]
        elif rm == 5 and mod == 0:
            absolute = _read_u32(code, offset)
            offset += 4
        else:
            base = REGISTER_BY_INDEX[rm]

        if mod == 1:
            displacement = _i8(self._read_u8(code, offset))
            offset += 1
        elif mod == 2:
            displacement = _i32(_read_u32(code, offset))
            offset += 4

        return reg, Operand.memory(
            base=base,
            index=index,
            scale=scale,
            displacement=displacement,
            absolute=absolute,
            size=operand_size,
            segment=getattr(self, "_segment_override", None),
        ), offset

    @staticmethod
    def _read_u8(code: bytes, offset: int) -> int:
        if offset >= len(code):
            raise X86DecodeError("truncated instruction")
        return code[offset]


def lift_x86_function(
    code: bytes,
    *,
    base_address: int,
    symbol: str = "lifted_function",
    max_instructions: int = 256,
) -> LiftedFunction:
    return X86Decoder().decode_function(
        code,
        base_address=base_address,
        symbol=symbol,
        max_instructions=max_instructions,
    )


def lift_x86_block(
    code: bytes,
    *,
    base_address: int,
    symbol: str = "lifted_block",
    max_instructions: int = 128,
) -> LiftedFunction:
    return X86Decoder().decode_block(
        code,
        base_address=base_address,
        symbol=symbol,
        max_instructions=max_instructions,
    )


def lift_xbe_range(
    xbe_path: Path,
    *,
    virtual_address: int,
    size: int,
    symbol: str = "lifted_function",
    max_instructions: int = 256,
) -> LiftedFunction:
    loaded = load_xbe_file(xbe_path, resolve_imports=False)
    code = loaded.arena.read(virtual_address, size)
    return lift_x86_function(
        code,
        base_address=virtual_address,
        symbol=symbol,
        max_instructions=max_instructions,
    )


def execute_lifted_function(
    function: LiftedFunction,
    *,
    state: CpuState | None = None,
    memory: SparseMemory | None = None,
    call_handlers: dict[int, CallHandler] | None = None,
    unhandled_call_handler: CallHandler | None = None,
    block_loader: BlockLoader | None = None,
    step_observer: StepObserver | None = None,
    max_steps: int = 1024,
    record_instruction_trace: bool = True,
    record_trace: bool = True,
    trace_max_events: int | None = None,
    return_on_missing_instruction: bool = False,
    call_handler_yield_predicate: Callable[[int], bool] | None = None,
    execution_yield_predicate: Callable[[int], bool] | None = None,
) -> ExecutionResult:
    active_state = state or CpuState()
    active_memory = memory or SparseMemory()
    active_state.eip = function.base_address
    handlers = call_handlers or {}
    trace = ExecutionTrace(enabled=record_trace, max_events=trace_max_events)
    instructions = {instruction.address: instruction for instruction in function.instructions}
    valid_addresses = set(instructions)
    steps = 0

    def install_block(target: int) -> bool:
        if block_loader is None:
            return False
        loaded_block = block_loader(target)
        if loaded_block is None:
            return False
        for loaded_instruction in loaded_block.instructions:
            instructions.setdefault(loaded_instruction.address, loaded_instruction)
            valid_addresses.add(loaded_instruction.address)
        return target in valid_addresses

    while max_steps == 0 or steps < max_steps:
        if step_observer is not None:
            step_observer(active_state, active_memory, trace, steps)
        if (
            execution_yield_predicate is not None
            and execution_yield_predicate(steps)
        ):
            return ExecutionResult(
                active_state,
                active_memory,
                trace,
                return_address=active_state.eip,
                steps=steps,
            )
        instruction = instructions.get(active_state.eip)
        if instruction is None and install_block(active_state.eip):
            instruction = instructions.get(active_state.eip)
        if instruction is None:
            trace.add(
                active_state.eip,
                "missing_instruction",
                eip=active_state.eip,
                eip_hex=_hex32(active_state.eip),
            )
            if return_on_missing_instruction:
                return ExecutionResult(
                    active_state,
                    active_memory,
                    trace,
                    return_address=active_state.eip,
                    steps=steps,
                )
            raise X86ExecutionError(
                f"no lifted instruction at {_hex32(active_state.eip)}",
                state=active_state,
                trace=trace,
                steps=steps,
            )
        if record_instruction_trace:
            trace.add(instruction.address, "instruction", text=instruction.text())
        steps += 1
        next_eip = instruction.next_address

        if instruction.mnemonic == "nop":
            active_state.eip = next_eip
            continue
        if instruction.mnemonic in {"cld", "std"}:
            active_state.flags.df = instruction.mnemonic == "std"
            trace.add(
                instruction.address,
                "direction_flag",
                set=active_state.flags.df,
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic in {"cli", "sti"}:
            active_state.flags.interrupt_enabled = instruction.mnemonic == "sti"
            trace.add(
                instruction.address,
                "interrupt_flag",
                enabled=active_state.flags.interrupt_enabled,
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "sahf":
            value = _read_low_u8_register(active_state, "ah")
            active_state.flags.sf = bool(value & 0x80)
            active_state.flags.zf = bool(value & 0x40)
            active_state.flags.af = bool(value & 0x10)
            active_state.flags.pf = bool(value & 0x04)
            active_state.flags.cf = bool(value & 0x01)
            trace.add(
                instruction.address,
                "store_ah_into_flags",
                value=value,
                value_hex=_hex32(value),
                **active_state.flags.to_dict(),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic in {
            "prefetch",
            "prefetchnta",
            "prefetcht0",
            "prefetcht1",
            "prefetcht2",
        }:
            operand = instruction.operands[0] if instruction.operands else None
            address = (
                _effective_address(active_state, operand)
                if operand is not None and operand.kind == "mem"
                else None
            )
            trace.add(
                instruction.address,
                "cache_prefetch",
                hint=instruction.mnemonic,
                memory_address=address,
                memory_address_hex=_hex32(address) if address is not None else None,
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "emms":
            trace.add(instruction.address, "mmx_empty_state")
            active_state.eip = next_eip
            continue
        if instruction.mnemonic in {"movq", "movntq"}:
            value = _read_mmx_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                instruction.operands[1],
            )
            _write_mmx_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                instruction.operands[0],
                value,
                non_temporal=instruction.mnemonic == "movntq",
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "fwait":
            trace.add(instruction.address, "fpu_wait")
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "fnclex":
            trace.add(instruction.address, "fpu_clear_exceptions")
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "fnstsw":
            value = active_state.fpu_status_word & 0xFFFF
            _write_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                instruction.operands[0],
                value,
            )
            trace.add(instruction.address, "fpu_store_status_word", value=value)
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "wbinvd":
            trace.add(instruction.address, "cache_writeback_invalidate")
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "sfence":
            trace.add(instruction.address, "store_fence")
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "out":
            trace.add(
                instruction.address,
                "port_write",
                port=active_state.get_register("edx") & 0xFFFF,
                value=active_state.get_register("eax") & 0xFF,
                size=1,
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "in":
            port = active_state.get_register("edx") & 0xFFFF
            value = 0
            _write_u8_register(active_state, "al", value)
            trace.add(
                instruction.address,
                "port_read",
                port=port,
                value=value,
                size=1,
                modeled_default=True,
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "sgdt":
            address = _effective_address(active_state, instruction.operands[0])
            active_memory.write(
                address,
                struct.pack(
                    "<HI",
                    active_state.gdtr_limit & 0xFFFF,
                    active_state.gdtr_base,
                ),
            )
            trace.add(
                instruction.address,
                "store_gdtr",
                memory_address=address,
                memory_address_hex=_hex32(address),
                limit=active_state.gdtr_limit & 0xFFFF,
                base=active_state.gdtr_base,
                base_hex=_hex32(active_state.gdtr_base),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "fnstcw":
            _write_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                instruction.operands[0],
                active_state.fpu_control_word,
            )
            trace.add(
                instruction.address,
                "fpu_store_control_word",
                value=active_state.fpu_control_word,
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "fldcw":
            value = _read_operand(
                active_state, active_memory, trace, instruction, instruction.operands[0]
            )
            active_state.fpu_control_word = value & 0xFFFF
            trace.add(
                instruction.address,
                "fpu_load_control_word",
                value=active_state.fpu_control_word,
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "fnsave":
            address = _effective_address(active_state, instruction.operands[0])
            payload = _x87_save_image(active_state)
            active_memory.write(address, payload)
            trace.add(
                instruction.address,
                "fpu_save_state",
                memory_address=address,
                memory_address_hex=_hex32(address),
                size=len(payload),
                stack_depth=len(active_state.fpu_stack),
            )
            active_state.fpu_control_word = DEFAULT_X87_CONTROL_WORD
            active_state.fpu_status_word = 0
            active_state.fpu_stack.clear()
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "frstor":
            address = _effective_address(active_state, instruction.operands[0])
            payload = active_memory.read(address, X87_SAVE_IMAGE_SIZE)
            _x87_restore_image(active_state, payload)
            trace.add(
                instruction.address,
                "fpu_restore_state",
                memory_address=address,
                memory_address_hex=_hex32(address),
                size=len(payload),
                stack_depth=len(active_state.fpu_stack),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "fild":
            value = _read_x87_integer_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                instruction.operands[0],
            )
            active_state.fpu_stack.insert(0, float(value))
            trace.add(
                instruction.address,
                "fpu_integer_load",
                value=value,
                stack_depth=len(active_state.fpu_stack),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "fistp":
            if not active_state.fpu_stack:
                raise X86ExecutionError(f"x87 stack underflow at {_hex32(instruction.address)}")
            operand = instruction.operands[0]
            value = active_state.fpu_stack.pop(0)
            integer = _x87_float_to_int(value, operand.size)
            _write_x87_integer_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                operand,
                integer,
            )
            trace.add(
                instruction.address,
                "fpu_integer_store_pop",
                value=value,
                integer=integer,
                integer_hex=_hex64(integer) if operand.size == 64 else _hex32(integer),
                stack_depth=len(active_state.fpu_stack),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "fld":
            value = _read_x87_float_operand(
                active_state, active_memory, trace, instruction, instruction.operands[0]
            )
            active_state.fpu_stack.insert(0, value)
            trace.add(
                instruction.address,
                "fpu_load",
                value=value,
                stack_depth=len(active_state.fpu_stack),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic in {
            "fld1",
            "fldl2t",
            "fldl2e",
            "fldpi",
            "fldlg2",
            "fldln2",
            "fldz",
        }:
            constants = {
                "fld1": 1.0,
                "fldl2t": math.log2(10.0),
                "fldl2e": math.log2(math.e),
                "fldpi": math.pi,
                "fldlg2": math.log10(2.0),
                "fldln2": math.log(2.0),
                "fldz": 0.0,
            }
            value = constants[instruction.mnemonic]
            active_state.fpu_stack.insert(0, value)
            trace.add(
                instruction.address,
                "fpu_load_constant",
                constant=instruction.mnemonic[3:],
                value=value,
                stack_depth=len(active_state.fpu_stack),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "frndint":
            if not active_state.fpu_stack:
                raise X86ExecutionError(f"x87 stack underflow at {_hex32(instruction.address)}")
            active_state.fpu_stack[0] = float(round(active_state.fpu_stack[0]))
            trace.add(
                instruction.address,
                "fpu_round_integer",
                result=active_state.fpu_stack[0],
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "fscale":
            if len(active_state.fpu_stack) < 2:
                raise X86ExecutionError(f"x87 stack underflow at {_hex32(instruction.address)}")
            scale = math.trunc(active_state.fpu_stack[1])
            active_state.fpu_stack[0] = math.ldexp(active_state.fpu_stack[0], scale)
            trace.add(
                instruction.address,
                "fpu_scale",
                scale=scale,
                result=active_state.fpu_stack[0],
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "f2xm1":
            if not active_state.fpu_stack:
                raise X86ExecutionError(f"x87 stack underflow at {_hex32(instruction.address)}")
            active_state.fpu_stack[0] = math.pow(2.0, active_state.fpu_stack[0]) - 1.0
            trace.add(
                instruction.address,
                "fpu_two_to_x_minus_one",
                result=active_state.fpu_stack[0],
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic in {"fsqrt", "fsin", "fcos"}:
            if not active_state.fpu_stack:
                raise X86ExecutionError(f"x87 stack underflow at {_hex32(instruction.address)}")
            operations = {
                "fsqrt": math.sqrt,
                "fsin": math.sin,
                "fcos": math.cos,
            }
            active_state.fpu_stack[0] = operations[instruction.mnemonic](
                active_state.fpu_stack[0]
            )
            trace.add(
                instruction.address,
                "fpu_unary_math",
                function=instruction.mnemonic,
                result=active_state.fpu_stack[0],
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "fyl2x":
            if len(active_state.fpu_stack) < 2:
                raise X86ExecutionError(f"x87 stack underflow at {_hex32(instruction.address)}")
            x = active_state.fpu_stack.pop(0)
            y = active_state.fpu_stack[0]
            result = y * math.log2(x) if x > 0.0 else float("nan")
            active_state.fpu_stack[0] = result
            trace.add(
                instruction.address,
                "fpu_y_log2_x",
                x=x,
                y=y,
                result=result,
                stack_depth=len(active_state.fpu_stack),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "fptan":
            if not active_state.fpu_stack:
                raise X86ExecutionError(f"x87 stack underflow at {_hex32(instruction.address)}")
            argument = active_state.fpu_stack[0]
            result = math.tan(argument)
            active_state.fpu_stack[0] = result
            active_state.fpu_stack.insert(0, 1.0)
            trace.add(
                instruction.address,
                "fpu_partial_tangent",
                argument=argument,
                result=result,
                stack_depth=len(active_state.fpu_stack),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "fpatan":
            if len(active_state.fpu_stack) < 2:
                raise X86ExecutionError(
                    f"x87 stack underflow at {_hex32(instruction.address)}"
                )
            x = active_state.fpu_stack.pop(0)
            y = active_state.fpu_stack[0]
            result = math.atan2(y, x)
            active_state.fpu_stack[0] = result
            trace.add(
                instruction.address,
                "fpu_partial_arctangent",
                x=x,
                y=y,
                result=result,
                stack_depth=len(active_state.fpu_stack),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic in {"fcom", "fcomp"}:
            if not active_state.fpu_stack:
                raise X86ExecutionError(f"x87 stack underflow at {_hex32(instruction.address)}")
            value = _read_x87_float_operand(
                active_state, active_memory, trace, instruction, instruction.operands[0]
            )
            top = active_state.fpu_stack[0]
            comparison = _x87_compare(top, value)
            active_state.fpu_status_word = _x87_compare_status_word(
                active_state.fpu_status_word,
                comparison,
            )
            if instruction.mnemonic == "fcomp":
                active_state.fpu_stack.pop(0)
            trace.add(
                instruction.address,
                "fpu_compare_pop" if instruction.mnemonic == "fcomp" else "fpu_compare",
                value=value,
                top=top,
                comparison=comparison,
                status_word=active_state.fpu_status_word,
                status_word_hex=f"0x{active_state.fpu_status_word:04X}",
                stack_depth=len(active_state.fpu_stack),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "fcompp":
            if len(active_state.fpu_stack) < 2:
                raise X86ExecutionError(
                    f"x87 stack underflow at {_hex32(instruction.address)}"
                )
            top = active_state.fpu_stack[0]
            value = active_state.fpu_stack[1]
            comparison = _x87_compare(top, value)
            active_state.fpu_status_word = _x87_compare_status_word(
                active_state.fpu_status_word,
                comparison,
            )
            del active_state.fpu_stack[:2]
            trace.add(
                instruction.address,
                "fpu_compare_pop_twice",
                value=value,
                top=top,
                comparison=comparison,
                status_word=active_state.fpu_status_word,
                status_word_hex=f"0x{active_state.fpu_status_word:04X}",
                stack_depth=len(active_state.fpu_stack),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "fadd":
            if not active_state.fpu_stack:
                raise X86ExecutionError(f"x87 stack underflow at {_hex32(instruction.address)}")
            value = _read_x87_float_operand(
                active_state, active_memory, trace, instruction, instruction.operands[0]
            )
            active_state.fpu_stack[0] += value
            trace.add(
                instruction.address,
                "fpu_add",
                value=value,
                result=active_state.fpu_stack[0],
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "fmul":
            if not active_state.fpu_stack:
                raise X86ExecutionError(f"x87 stack underflow at {_hex32(instruction.address)}")
            value = _read_x87_float_operand(
                active_state, active_memory, trace, instruction, instruction.operands[0]
            )
            active_state.fpu_stack[0] *= value
            trace.add(
                instruction.address,
                "fpu_multiply",
                value=value,
                result=active_state.fpu_stack[0],
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "fsub":
            if not active_state.fpu_stack:
                raise X86ExecutionError(f"x87 stack underflow at {_hex32(instruction.address)}")
            value = _read_x87_float_operand(
                active_state, active_memory, trace, instruction, instruction.operands[0]
            )
            active_state.fpu_stack[0] -= value
            trace.add(
                instruction.address,
                "fpu_subtract",
                value=value,
                result=active_state.fpu_stack[0],
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic in {"fiadd", "fimul"}:
            if not active_state.fpu_stack:
                raise X86ExecutionError(
                    f"x87 stack underflow at {_hex32(instruction.address)}"
                )
            value = _read_x87_integer_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                instruction.operands[0],
            )
            if instruction.mnemonic == "fiadd":
                active_state.fpu_stack[0] += float(value)
            else:
                active_state.fpu_stack[0] *= float(value)
            trace.add(
                instruction.address,
                "fpu_integer_add"
                if instruction.mnemonic == "fiadd"
                else "fpu_integer_multiply",
                value=value,
                result=active_state.fpu_stack[0],
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "fisub":
            if not active_state.fpu_stack:
                raise X86ExecutionError(f"x87 stack underflow at {_hex32(instruction.address)}")
            value = _read_x87_integer_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                instruction.operands[0],
            )
            active_state.fpu_stack[0] -= float(value)
            trace.add(
                instruction.address,
                "fpu_integer_subtract",
                value=value,
                result=active_state.fpu_stack[0],
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "fidiv":
            if not active_state.fpu_stack:
                raise X86ExecutionError(
                    f"x87 stack underflow at {_hex32(instruction.address)}"
                )
            value = _read_x87_integer_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                instruction.operands[0],
            )
            active_state.fpu_stack[0] = _x87_divide(
                active_state.fpu_stack[0],
                float(value),
            )
            trace.add(
                instruction.address,
                "fpu_integer_divide",
                value=value,
                result=active_state.fpu_stack[0],
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "fsubr":
            if not active_state.fpu_stack:
                raise X86ExecutionError(f"x87 stack underflow at {_hex32(instruction.address)}")
            value = _read_x87_float_operand(
                active_state, active_memory, trace, instruction, instruction.operands[0]
            )
            active_state.fpu_stack[0] = value - active_state.fpu_stack[0]
            trace.add(
                instruction.address,
                "fpu_reverse_subtract",
                value=value,
                result=active_state.fpu_stack[0],
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "fdiv":
            if not active_state.fpu_stack:
                raise X86ExecutionError(f"x87 stack underflow at {_hex32(instruction.address)}")
            value = _read_x87_float_operand(
                active_state, active_memory, trace, instruction, instruction.operands[0]
            )
            active_state.fpu_stack[0] = _x87_divide(active_state.fpu_stack[0], value)
            trace.add(
                instruction.address,
                "fpu_divide",
                value=value,
                result=active_state.fpu_stack[0],
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "fdivr":
            if not active_state.fpu_stack:
                raise X86ExecutionError(f"x87 stack underflow at {_hex32(instruction.address)}")
            value = _read_x87_float_operand(
                active_state, active_memory, trace, instruction, instruction.operands[0]
            )
            active_state.fpu_stack[0] = _x87_divide(value, active_state.fpu_stack[0])
            trace.add(
                instruction.address,
                "fpu_reverse_divide",
                value=value,
                result=active_state.fpu_stack[0],
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic in {
            "fadd_st",
            "fmul_st",
            "fsubr_st",
            "fsub_st",
            "fdivr_st",
            "fdiv_st",
        }:
            index = _x87_stack_operand_index(
                active_state, instruction, instruction.operands[0]
            )
            top = active_state.fpu_stack[0]
            target = active_state.fpu_stack[index]
            if instruction.mnemonic == "fadd_st":
                result = target + top
            elif instruction.mnemonic == "fmul_st":
                result = target * top
            elif instruction.mnemonic == "fsubr_st":
                result = top - target
            elif instruction.mnemonic == "fsub_st":
                result = target - top
            elif instruction.mnemonic == "fdivr_st":
                result = _x87_divide(top, target)
            else:
                result = _x87_divide(target, top)
            active_state.fpu_stack[index] = result
            trace.add(
                instruction.address,
                "fpu_stack_arithmetic",
                arithmetic_kind=instruction.mnemonic,
                stack_index=index,
                top=top,
                target=target,
                result=result,
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "fchs":
            if not active_state.fpu_stack:
                raise X86ExecutionError(f"x87 stack underflow at {_hex32(instruction.address)}")
            active_state.fpu_stack[0] = -active_state.fpu_stack[0]
            trace.add(
                instruction.address,
                "fpu_change_sign",
                result=active_state.fpu_stack[0],
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "fabs":
            if not active_state.fpu_stack:
                raise X86ExecutionError(f"x87 stack underflow at {_hex32(instruction.address)}")
            active_state.fpu_stack[0] = abs(active_state.fpu_stack[0])
            trace.add(
                instruction.address,
                "fpu_absolute_value",
                result=active_state.fpu_stack[0],
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "ftst":
            if not active_state.fpu_stack:
                raise X86ExecutionError(f"x87 stack underflow at {_hex32(instruction.address)}")
            top = active_state.fpu_stack[0]
            comparison = _x87_compare(top, 0.0)
            active_state.fpu_status_word = _x87_compare_status_word(
                active_state.fpu_status_word,
                comparison,
            )
            trace.add(
                instruction.address,
                "fpu_test",
                top=top,
                comparison=comparison,
                status_word=active_state.fpu_status_word,
                status_word_hex=f"0x{active_state.fpu_status_word:04X}",
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "fxch":
            index = _x87_stack_operand_index(
                active_state, instruction, instruction.operands[0]
            )
            active_state.fpu_stack[0], active_state.fpu_stack[index] = (
                active_state.fpu_stack[index],
                active_state.fpu_stack[0],
            )
            trace.add(
                instruction.address,
                "fpu_exchange",
                stack_index=index,
                top=active_state.fpu_stack[0],
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "ffree":
            index = _x87_stack_operand_index(
                active_state,
                instruction,
                instruction.operands[0],
            )
            value = active_state.fpu_stack.pop(index)
            trace.add(
                instruction.address,
                "fpu_free",
                stack_index=index,
                value=value,
                stack_depth=len(active_state.fpu_stack),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "faddp":
            index = _x87_stack_operand_index(
                active_state, instruction, instruction.operands[0]
            )
            value = active_state.fpu_stack[0]
            active_state.fpu_stack[index] += value
            result = active_state.fpu_stack[index]
            active_state.fpu_stack.pop(0)
            trace.add(
                instruction.address,
                "fpu_add_pop",
                stack_index=index,
                value=value,
                result=result,
                stack_depth=len(active_state.fpu_stack),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "fmulp":
            index = _x87_stack_operand_index(
                active_state, instruction, instruction.operands[0]
            )
            value = active_state.fpu_stack[0]
            active_state.fpu_stack[index] *= value
            result = active_state.fpu_stack[index]
            active_state.fpu_stack.pop(0)
            trace.add(
                instruction.address,
                "fpu_multiply_pop",
                stack_index=index,
                value=value,
                result=result,
                stack_depth=len(active_state.fpu_stack),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "fsubp":
            index = _x87_stack_operand_index(
                active_state, instruction, instruction.operands[0]
            )
            value = active_state.fpu_stack[0]
            active_state.fpu_stack[index] -= value
            result = active_state.fpu_stack[index]
            active_state.fpu_stack.pop(0)
            trace.add(
                instruction.address,
                "fpu_subtract_pop",
                stack_index=index,
                value=value,
                result=result,
                stack_depth=len(active_state.fpu_stack),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "fsubrp":
            index = _x87_stack_operand_index(
                active_state, instruction, instruction.operands[0]
            )
            top = active_state.fpu_stack[0]
            target = active_state.fpu_stack[index]
            active_state.fpu_stack[index] = top - target
            result = active_state.fpu_stack[index]
            active_state.fpu_stack.pop(0)
            trace.add(
                instruction.address,
                "fpu_reverse_subtract_pop",
                stack_index=index,
                top=top,
                target=target,
                result=result,
                stack_depth=len(active_state.fpu_stack),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "fdivrp":
            index = _x87_stack_operand_index(
                active_state,
                instruction,
                instruction.operands[0],
            )
            top = active_state.fpu_stack[0]
            target = active_state.fpu_stack[index]
            active_state.fpu_stack[index] = _x87_divide(top, target)
            result = active_state.fpu_stack[index]
            active_state.fpu_stack.pop(0)
            trace.add(
                instruction.address,
                "fpu_reverse_divide_pop",
                stack_index=index,
                top=top,
                target=target,
                result=result,
                stack_depth=len(active_state.fpu_stack),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "fdivp":
            index = _x87_stack_operand_index(
                active_state, instruction, instruction.operands[0]
            )
            value = active_state.fpu_stack[0]
            active_state.fpu_stack[index] = _x87_divide(
                active_state.fpu_stack[index],
                value,
            )
            result = active_state.fpu_stack[index]
            active_state.fpu_stack.pop(0)
            trace.add(
                instruction.address,
                "fpu_divide_pop",
                stack_index=index,
                value=value,
                result=result,
                stack_depth=len(active_state.fpu_stack),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "fst":
            if not active_state.fpu_stack:
                raise X86ExecutionError(f"x87 stack underflow at {_hex32(instruction.address)}")
            value = active_state.fpu_stack[0]
            operand = instruction.operands[0]
            if operand.kind == "imm":
                index = _x87_stack_operand_index(active_state, instruction, operand)
                active_state.fpu_stack[index] = value
                trace.add(
                    instruction.address,
                    "fpu_store_stack",
                    stack_index=index,
                    value=value,
                    stack_depth=len(active_state.fpu_stack),
                )
            else:
                _write_float32_operand(
                    active_state,
                    active_memory,
                    trace,
                    instruction,
                    operand,
                    value,
                )
                trace.add(
                    instruction.address,
                    "fpu_store",
                    value=value,
                    stack_depth=len(active_state.fpu_stack),
                )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "fstp":
            if not active_state.fpu_stack:
                raise X86ExecutionError(f"x87 stack underflow at {_hex32(instruction.address)}")
            operand = instruction.operands[0]
            value = active_state.fpu_stack[0]
            if operand.kind == "imm":
                index = _x87_stack_operand_index(active_state, instruction, operand)
                active_state.fpu_stack[index] = value
                active_state.fpu_stack.pop(0)
            else:
                active_state.fpu_stack.pop(0)
                _write_float32_operand(
                    active_state,
                    active_memory,
                    trace,
                    instruction,
                    operand,
                    value,
                )
            trace.add(
                instruction.address,
                "fpu_store_pop",
                value=value,
                stack_depth=len(active_state.fpu_stack),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "stmxcsr":
            _write_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                instruction.operands[0],
                active_state.mxcsr,
            )
            trace.add(instruction.address, "sse_store_mxcsr", value=active_state.mxcsr)
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "ldmxcsr":
            value = _read_operand(
                active_state, active_memory, trace, instruction, instruction.operands[0]
            )
            active_state.mxcsr = _u32(value)
            trace.add(instruction.address, "sse_load_mxcsr", value=active_state.mxcsr)
            active_state.eip = next_eip
            continue
        if instruction.mnemonic in {"cvttss2si", "cvtss2si"}:
            value = _read_xmm_scalar_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                instruction.operands[1],
            )
            integer = (
                _sse_truncate_float32_to_i32(value)
                if instruction.mnemonic == "cvttss2si"
                else _sse_round_float32_to_i32(value, active_state.mxcsr)
            )
            _write_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                instruction.operands[0],
                integer,
            )
            trace.add(
                instruction.address,
                "sse_truncate_scalar_float_to_i32"
                if instruction.mnemonic == "cvttss2si"
                else "sse_round_scalar_float_to_i32",
                value=value,
                integer=integer,
                integer_hex=_hex32(integer),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic in {"movaps", "movups"}:
            value = _read_xmm_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                instruction.operands[1],
            )
            _write_xmm_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                instruction.operands[0],
                value,
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "movss":
            destination = instruction.operands[0]
            source = instruction.operands[1]
            value = _read_xmm_scalar_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                source,
            )
            if destination.kind == "reg":
                current = active_state.get_xmm_register(destination.reg or "")
                vector = (
                    (value, 0.0, 0.0, 0.0)
                    if source.kind == "mem"
                    else (value, current[1], current[2], current[3])
                )
                _write_xmm_operand(
                    active_state,
                    active_memory,
                    trace,
                    instruction,
                    destination,
                    vector,
                )
            else:
                _write_float32_operand(
                    active_state,
                    active_memory,
                    trace,
                    instruction,
                    destination,
                    value,
                )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "rsqrtss":
            destination = instruction.operands[0]
            value = _read_xmm_scalar_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                instruction.operands[1],
            )
            current = active_state.get_xmm_register(destination.reg or "")
            result = 1.0 / math.sqrt(value) if value > 0.0 else float("nan")
            _write_xmm_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                destination,
                (result, current[1], current[2], current[3]),
            )
            trace.add(
                instruction.address,
                "xmm_reciprocal_sqrt_scalar",
                value=value,
                result=result,
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "sqrtps":
            source = _read_xmm_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                instruction.operands[1],
            )
            result = tuple(math.sqrt(value) if value >= 0.0 else float("nan") for value in source)
            _write_xmm_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                instruction.operands[0],
                result,
            )
            trace.add(instruction.address, "xmm_square_root_packed_float", result=list(result))
            active_state.eip = next_eip
            continue
        if instruction.mnemonic in {"addps", "mulps", "minps", "maxps"}:
            destination = _read_xmm_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                instruction.operands[0],
            )
            source = _read_xmm_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                instruction.operands[1],
            )
            if instruction.mnemonic == "addps":
                result = tuple(left + right for left, right in zip(destination, source))
                operation = "xmm_add_packed_float"
            elif instruction.mnemonic == "mulps":
                result = tuple(left * right for left, right in zip(destination, source))
                operation = "xmm_multiply_packed_float"
            else:
                minimum = instruction.mnemonic == "minps"
                result = tuple(
                    _sse_minmax_float32(left, right, minimum=minimum)
                    for left, right in zip(destination, source)
                )
                operation = (
                    "xmm_minimum_packed_float"
                    if minimum
                    else "xmm_maximum_packed_float"
                )
            _write_xmm_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                instruction.operands[0],
                result,
            )
            trace.add(instruction.address, operation, result=list(result))
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "shufps":
            destination = _read_xmm_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                instruction.operands[0],
            )
            source = _read_xmm_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                instruction.operands[1],
            )
            immediate = instruction.operands[2].immediate or 0
            result = _xmm_shuffle(destination, source, immediate)
            _write_xmm_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                instruction.operands[0],
                result,
            )
            trace.add(
                instruction.address,
                "xmm_shuffle_packed_float",
                immediate=immediate,
                immediate_hex=_hex32(immediate),
                result=list(result),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic in {"unpcklps", "unpckhps", "movlhps", "movhlps"}:
            destination = _read_xmm_operand(
                active_state, active_memory, trace, instruction, instruction.operands[0]
            )
            source = _read_xmm_operand(
                active_state, active_memory, trace, instruction, instruction.operands[1]
            )
            if instruction.mnemonic == "unpcklps":
                result = (destination[0], source[0], destination[1], source[1])
            elif instruction.mnemonic == "unpckhps":
                result = (destination[2], source[2], destination[3], source[3])
            elif instruction.mnemonic == "movlhps":
                result = (destination[0], destination[1], source[0], source[1])
            else:
                result = (source[2], source[3], destination[2], destination[3])
            _write_xmm_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                instruction.operands[0],
                result,
            )
            trace.add(
                instruction.address,
                f"xmm_{instruction.mnemonic}",
                result=list(result),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic in {"movlps_store", "movhps_store"}:
            source = _read_xmm_operand(
                active_state, active_memory, trace, instruction, instruction.operands[1]
            )
            lanes = source[:2] if instruction.mnemonic == "movlps_store" else source[2:]
            address = _effective_address(active_state, instruction.operands[0])
            active_memory.write(address, struct.pack("<2f", *lanes))
            trace.add(
                instruction.address,
                "xmm_store_low_packed_float" if instruction.mnemonic == "movlps_store"
                else "xmm_store_high_packed_float",
                memory_address=address,
                memory_address_hex=_hex32(address),
                value=list(lanes),
                size=8,
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "mov":
            value = _read_operand(active_state, active_memory, trace, instruction, instruction.operands[1])
            _write_operand(active_state, active_memory, trace, instruction, instruction.operands[0], value)
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "xchg":
            left = _read_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                instruction.operands[0],
            )
            right = _read_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                instruction.operands[1],
            )
            _write_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                instruction.operands[0],
                right,
            )
            _write_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                instruction.operands[1],
                left,
            )
            trace.add(
                instruction.address,
                "exchange",
                left=left,
                left_hex=_hex32(left),
                right=right,
                right_hex=_hex32(right),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "xadd":
            destination = instruction.operands[0]
            source = instruction.operands[1]
            left = _read_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                destination,
            )
            right = _read_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                source,
            )
            result = _binary_result("add", left, right)
            _write_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                destination,
                result,
            )
            _write_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                source,
                left,
            )
            _update_flags(
                active_state.flags,
                "add",
                left,
                right,
                result,
                size=destination.size,
            )
            trace.add(
                instruction.address,
                "exchange_add",
                left=left,
                right=right,
                result=result,
                **active_state.flags.to_dict(),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "movzx":
            value = _read_operand(active_state, active_memory, trace, instruction, instruction.operands[1])
            _write_operand(active_state, active_memory, trace, instruction, instruction.operands[0], value)
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "movsx":
            source = instruction.operands[1]
            value = _read_operand(active_state, active_memory, trace, instruction, source)
            _write_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                instruction.operands[0],
                _sign_extend(value, source.size),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "setcc":
            if instruction.condition is None:
                raise X86ExecutionError("setcc missing condition")
            value = 1 if _evaluate_condition(active_state.flags, instruction.condition) else 0
            _write_operand(active_state, active_memory, trace, instruction, instruction.operands[0], value)
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "lea":
            address = _effective_address(active_state, instruction.operands[1])
            _write_operand(active_state, active_memory, trace, instruction, instruction.operands[0], address)
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "bsf":
            value = _read_operand(active_state, active_memory, trace, instruction, instruction.operands[1])
            if value == 0:
                active_state.flags.zf = True
            else:
                mask = (1 << instruction.operands[1].size) - 1
                masked = value & mask
                index = (masked & -masked).bit_length() - 1
                _write_operand(
                    active_state,
                    active_memory,
                    trace,
                    instruction,
                    instruction.operands[0],
                    index,
                )
                active_state.flags.zf = False
            trace.add(instruction.address, "flags", **active_state.flags.to_dict())
            active_state.eip = next_eip
            continue
        if instruction.mnemonic in {"add", "adc", "sub", "sbb", "xor", "and", "or"}:
            lhs = _read_operand(active_state, active_memory, trace, instruction, instruction.operands[0])
            rhs = _read_operand(active_state, active_memory, trace, instruction, instruction.operands[1])
            operation = {
                "adc": "add",
                "sbb": "sub",
            }.get(instruction.mnemonic, instruction.mnemonic)
            if instruction.mnemonic in {"adc", "sbb"}:
                rhs += 1 if active_state.flags.cf else 0
            result = _binary_result(operation, lhs, rhs)
            _write_operand(active_state, active_memory, trace, instruction, instruction.operands[0], result)
            _update_flags(
                active_state.flags,
                operation,
                lhs,
                rhs,
                result,
                size=instruction.operands[0].size,
            )
            trace.add(instruction.address, "flags", **active_state.flags.to_dict())
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "imul":
            if len(instruction.operands) == 1:
                operand = instruction.operands[0]
                if operand.size == 32:
                    lhs = _signed_int(active_state.get_register("eax"), 32)
                    rhs = _signed_int(
                        _read_operand(
                            active_state, active_memory, trace, instruction, operand
                        ),
                        32,
                    )
                    product = lhs * rhs
                    result = product & 0xFFFFFFFFFFFFFFFF
                    low = result & 0xFFFFFFFF
                    high = (result >> 32) & 0xFFFFFFFF
                    active_state.set_register("eax", low)
                    active_state.set_register("edx", high)
                    active_state.flags.cf = active_state.flags.of = (
                        product != _signed_int(low, 32)
                    )
                    trace.add(
                        instruction.address,
                        "signed_multiply",
                        lhs=lhs,
                        rhs=rhs,
                        product=product,
                        low=low,
                        low_hex=_hex32(low),
                        high=high,
                        high_hex=_hex32(high),
                    )
                    trace.add(
                        instruction.address, "flags", **active_state.flags.to_dict()
                    )
                    active_state.eip = next_eip
                    continue
                if operand.size != 8:
                    raise X86ExecutionError("unsupported one-operand imul size")
                lhs = _signed_int(active_state.get_register("eax") & 0xFF, 8)
                rhs = _signed_int(
                    _read_operand(
                        active_state, active_memory, trace, instruction, operand
                    ),
                    8,
                )
                product = lhs * rhs
                result = product & 0xFFFF
                active_state.set_register(
                    "eax",
                    (active_state.get_register("eax") & 0xFFFF0000) | result,
                )
                active_state.flags.cf = active_state.flags.of = (
                    product != _signed_int(result & 0xFF, 8)
                )
                trace.add(
                    instruction.address,
                    "signed_multiply",
                    lhs=lhs,
                    rhs=rhs,
                    product=product,
                    result=result,
                    result_hex=_hex32(result),
                )
                trace.add(
                    instruction.address, "flags", **active_state.flags.to_dict()
                )
                active_state.eip = next_eip
                continue
            destination = instruction.operands[0]
            size = destination.size
            lhs_operand = (
                instruction.operands[1]
                if len(instruction.operands) == 3
                else destination
            )
            rhs_operand = (
                instruction.operands[2]
                if len(instruction.operands) == 3
                else instruction.operands[1]
            )
            lhs = _signed_int(
                _read_operand(active_state, active_memory, trace, instruction, lhs_operand),
                lhs_operand.size,
            )
            rhs = _signed_int(
                _read_operand(active_state, active_memory, trace, instruction, rhs_operand),
                rhs_operand.size,
            )
            product = lhs * rhs
            mask = (1 << size) - 1
            result = product & mask
            _write_operand(active_state, active_memory, trace, instruction, destination, result)
            active_state.flags.cf = active_state.flags.of = (
                product != _signed_int(result, size)
            )
            trace.add(
                instruction.address,
                "signed_multiply",
                lhs=lhs,
                rhs=rhs,
                product=product,
                result=result,
                result_hex=_hex32(result),
            )
            trace.add(instruction.address, "flags", **active_state.flags.to_dict())
            active_state.eip = next_eip
            continue
        if instruction.mnemonic in {"cmp", "test"}:
            lhs = _read_operand(active_state, active_memory, trace, instruction, instruction.operands[0])
            rhs = _read_operand(active_state, active_memory, trace, instruction, instruction.operands[1])
            result = _binary_result("sub" if instruction.mnemonic == "cmp" else "and", lhs, rhs)
            _update_flags(
                active_state.flags,
                "sub" if instruction.mnemonic == "cmp" else "and",
                lhs,
                rhs,
                result,
                size=instruction.operands[0].size,
            )
            trace.add(instruction.address, "flags", **active_state.flags.to_dict())
            active_state.eip = next_eip
            continue
        if instruction.mnemonic in {"inc", "dec"}:
            lhs = _read_operand(active_state, active_memory, trace, instruction, instruction.operands[0])
            old_cf = active_state.flags.cf
            rhs = 1
            operation = "add" if instruction.mnemonic == "inc" else "sub"
            result = _binary_result(operation, lhs, rhs)
            _write_operand(active_state, active_memory, trace, instruction, instruction.operands[0], result)
            _update_flags(
                active_state.flags,
                operation,
                lhs,
                rhs,
                result,
                size=instruction.operands[0].size,
            )
            active_state.flags.cf = old_cf
            trace.add(instruction.address, "flags", **active_state.flags.to_dict())
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "neg":
            lhs = _read_operand(active_state, active_memory, trace, instruction, instruction.operands[0])
            result = _binary_result("sub", 0, lhs)
            _write_operand(active_state, active_memory, trace, instruction, instruction.operands[0], result)
            _update_flags(
                active_state.flags,
                "sub",
                0,
                lhs,
                result,
                size=instruction.operands[0].size,
            )
            trace.add(instruction.address, "flags", **active_state.flags.to_dict())
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "not":
            value = _read_operand(active_state, active_memory, trace, instruction, instruction.operands[0])
            _write_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                instruction.operands[0],
                ~value,
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic in {"shl", "shr", "sar"}:
            lhs = _read_operand(active_state, active_memory, trace, instruction, instruction.operands[0])
            count = _read_operand(active_state, active_memory, trace, instruction, instruction.operands[1]) & 0x1F
            result = _shift_result(
                instruction.mnemonic,
                lhs,
                count,
                size=instruction.operands[0].size,
            )
            _write_operand(active_state, active_memory, trace, instruction, instruction.operands[0], result)
            _update_shift_flags(
                active_state.flags,
                instruction.mnemonic,
                lhs,
                count,
                result,
                size=instruction.operands[0].size,
            )
            trace.add(instruction.address, "flags", **active_state.flags.to_dict())
            active_state.eip = next_eip
            continue
        if instruction.mnemonic in {"ror", "rcr"}:
            operand = instruction.operands[0]
            value = _read_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                operand,
            )
            raw_count = _read_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                instruction.operands[1],
            )
            result, carry, overflow, effective_count = _rotate_right_result(
                instruction.mnemonic,
                value,
                raw_count,
                size=operand.size,
                carry=active_state.flags.cf,
            )
            _write_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                operand,
                result,
            )
            if effective_count:
                active_state.flags.cf = carry
                if effective_count == 1:
                    active_state.flags.of = overflow
            trace.add(
                instruction.address,
                "rotate_right_through_carry"
                if instruction.mnemonic == "rcr"
                else "rotate_right",
                count=effective_count,
                result=result,
                result_hex=_hex32(result),
                **active_state.flags.to_dict(),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic in {"shld", "shrd"}:
            destination = _read_operand(
                active_state, active_memory, trace, instruction, instruction.operands[0]
            )
            source = _read_operand(
                active_state, active_memory, trace, instruction, instruction.operands[1]
            )
            count = _read_operand(
                active_state, active_memory, trace, instruction, instruction.operands[2]
            ) & 0x1F
            result = _double_shift_result(
                instruction.mnemonic,
                destination,
                source,
                count,
            )
            _write_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                instruction.operands[0],
                result,
            )
            _update_double_shift_flags(
                active_state.flags,
                instruction.mnemonic,
                destination,
                count,
                result,
            )
            trace.add(instruction.address, "flags", **active_state.flags.to_dict())
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "push":
            value = _read_operand(active_state, active_memory, trace, instruction, instruction.operands[0])
            _push(active_state, active_memory, trace, instruction, value)
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "pop":
            value = _pop(active_state, active_memory, trace, instruction)
            _write_operand(active_state, active_memory, trace, instruction, instruction.operands[0], value)
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "leave":
            active_state.set_register("esp", active_state.get_register("ebp"))
            value = _pop(active_state, active_memory, trace, instruction)
            active_state.set_register("ebp", value)
            trace.add(
                instruction.address,
                "stack_frame",
                esp=active_state.get_register("esp"),
                ebp=active_state.get_register("ebp"),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "cdq":
            eax = active_state.get_register("eax")
            edx = 0xFFFFFFFF if eax & 0x80000000 else 0
            active_state.set_register("edx", edx)
            trace.add(
                instruction.address,
                "register_write",
                register="edx",
                value=edx,
                value_hex=_hex32(edx),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "rdtsc":
            value = active_state.timestamp_counter & 0xFFFFFFFFFFFFFFFF
            active_state.set_register("eax", value & 0xFFFFFFFF)
            active_state.set_register("edx", value >> 32)
            active_state.timestamp_counter = (
                value + DETERMINISTIC_TSC_STEP
            ) & 0xFFFFFFFFFFFFFFFF
            trace.add(
                instruction.address,
                "timestamp_counter",
                value=value,
                value_hex=f"0x{value:016X}",
                next_value=active_state.timestamp_counter,
                next_value_hex=f"0x{active_state.timestamp_counter:016X}",
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "idiv":
            divisor = _i32(
                _read_operand(
                    active_state,
                    active_memory,
                    trace,
                    instruction,
                    instruction.operands[0],
                )
            )
            if divisor == 0:
                raise X86ExecutionError(f"signed division by zero at {_hex32(instruction.address)}")
            high = _i32(active_state.get_register("edx"))
            low = active_state.get_register("eax")
            dividend = (high << 32) | low
            quotient_abs = abs(dividend) // abs(divisor)
            quotient = -quotient_abs if (dividend < 0) ^ (divisor < 0) else quotient_abs
            if quotient < -0x80000000 or quotient > 0x7FFFFFFF:
                raise X86ExecutionError(f"signed division overflow at {_hex32(instruction.address)}")
            remainder = dividend - (quotient * divisor)
            active_state.set_register("eax", quotient)
            active_state.set_register("edx", remainder)
            trace.add(
                instruction.address,
                "signed_divide",
                dividend=dividend,
                divisor=divisor,
                quotient=quotient,
                quotient_hex=_hex32(quotient),
                remainder=remainder,
                remainder_hex=_hex32(remainder),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "div":
            divisor = _read_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                instruction.operands[0],
            )
            if divisor == 0:
                raise X86ExecutionError(f"unsigned division by zero at {_hex32(instruction.address)}")
            dividend = (
                (active_state.get_register("edx") << 32)
                | active_state.get_register("eax")
            )
            quotient = dividend // divisor
            remainder = dividend % divisor
            if quotient > 0xFFFFFFFF:
                raise X86ExecutionError(f"unsigned division overflow at {_hex32(instruction.address)}")
            active_state.set_register("eax", quotient)
            active_state.set_register("edx", remainder)
            trace.add(
                instruction.address,
                "unsigned_divide",
                dividend=dividend,
                divisor=divisor,
                quotient=quotient,
                quotient_hex=_hex32(quotient),
                remainder=remainder,
                remainder_hex=_hex32(remainder),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "mul":
            multiplier = _read_operand(
                active_state,
                active_memory,
                trace,
                instruction,
                instruction.operands[0],
            )
            multiplicand = active_state.get_register("eax")
            product = multiplicand * multiplier
            low = product & 0xFFFFFFFF
            high = (product >> 32) & 0xFFFFFFFF
            active_state.set_register("eax", low)
            active_state.set_register("edx", high)
            active_state.flags.cf = active_state.flags.of = high != 0
            trace.add(
                instruction.address,
                "unsigned_multiply",
                multiplicand=multiplicand,
                multiplier=multiplier,
                product=product,
                product_hex=_hex64(product),
                low=low,
                low_hex=_hex32(low),
                high=high,
                high_hex=_hex32(high),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic in {"stosb", "rep_stosb"}:
            count = (
                active_state.get_register("ecx")
                if instruction.mnemonic == "rep_stosb"
                else 1
            )
            destination = active_state.get_register("edi")
            value = _read_low_u8_register(active_state, "al")
            step = -1 if active_state.flags.df else 1
            for index in range(count):
                memory.write(_u32(destination + index * step), bytes([value]))
            active_state.set_register("edi", destination + count * step)
            if instruction.mnemonic == "rep_stosb":
                active_state.set_register("ecx", 0)
            trace.add(
                instruction.address,
                "string_store",
                element_size=1,
                count=count,
                destination=destination,
                destination_hex=_hex32(destination),
                value=value,
                value_hex=_hex32(value),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic in {"stosd", "rep_stosd"}:
            count = (
                active_state.get_register("ecx")
                if instruction.mnemonic == "rep_stosd"
                else 1
            )
            destination = active_state.get_register("edi")
            value = active_state.get_register("eax")
            step = -4 if active_state.flags.df else 4
            for index in range(count):
                memory.write_u32(_u32(destination + index * step), value)
            active_state.set_register("edi", destination + count * step)
            if instruction.mnemonic == "rep_stosd":
                active_state.set_register("ecx", 0)
            trace.add(
                instruction.address,
                "string_store",
                element_size=4,
                count=count,
                destination=destination,
                destination_hex=_hex32(destination),
                value=value,
                value_hex=_hex32(value),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic in {"movsb", "rep_movsb"}:
            count = (
                active_state.get_register("ecx")
                if instruction.mnemonic == "rep_movsb"
                else 1
            )
            source = active_state.get_register("esi")
            destination = active_state.get_register("edi")
            step = -1 if active_state.flags.df else 1
            for index in range(count):
                value = active_memory.read(_u32(source + index * step), 1)[0]
                active_memory.write(
                    _u32(destination + index * step),
                    bytes([value]),
                )
            active_state.set_register("esi", source + count * step)
            active_state.set_register("edi", destination + count * step)
            if instruction.mnemonic == "rep_movsb":
                active_state.set_register("ecx", 0)
            trace.add(
                instruction.address,
                "string_move",
                element_size=1,
                count=count,
                source=source,
                source_hex=_hex32(source),
                destination=destination,
                destination_hex=_hex32(destination),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic in {"movsd", "rep_movsd"}:
            count = (
                active_state.get_register("ecx")
                if instruction.mnemonic == "rep_movsd"
                else 1
            )
            source = active_state.get_register("esi")
            destination = active_state.get_register("edi")
            step = -4 if active_state.flags.df else 4
            for index in range(count):
                value = active_memory.read_u32(_u32(source + index * step))
                active_memory.write_u32(
                    _u32(destination + index * step),
                    value,
                )
            active_state.set_register("esi", source + count * step)
            active_state.set_register("edi", destination + count * step)
            if instruction.mnemonic == "rep_movsd":
                active_state.set_register("ecx", 0)
            trace.add(
                instruction.address,
                "string_move",
                element_size=4,
                count=count,
                source=source,
                source_hex=_hex32(source),
                destination=destination,
                destination_hex=_hex32(destination),
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic in {"cmpsb", "rep_cmpsb", "cmpsd", "rep_cmpsd"}:
            repeated = instruction.mnemonic in {"rep_cmpsb", "rep_cmpsd"}
            element_size = 4 if instruction.mnemonic in {"cmpsd", "rep_cmpsd"} else 1
            operand_size = element_size * 8
            requested_count = (
                active_state.get_register("ecx")
                if repeated
                else 1
            )
            source = active_state.get_register("esi")
            destination = active_state.get_register("edi")
            remaining = requested_count
            compared = 0
            last_lhs = 0
            last_rhs = 0
            step = -element_size if active_state.flags.df else element_size
            while remaining:
                source_address = _u32(source + compared * step)
                destination_address = _u32(destination + compared * step)
                if element_size == 4:
                    last_lhs = active_memory.read_u32(source_address)
                    last_rhs = active_memory.read_u32(destination_address)
                    result = _u32(last_lhs - last_rhs)
                else:
                    last_lhs = active_memory.read(source_address, 1)[0]
                    last_rhs = active_memory.read(destination_address, 1)[0]
                    result = _u32(last_lhs - last_rhs) & 0xFF
                _update_flags(
                    active_state.flags,
                    "sub",
                    last_lhs,
                    last_rhs,
                    result,
                    size=operand_size,
                )
                compared += 1
                remaining -= 1
                if not repeated or not active_state.flags.zf:
                    break
            active_state.set_register("esi", source + compared * step)
            active_state.set_register("edi", destination + compared * step)
            if repeated:
                active_state.set_register("ecx", remaining)
            trace.add(
                instruction.address,
                "string_compare",
                element_size=element_size,
                requested_count=requested_count,
                compared_count=compared,
                source=source,
                source_hex=_hex32(source),
                destination=destination,
                destination_hex=_hex32(destination),
                last_lhs=last_lhs,
                last_lhs_hex=_hex32(last_lhs),
                last_rhs=last_rhs,
                last_rhs_hex=_hex32(last_rhs),
                equal=active_state.flags.zf,
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic in {"scasb", "repne_scasb"}:
            requested_count = (
                active_state.get_register("ecx")
                if instruction.mnemonic == "repne_scasb"
                else 1
            )
            source = active_state.get_register("edi")
            needle = active_state.get_register("eax") & 0xFF
            remaining = requested_count
            scanned = 0
            last_value = 0
            step = -1 if active_state.flags.df else 1
            while remaining:
                last_value = active_memory.read(
                    _u32(source + scanned * step),
                    1,
                )[0]
                result = _u32(needle - last_value) & 0xFF
                _update_flags(
                    active_state.flags,
                    "sub",
                    needle,
                    last_value,
                    result,
                    size=8,
                )
                scanned += 1
                remaining -= 1
                if instruction.mnemonic != "repne_scasb" or active_state.flags.zf:
                    break
            active_state.set_register("edi", source + scanned * step)
            if instruction.mnemonic == "repne_scasb":
                active_state.set_register("ecx", remaining)
            trace.add(
                instruction.address,
                "string_scan",
                element_size=1,
                requested_count=requested_count,
                scanned_count=scanned,
                source=source,
                source_hex=_hex32(source),
                needle=needle,
                needle_hex=_hex32(needle),
                last_value=last_value,
                last_value_hex=_hex32(last_value),
                remaining=remaining,
            )
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "int3":
            raise X86ExecutionError(f"debug trap at {_hex32(instruction.address)}")
        if instruction.mnemonic == "int":
            vector = instruction.operands[0].immediate if instruction.operands else 0
            raise X86ExecutionError(
                f"software interrupt {_hex32(vector or 0)} at {_hex32(instruction.address)}"
            )
        if instruction.mnemonic == "call":
            target = instruction.target
            if target is None:
                target = _read_operand(
                    active_state, active_memory, trace, instruction, instruction.operands[0]
                )
            _push(active_state, active_memory, trace, instruction, next_eip)
            trace.add(instruction.address, "call", target=target, target_hex=_hex32(target))
            handler = handlers.get(target)
            if handler is not None:
                handler(active_state, active_memory, target, trace)
                returned_to = _pop(active_state, active_memory, trace, instruction)
                if returned_to != next_eip:
                    raise X86ExecutionError(
                        f"external call returned to {_hex32(returned_to)}, "
                        f"expected {_hex32(next_eip)}"
                    )
                active_state.eip = returned_to
                if (
                    call_handler_yield_predicate is not None
                    and call_handler_yield_predicate(target)
                ):
                    return ExecutionResult(
                        active_state,
                        active_memory,
                        trace,
                        return_address=returned_to,
                        steps=steps,
                    )
            elif target in valid_addresses:
                active_state.eip = target
            elif install_block(target):
                active_state.eip = target
            elif unhandled_call_handler is not None:
                unhandled_call_handler(active_state, active_memory, target, trace)
                returned_to = _pop(active_state, active_memory, trace, instruction)
                if returned_to != next_eip:
                    raise X86ExecutionError(
                        f"external call returned to {_hex32(returned_to)}, "
                        f"expected {_hex32(next_eip)}"
                )
                active_state.eip = returned_to
            elif return_on_missing_instruction:
                active_state.eip = target
                return ExecutionResult(
                    active_state,
                    active_memory,
                    trace,
                    return_address=target,
                    steps=steps,
                )
            else:
                raise X86ExecutionError(f"unhandled external call target {_hex32(target)}")
            continue
        if instruction.mnemonic == "jmp":
            target = instruction.target
            if target is None:
                target = _read_operand(
                    active_state, active_memory, trace, instruction, instruction.operands[0]
                )
            trace.add(instruction.address, "jump", target=target, target_hex=_hex32(target))
            handler = handlers.get(target)
            if handler is not None:
                handler(active_state, active_memory, target, trace)
                returned_to = _pop(active_state, active_memory, trace, instruction)
                active_state.eip = returned_to
                if returned_to in valid_addresses:
                    continue
                return ExecutionResult(
                    active_state,
                    active_memory,
                    trace,
                    return_address=returned_to,
                    steps=steps,
                )
            active_state.eip = target
            continue
        if instruction.mnemonic == "jmp_far":
            if instruction.target is None:
                raise X86ExecutionError("far jump missing target")
            selector = instruction.operands[0].immediate or 0
            active_state.cs_selector = selector & 0xFFFF
            trace.add(
                instruction.address,
                "far_jump",
                selector=active_state.cs_selector,
                selector_hex=f"0x{active_state.cs_selector:04X}",
                target=instruction.target,
                target_hex=_hex32(instruction.target),
            )
            active_state.eip = instruction.target
            continue
        if instruction.mnemonic == "jcc":
            if instruction.target is None or instruction.condition is None:
                raise X86ExecutionError("conditional branch missing target or condition")
            taken = (
                active_state.get_register("ecx") == 0
                if instruction.condition == "ecx_zero"
                else _evaluate_condition(active_state.flags, instruction.condition)
            )
            trace.add(
                instruction.address,
                "branch",
                condition=instruction.condition,
                taken=taken,
                target=instruction.target,
                target_hex=_hex32(instruction.target),
            )
            active_state.eip = instruction.target if taken else next_eip
            continue
        if instruction.mnemonic == "ret":
            return_address = _pop(active_state, active_memory, trace, instruction)
            if instruction.ret_stack_adjust:
                active_state.set_register(
                    "esp",
                    active_state.get_register("esp") + instruction.ret_stack_adjust,
                )
            trace.add(
                instruction.address,
                "return",
                return_address=return_address,
                return_address_hex=_hex32(return_address),
                stack_adjust=instruction.ret_stack_adjust,
                esp=active_state.get_register("esp"),
            )
            active_state.eip = return_address
            if return_address in valid_addresses or install_block(return_address):
                continue
            return ExecutionResult(
                active_state,
                active_memory,
                trace,
                return_address=return_address,
                steps=steps,
            )
        raise X86ExecutionError(f"unsupported lifted mnemonic: {instruction.mnemonic}")

    raise X86ExecutionError(
        "execution step limit reached",
        state=active_state,
        trace=trace,
        steps=steps,
    )


def _read_operand(
    state: CpuState,
    memory: SparseMemory,
    trace: ExecutionTrace,
    instruction: X86Instruction,
    operand: Operand,
) -> int:
    if operand.kind == "reg":
        if operand.size == 8:
            return _read_low_u8_register(state, operand.reg or "")
        if operand.size == 16:
            return state.get_register(operand.reg or "") & 0xFFFF
        return state.get_register(operand.reg or "")
    if operand.kind == "imm":
        return operand.immediate or 0
    if operand.kind == "mem":
        address = _effective_address(state, operand)
        if operand.size == 8:
            value = memory.read(address, 1)[0]
        elif operand.size == 16:
            value = struct.unpack("<H", memory.read(address, 2))[0]
        else:
            value = memory.read_u32(address)
        trace.add(
            instruction.address,
            "memory_read",
            memory_address=address,
            memory_address_hex=_hex32(address),
            value=value,
            value_hex=_hex32(value),
        )
        return value
    raise X86ExecutionError(f"cannot read operand kind {operand.kind}")


def _read_float32_operand(
    state: CpuState,
    memory: SparseMemory,
    trace: ExecutionTrace,
    instruction: X86Instruction,
    operand: Operand,
) -> float:
    if operand.kind != "mem":
        raise X86ExecutionError("x87 float operand must be memory-backed")
    address = _effective_address(state, operand)
    size = 8 if operand.size == 64 else 4
    payload = memory.read(address, size)
    raw = int.from_bytes(payload, "little")
    trace.add(
        instruction.address,
        "memory_read",
        memory_address=address,
        memory_address_hex=_hex32(address),
        value=raw,
        value_hex=_hex64(raw) if size == 8 else _hex32(raw),
    )
    return struct.unpack("<d" if size == 8 else "<f", payload)[0]


def _read_xmm_operand(
    state: CpuState,
    memory: SparseMemory,
    trace: ExecutionTrace,
    instruction: X86Instruction,
    operand: Operand,
) -> tuple[float, float, float, float]:
    if operand.kind == "reg":
        if operand.reg not in XMM_REGISTER_NAMES:
            raise X86ExecutionError(f"unsupported XMM register read: {operand.reg}")
        value = state.get_xmm_register(operand.reg)
        trace.add(
            instruction.address,
            "xmm_register_read",
            register=operand.reg,
            value=list(value),
        )
        return value
    if operand.kind == "mem":
        address = _effective_address(state, operand)
        payload = memory.read(address, 16)
        value = struct.unpack("<4f", payload)
        trace.add(
            instruction.address,
            "xmm_memory_read",
            memory_address=address,
            memory_address_hex=_hex32(address),
            value=list(value),
            size=16,
        )
        return value
    raise X86ExecutionError(f"cannot read XMM operand kind {operand.kind}")


def _write_xmm_operand(
    state: CpuState,
    memory: SparseMemory,
    trace: ExecutionTrace,
    instruction: X86Instruction,
    operand: Operand,
    value: Iterable[float],
) -> None:
    lanes = tuple(float(lane) for lane in value)
    if len(lanes) != 4:
        raise X86ExecutionError("XMM operand writes require four lanes")
    vector = (lanes[0], lanes[1], lanes[2], lanes[3])
    if operand.kind == "reg":
        if operand.reg not in XMM_REGISTER_NAMES:
            raise X86ExecutionError(f"unsupported XMM register write: {operand.reg}")
        state.set_xmm_register(operand.reg, vector)
        trace.add(
            instruction.address,
            "xmm_register_write",
            register=operand.reg,
            value=list(vector),
        )
        return
    if operand.kind == "mem":
        address = _effective_address(state, operand)
        memory.write(address, struct.pack("<4f", *vector))
        trace.add(
            instruction.address,
            "xmm_memory_write",
            memory_address=address,
            memory_address_hex=_hex32(address),
            value=list(vector),
            size=16,
        )
        return
    raise X86ExecutionError(f"cannot write XMM operand kind {operand.kind}")


def _read_mmx_operand(
    state: CpuState,
    memory: SparseMemory,
    trace: ExecutionTrace,
    instruction: X86Instruction,
    operand: Operand,
) -> int:
    if operand.kind == "reg":
        if operand.reg not in MMX_REGISTER_NAMES:
            raise X86ExecutionError(f"unsupported MMX register read: {operand.reg}")
        value = state.get_mmx_register(operand.reg)
        trace.add(
            instruction.address,
            "mmx_register_read",
            register=operand.reg,
            value=value,
            value_hex=_hex64(value),
        )
        return value
    if operand.kind == "mem":
        address = _effective_address(state, operand)
        payload = memory.read(address, 8)
        value = struct.unpack("<Q", payload)[0]
        trace.add(
            instruction.address,
            "memory_read",
            memory_address=address,
            memory_address_hex=_hex32(address),
            value=value,
            value_hex=_hex64(value),
            size=8,
        )
        return value
    raise X86ExecutionError(f"cannot read MMX operand kind {operand.kind}")


def _write_mmx_operand(
    state: CpuState,
    memory: SparseMemory,
    trace: ExecutionTrace,
    instruction: X86Instruction,
    operand: Operand,
    value: int,
    *,
    non_temporal: bool = False,
) -> None:
    value &= 0xFFFFFFFFFFFFFFFF
    if operand.kind == "reg":
        if operand.reg not in MMX_REGISTER_NAMES:
            raise X86ExecutionError(f"unsupported MMX register write: {operand.reg}")
        state.set_mmx_register(operand.reg, value)
        trace.add(
            instruction.address,
            "mmx_register_write",
            register=operand.reg,
            value=value,
            value_hex=_hex64(value),
        )
        return
    if operand.kind == "mem":
        address = _effective_address(state, operand)
        memory.write(address, struct.pack("<Q", value))
        trace.add(
            instruction.address,
            "memory_write",
            memory_address=address,
            memory_address_hex=_hex32(address),
            value=value,
            value_hex=_hex64(value),
            size=8,
            non_temporal=non_temporal,
        )
        return
    raise X86ExecutionError(f"cannot write MMX operand kind {operand.kind}")


def _read_xmm_scalar_operand(
    state: CpuState,
    memory: SparseMemory,
    trace: ExecutionTrace,
    instruction: X86Instruction,
    operand: Operand,
) -> float:
    if operand.kind == "reg":
        if operand.reg not in XMM_REGISTER_NAMES:
            raise X86ExecutionError(f"unsupported XMM scalar read: {operand.reg}")
        value = state.get_xmm_register(operand.reg)[0]
        trace.add(
            instruction.address,
            "xmm_scalar_register_read",
            register=operand.reg,
            value=value,
        )
        return value
    return _read_float32_operand(state, memory, trace, instruction, operand)


def _xmm_shuffle(
    destination: tuple[float, float, float, float],
    source: tuple[float, float, float, float],
    immediate: int,
) -> tuple[float, float, float, float]:
    return (
        destination[immediate & 0x03],
        destination[(immediate >> 2) & 0x03],
        source[(immediate >> 4) & 0x03],
        source[(immediate >> 6) & 0x03],
    )


def _read_x87_integer_operand(
    state: CpuState,
    memory: SparseMemory,
    trace: ExecutionTrace,
    instruction: X86Instruction,
    operand: Operand,
) -> int:
    if operand.kind != "mem":
        raise X86ExecutionError("x87 integer operand must be memory-backed")
    if operand.size == 32:
        return _i32(_read_operand(state, memory, trace, instruction, operand))
    if operand.size != 64:
        raise X86ExecutionError(f"unsupported x87 integer size {operand.size}")
    address = _effective_address(state, operand)
    payload = memory.read(address, 8)
    raw = struct.unpack("<Q", payload)[0]
    value = struct.unpack("<q", payload)[0]
    trace.add(
        instruction.address,
        "memory_read",
        memory_address=address,
        memory_address_hex=_hex32(address),
        value=raw,
        value_hex=_hex64(raw),
        size=8,
    )
    return value


def _write_x87_integer_operand(
    state: CpuState,
    memory: SparseMemory,
    trace: ExecutionTrace,
    instruction: X86Instruction,
    operand: Operand,
    value: int,
) -> None:
    if operand.kind != "mem":
        raise X86ExecutionError("x87 integer operand must be memory-backed")
    if operand.size != 64:
        _write_operand(state, memory, trace, instruction, operand, value)
        return
    address = _effective_address(state, operand)
    payload = struct.pack("<q", int(value))
    raw = struct.unpack("<Q", payload)[0]
    memory.write(address, payload)
    trace.add(
        instruction.address,
        "memory_write",
        memory_address=address,
        memory_address_hex=_hex32(address),
        value=raw,
        value_hex=_hex64(raw),
        size=8,
    )


def _x87_float_to_int(value: float, bits: int) -> int:
    minimum = -(1 << (bits - 1))
    maximum = (1 << (bits - 1)) - 1
    if not math.isfinite(value):
        return minimum
    rounded = int(round(value))
    if rounded < minimum or rounded > maximum:
        return minimum
    return rounded


def _sse_truncate_float32_to_i32(value: float) -> int:
    if not math.isfinite(value) or value < -2147483648.0 or value > 2147483647.0:
        return 0x80000000
    return _u32(int(value))


def _sse_round_float32_to_i32(value: float, mxcsr: int) -> int:
    if not math.isfinite(value):
        return 0x80000000
    rounding_mode = (mxcsr >> 13) & 0x03
    if rounding_mode == 0:
        rounded = round(value)
    elif rounding_mode == 1:
        rounded = math.floor(value)
    elif rounding_mode == 2:
        rounded = math.ceil(value)
    else:
        rounded = math.trunc(value)
    if rounded < -2147483648 or rounded > 2147483647:
        return 0x80000000
    return _u32(int(rounded))


def _sse_minmax_float32(left: float, right: float, *, minimum: bool) -> float:
    if math.isnan(left) or math.isnan(right) or left == right:
        return right
    if minimum:
        return left if left < right else right
    return left if left > right else right


def _x87_stack_operand_index(
    state: CpuState,
    instruction: X86Instruction,
    operand: Operand,
) -> int:
    if operand.kind != "imm":
        raise X86ExecutionError("x87 stack operand must be an encoded ST index")
    index = operand.immediate or 0
    if index >= len(state.fpu_stack):
        raise X86ExecutionError(
            f"x87 stack underflow reading st({index}) at {_hex32(instruction.address)}"
        )
    return index


def _read_x87_float_operand(
    state: CpuState,
    memory: SparseMemory,
    trace: ExecutionTrace,
    instruction: X86Instruction,
    operand: Operand,
) -> float:
    if operand.kind == "imm":
        index = _x87_stack_operand_index(state, instruction, operand)
        value = state.fpu_stack[index]
        trace.add(
            instruction.address,
            "fpu_stack_read",
            stack_index=index,
            value=value,
        )
        return value
    if operand.size == 80:
        address = _effective_address(state, operand)
        payload = memory.read(address, 10)
        value = _x87_decode_extended80(payload)
        trace.add(
            instruction.address,
            "memory_read",
            memory_address=address,
            memory_address_hex=_hex32(address),
            value=payload.hex().upper(),
            size=10,
        )
        return value
    if operand.size != 64:
        return _read_float32_operand(state, memory, trace, instruction, operand)
    address = _effective_address(state, operand)
    payload = memory.read(address, 8)
    raw = struct.unpack("<Q", payload)[0]
    trace.add(
        instruction.address,
        "memory_read",
        memory_address=address,
        memory_address_hex=_hex32(address),
        value=raw,
        value_hex=_hex64(raw),
        size=8,
    )
    return struct.unpack("<d", payload)[0]


def _x87_decode_extended80(payload: bytes) -> float:
    if len(payload) != 10:
        raise X86ExecutionError("x87 extended value must contain ten bytes")
    significand = int.from_bytes(payload[:8], "little")
    exponent_sign = int.from_bytes(payload[8:], "little")
    negative = bool(exponent_sign & 0x8000)
    exponent = exponent_sign & 0x7FFF
    if exponent == 0x7FFF:
        if significand == 0x8000000000000000:
            value = float("inf")
        else:
            value = float("nan")
    elif exponent == 0:
        if significand == 0:
            value = 0.0
        else:
            value = math.ldexp(
                significand / float(1 << 63),
                1 - 16383,
            )
    else:
        value = math.ldexp(
            significand / float(1 << 63),
            exponent - 16383,
        )
    return -value if negative else value


def _x87_encode_extended80(value: float) -> bytes:
    negative = math.copysign(1.0, value) < 0.0
    magnitude = abs(value)
    if math.isnan(value):
        significand = 0xC000000000000000
        exponent = 0x7FFF
    elif math.isinf(value):
        significand = 0x8000000000000000
        exponent = 0x7FFF
    elif magnitude == 0.0:
        significand = 0
        exponent = 0
    else:
        fraction, binary_exponent = math.frexp(magnitude)
        exponent = binary_exponent - 1 + 16383
        significand = int(math.ldexp(fraction, 64)) & 0xFFFFFFFFFFFFFFFF
    exponent_sign = exponent | (0x8000 if negative else 0)
    return significand.to_bytes(8, "little") + exponent_sign.to_bytes(2, "little")


def _x87_tag(value: float) -> int:
    if math.isnan(value) or math.isinf(value):
        return 2
    if value == 0.0:
        return 1
    return 0


def _x87_save_image(state: CpuState) -> bytes:
    payload = bytearray(X87_SAVE_IMAGE_SIZE)
    struct.pack_into("<H", payload, 0, state.fpu_control_word & 0xFFFF)
    struct.pack_into("<H", payload, 4, state.fpu_status_word & 0xFFFF)
    tag_word = 0
    for index in range(8):
        tag = _x87_tag(state.fpu_stack[index]) if index < len(state.fpu_stack) else 3
        tag_word |= tag << (index * 2)
        if index < len(state.fpu_stack):
            start = 28 + index * 10
            payload[start : start + 10] = _x87_encode_extended80(
                state.fpu_stack[index]
            )
    struct.pack_into("<H", payload, 8, tag_word)
    return bytes(payload)


def _x87_restore_image(state: CpuState, payload: bytes) -> None:
    if len(payload) != X87_SAVE_IMAGE_SIZE:
        raise X86ExecutionError("x87 save image must contain 108 bytes")
    state.fpu_control_word = struct.unpack_from("<H", payload, 0)[0]
    state.fpu_status_word = struct.unpack_from("<H", payload, 4)[0]
    tag_word = struct.unpack_from("<H", payload, 8)[0]
    restored: list[float] = []
    for index in range(8):
        if ((tag_word >> (index * 2)) & 0x03) == 3:
            continue
        start = 28 + index * 10
        restored.append(_x87_decode_extended80(payload[start : start + 10]))
    state.fpu_stack = restored


def _x87_compare(left: float, right: float) -> str:
    if math.isnan(left) or math.isnan(right):
        return "unordered"
    if left < right:
        return "less"
    if left > right:
        return "greater"
    return "equal"


def _x87_compare_status_word(status_word: int, comparison: str) -> int:
    status_word &= ~X87_STATUS_CONDITION_MASK
    if comparison == "less":
        status_word |= X87_STATUS_C0
    elif comparison == "equal":
        status_word |= X87_STATUS_C3
    elif comparison == "unordered":
        status_word |= X87_STATUS_C0 | X87_STATUS_C2 | X87_STATUS_C3
    return status_word & 0xFFFF


def _x87_divide(numerator: float, denominator: float) -> float:
    if denominator == 0.0:
        if numerator == 0.0:
            return float("nan")
        negative = math.copysign(1.0, numerator) != math.copysign(1.0, denominator)
        return float("-inf") if negative else float("inf")
    return numerator / denominator


def _read_low_u8_register(state: CpuState, name: str) -> int:
    low_map = {
        "al": "eax",
        "cl": "ecx",
        "dl": "edx",
        "bl": "ebx",
        "eax": "eax",
        "ecx": "ecx",
        "edx": "edx",
        "ebx": "ebx",
    }
    high_map = {
        "ah": "eax",
        "ch": "ecx",
        "dh": "edx",
        "bh": "ebx",
    }
    normalized = name.casefold()
    parent = low_map.get(normalized)
    if parent is not None:
        return state.get_register(parent) & 0xFF
    parent = high_map.get(normalized)
    if parent is None:
        raise X86ExecutionError(f"unsupported 8-bit register read: {name}")
    return (state.get_register(parent) >> 8) & 0xFF


def _write_u8_register(state: CpuState, name: str, value: int) -> None:
    low_map = {
        "al": "eax",
        "cl": "ecx",
        "dl": "edx",
        "bl": "ebx",
        "eax": "eax",
        "ecx": "ecx",
        "edx": "edx",
        "ebx": "ebx",
    }
    high_map = {
        "ah": "eax",
        "ch": "ecx",
        "dh": "edx",
        "bh": "ebx",
    }
    normalized = name.casefold()
    parent = low_map.get(normalized)
    if parent is not None:
        current = state.get_register(parent)
        state.set_register(parent, (current & 0xFFFFFF00) | (value & 0xFF))
        return
    parent = high_map.get(normalized)
    if parent is not None:
        current = state.get_register(parent)
        state.set_register(parent, (current & 0xFFFF00FF) | ((value & 0xFF) << 8))
        return
    raise X86ExecutionError(f"unsupported 8-bit register write: {name}")


def _write_u16_register(state: CpuState, name: str, value: int) -> None:
    normalized = name.casefold()
    if normalized not in REGISTER_NAMES:
        raise X86ExecutionError(f"unsupported 16-bit register write: {name}")
    current = state.get_register(normalized)
    state.set_register(normalized, (current & 0xFFFF0000) | (value & 0xFFFF))


def _write_operand(
    state: CpuState,
    memory: SparseMemory,
    trace: ExecutionTrace,
    instruction: X86Instruction,
    operand: Operand,
    value: int,
) -> None:
    value = _u32(value)
    if operand.kind == "reg":
        if operand.size == 8:
            _write_u8_register(state, operand.reg or "", value)
        elif operand.size == 16:
            _write_u16_register(state, operand.reg or "", value)
        else:
            state.set_register(operand.reg or "", value)
        trace.add(
            instruction.address,
            "register_write",
            register=operand.reg,
            value=value,
            value_hex=_hex32(value),
        )
        return
    if operand.kind == "mem":
        address = _effective_address(state, operand)
        if operand.size == 8:
            memory.write(address, bytes([value & 0xFF]))
        elif operand.size == 16:
            memory.write(address, struct.pack("<H", value & 0xFFFF))
        else:
            memory.write_u32(address, value)
        trace.add(
            instruction.address,
            "memory_write",
            memory_address=address,
            memory_address_hex=_hex32(address),
            value=value,
            value_hex=_hex32(value),
            size=operand.size // 8,
        )
        return
    raise X86ExecutionError(f"cannot write operand kind {operand.kind}")


def _write_float32_operand(
    state: CpuState,
    memory: SparseMemory,
    trace: ExecutionTrace,
    instruction: X86Instruction,
    operand: Operand,
    value: float,
) -> None:
    if operand.kind != "mem":
        raise X86ExecutionError("x87 float operand must be memory-backed")
    address = _effective_address(state, operand)
    size = 8 if operand.size == 64 else 4
    payload = struct.pack("<d" if size == 8 else "<f", float(value))
    raw = int.from_bytes(payload, "little")
    memory.write(address, payload)
    trace.add(
        instruction.address,
        "memory_write",
        memory_address=address,
        memory_address_hex=_hex32(address),
        value=raw,
        value_hex=_hex64(raw) if size == 8 else _hex32(raw),
        size=size,
    )


def _effective_address(state: CpuState, operand: Operand) -> int:
    if operand.kind != "mem":
        raise X86ExecutionError("effective address requested for non-memory operand")
    if operand.absolute is not None:
        base = operand.absolute
    else:
        base = 0
        if operand.base:
            base += state.get_register(operand.base)
        if operand.index:
            base += state.get_register(operand.index) * operand.scale
    if operand.segment == "fs":
        base += state.fs_base
    elif operand.segment is not None:
        raise X86ExecutionError(f"unsupported segment override {operand.segment}")
    return _u32(base + operand.displacement)


def _push(
    state: CpuState,
    memory: SparseMemory,
    trace: ExecutionTrace,
    instruction: X86Instruction,
    value: int,
) -> None:
    esp = _u32(state.get_register("esp") - 4)
    state.set_register("esp", esp)
    memory.write_u32(esp, value)
    trace.add(
        instruction.address,
        "stack_push",
        memory_address=esp,
        memory_address_hex=_hex32(esp),
        value=_u32(value),
        value_hex=_hex32(value),
    )


def _pop(
    state: CpuState,
    memory: SparseMemory,
    trace: ExecutionTrace,
    instruction: X86Instruction,
) -> int:
    esp = state.get_register("esp")
    value = memory.read_u32(esp)
    state.set_register("esp", esp + 4)
    trace.add(
        instruction.address,
        "stack_pop",
        memory_address=esp,
        memory_address_hex=_hex32(esp),
        value=value,
        value_hex=_hex32(value),
    )
    return value


def _binary_result(operation: str, lhs: int, rhs: int) -> int:
    if operation == "add":
        return _u32(lhs + rhs)
    if operation == "sub":
        return _u32(lhs - rhs)
    if operation == "xor":
        return _u32(lhs ^ rhs)
    if operation == "and":
        return _u32(lhs & rhs)
    if operation == "or":
        return _u32(lhs | rhs)
    raise X86ExecutionError(f"unsupported binary operation {operation}")


def _sign_extend(value: int, bits: int) -> int:
    mask = (1 << bits) - 1
    sign_bit = 1 << (bits - 1)
    value &= mask
    return _u32((value ^ sign_bit) - sign_bit)


def _signed_int(value: int, bits: int) -> int:
    mask = (1 << bits) - 1
    sign_bit = 1 << (bits - 1)
    value &= mask
    return (value ^ sign_bit) - sign_bit


def _shift_result(operation: str, value: int, count: int, *, size: int = 32) -> int:
    mask = (1 << size) - 1
    value = _u32(value) & mask
    if count == 0:
        return value
    if operation == "shl":
        return _u32(value << count) & mask
    if operation == "shr":
        return _u32(value >> count) & mask
    if operation == "sar":
        return _u32(_i32(_sign_extend(value, size)) >> count) & mask
    raise X86ExecutionError(f"unsupported shift operation {operation}")


def _rotate_right_result(
    operation: str,
    value: int,
    count: int,
    *,
    size: int,
    carry: bool,
) -> tuple[int, bool, bool, int]:
    mask = (1 << size) - 1
    value &= mask
    masked_count = count & 0x1F
    if operation == "ror":
        effective_count = masked_count % size
        if effective_count == 0:
            return value, carry, False, 0
        result = (
            (value >> effective_count)
            | (value << (size - effective_count))
        ) & mask
        next_carry = bool(result & (1 << (size - 1)))
    elif operation == "rcr":
        width = size + 1
        effective_count = masked_count % width
        if effective_count == 0:
            return value, carry, False, 0
        ring_mask = (1 << width) - 1
        ring = (int(carry) << size) | value
        ring = (
            (ring >> effective_count)
            | (ring << (width - effective_count))
        ) & ring_mask
        result = ring & mask
        next_carry = bool(ring & (1 << size))
    else:
        raise X86ExecutionError(f"unsupported rotate operation {operation}")
    overflow = bool(
        ((result >> (size - 1)) ^ (result >> (size - 2))) & 1
    )
    return result, next_carry, overflow, effective_count


def _double_shift_result(
    operation: str,
    destination: int,
    source: int,
    count: int,
) -> int:
    destination = _u32(destination)
    source = _u32(source)
    count &= 0x1F
    if count == 0:
        return destination
    if operation == "shld":
        return _u32((destination << count) | (source >> (32 - count)))
    if operation == "shrd":
        return _u32((destination >> count) | (source << (32 - count)))
    raise X86ExecutionError(f"unsupported double shift operation {operation}")


def _update_flags(
    flags: CpuFlags,
    operation: str,
    lhs: int,
    rhs: int,
    result: int,
    *,
    size: int = 32,
) -> None:
    mask = (1 << size) - 1
    sign_bit = 1 << (size - 1)
    rhs_raw = rhs
    lhs = _u32(lhs) & mask
    rhs = _u32(rhs) & mask
    result = _u32(result) & mask
    flags.zf = result == 0
    flags.sf = bool(result & sign_bit)
    flags.pf = _parity8(result)
    if operation == "add":
        flags.cf = lhs + rhs_raw > mask
        flags.of = bool((~(lhs ^ rhs) & (lhs ^ result) & sign_bit) != 0)
        flags.af = bool((lhs ^ rhs ^ result) & 0x10)
    elif operation == "sub":
        flags.cf = lhs < rhs_raw
        flags.of = bool(((lhs ^ rhs) & (lhs ^ result) & sign_bit) != 0)
        flags.af = bool((lhs ^ rhs ^ result) & 0x10)
    elif operation in {"xor", "and", "or"}:
        flags.cf = False
        flags.of = False
        flags.af = False
    else:
        raise X86ExecutionError(f"cannot update flags for {operation}")


def _update_shift_flags(
    flags: CpuFlags,
    operation: str,
    lhs: int,
    count: int,
    result: int,
    *,
    size: int = 32,
) -> None:
    mask = (1 << size) - 1
    sign_bit = 1 << (size - 1)
    lhs = _u32(lhs) & mask
    result = _u32(result) & mask
    if count == 0:
        return
    if operation == "shl":
        flags.cf = bool(lhs & (1 << (size - count))) if count <= size else False
        flags.of = bool(((result >> 31) & 1) ^ int(flags.cf)) if count == 1 else False
    elif operation == "shr":
        flags.cf = bool(lhs & (1 << (count - 1)))
        flags.of = bool(lhs & sign_bit) if count == 1 else False
    elif operation == "sar":
        flags.cf = bool(lhs & (1 << (count - 1)))
        flags.of = False
    else:
        raise X86ExecutionError(f"cannot update shift flags for {operation}")
    flags.zf = result == 0
    flags.sf = bool(result & sign_bit)
    flags.pf = _parity8(result)
    flags.af = False


def _update_double_shift_flags(
    flags: CpuFlags,
    operation: str,
    destination: int,
    count: int,
    result: int,
) -> None:
    count &= 0x1F
    if count == 0:
        return
    destination = _u32(destination)
    result = _u32(result)
    if operation == "shld":
        flags.cf = bool(destination & (1 << (32 - count)))
        flags.of = bool(((result >> 31) & 1) ^ int(flags.cf)) if count == 1 else False
    elif operation == "shrd":
        flags.cf = bool(destination & (1 << (count - 1)))
        flags.of = bool(((destination ^ result) >> 31) & 1) if count == 1 else False
    else:
        raise X86ExecutionError(f"cannot update double shift flags for {operation}")
    flags.zf = result == 0
    flags.sf = bool(result & 0x80000000)
    flags.pf = _parity8(result)
    flags.af = False


def _condition_for_short_opcode(opcode: int) -> str:
    return {
        0x70: "o",
        0x71: "no",
        0x72: "b",
        0x73: "ae",
        0x74: "e",
        0x75: "ne",
        0x76: "be",
        0x77: "a",
        0x78: "s",
        0x79: "ns",
        0x7A: "p",
        0x7B: "np",
        0x7C: "l",
        0x7D: "ge",
        0x7E: "le",
        0x7F: "g",
    }[opcode]


def _evaluate_condition(flags: CpuFlags, condition: str) -> bool:
    if condition == "o":
        return flags.of
    if condition == "no":
        return not flags.of
    if condition == "b":
        return flags.cf
    if condition == "ae":
        return not flags.cf
    if condition == "e":
        return flags.zf
    if condition == "ne":
        return not flags.zf
    if condition == "be":
        return flags.cf or flags.zf
    if condition == "a":
        return not flags.cf and not flags.zf
    if condition == "s":
        return flags.sf
    if condition == "ns":
        return not flags.sf
    if condition == "p":
        return flags.pf
    if condition == "np":
        return not flags.pf
    if condition == "l":
        return flags.sf != flags.of
    if condition == "ge":
        return flags.sf == flags.of
    if condition == "le":
        return flags.zf or flags.sf != flags.of
    if condition == "g":
        return not flags.zf and flags.sf == flags.of
    raise X86ExecutionError(f"unsupported condition {condition}")


class CppEmitter:
    """Emit deterministic C++17 for the lifted subset."""

    def emit(
        self,
        function: LiftedFunction,
        *,
        exported_symbol: str | None = None,
        resumable: bool = False,
        observer_addresses: Iterable[int] = (),
        callback_addresses: Iterable[int] = (),
        forwarded_callback_addresses: Iterable[int] = (),
        native_fast_paths: Mapping[int, NativeFastPath] | None = None,
        synchronize_eip_for_callbacks: bool = False,
    ) -> str:
        self._resumable = resumable
        self._observer_addresses = {_u32(address) for address in observer_addresses}
        self._callback_addresses = {_u32(address) for address in callback_addresses}
        self._forwarded_callback_addresses = {
            _u32(address) for address in forwarded_callback_addresses
        }
        if not self._forwarded_callback_addresses <= self._callback_addresses:
            raise ValueError(
                "forwarded callback addresses must also be callback addresses"
            )
        self._native_fast_paths = {
            _u32(address): fast_path
            for address, fast_path in (native_fast_paths or {}).items()
        }
        if self._native_fast_paths and not resumable:
            raise ValueError("native fast paths require resumable C++ emission")
        self._synchronize_eip_for_callbacks = synchronize_eip_for_callbacks
        self._direct_fallthrough_addresses = {
            _u32(current.address)
            for current, following in zip(
                function.instructions,
                function.instructions[1:],
            )
            if _u32(current.next_address) == _u32(following.address)
        }
        symbol = _cpp_identifier(exported_symbol or function.symbol)
        lines = [
            "// Generated by b2_recomp Milestone 5 prototype.",
            "// Target: Windows x86 guest semantics, C++17 host translation unit.",
            "#include <cmath>",
            "#include <cstdint>",
            "#include <cstring>",
            "#include <limits>",
            "",
            "#ifndef _WIN32",
            '#error "b2_recomp Milestone 5 generated code currently targets Windows hosts only"',
            "#endif",
            "",
            "struct B2RFlags {",
            "    bool cf;",
            "    bool pf;",
            "    bool af;",
            "    bool zf;",
            "    bool sf;",
            "    bool of;",
            "    bool df;",
            "    bool interrupt_enabled;",
            "};",
            "",
            "struct B2RXmm {",
            "    float lane[4];",
            "};",
            "",
            "struct B2RContext {",
            "    uint32_t eax;",
            "    uint32_t ecx;",
            "    uint32_t edx;",
            "    uint32_t ebx;",
            "    uint32_t esp;",
            "    uint32_t ebp;",
            "    uint32_t esi;",
            "    uint32_t edi;",
            "    uint32_t fs_base;",
            "    uint32_t cs_selector;",
            "    uint32_t gdtr_base;",
            "    uint32_t gdtr_limit;",
            "    uint64_t timestamp_counter;",
            "    uint32_t mxcsr;",
            "    uint32_t fpu_control_word;",
            "    uint32_t fpu_status_word;",
            "    float fpu_stack[8];",
            "    uint32_t fpu_depth;",
            "    B2RXmm xmm[8];",
            "    uint64_t mmx[8];",
            "    B2RFlags flags;",
            "    uint32_t eip;",
            "    uint32_t fault_code;",
            "    uint32_t fault_eip;",
            "    uint32_t module_exit_reason;",
            "    uint32_t callback_bypass_target;",
            "    uint64_t steps;",
            "    uint64_t step_budget;",
            "    bool yield_requested;",
            "    bool direct_observed_write_transport;",
            "    void* user;",
            "    uint32_t (*read_u32)(void*, uint32_t);",
            "    void (*write_u32)(void*, uint32_t, uint32_t);",
            "    uint8_t (*read_u8)(void*, uint32_t);",
            "    void (*write_u8)(void*, uint32_t, uint8_t);",
            "    void* native_memory_user;",
            "    bool (*native_read_u32)(void*, uint32_t, uint32_t*);",
            "    bool (*native_write_u32)(void*, uint32_t, uint32_t);",
            "    bool (*native_read_u8)(void*, uint32_t, uint8_t*);",
            "    bool (*native_write_u8)(void*, uint32_t, uint8_t);",
            "    void (*call)(void*, uint32_t, B2RContext*);",
            "    void (*observe)(void*, B2RContext*);",
            "    uint8_t** read_pages;",
            "    uint8_t* callback_pages;",
            "    uint32_t* callback_address_keys;",
            "    uint32_t callback_address_mask;",
            "    uint8_t* write_callback_pages;",
            "    uint32_t* write_callback_address_keys;",
            "    uint32_t write_callback_address_mask;",
            "    uint8_t* zero_read_callback_pages;",
            "    uint32_t* zero_read_callback_address_keys;",
            "    uint32_t zero_read_callback_address_mask;",
            "    bool cache_physical_aliases;",
            "    uint8_t* dirty_pages;",
            "    uint32_t* dirty_page_generations;",
            "    uint32_t* dirty_page_indices;",
            "    uint16_t* dirty_page_min_offsets;",
            "    uint16_t* dirty_page_max_offsets;",
            "    uint32_t dirty_page_count;",
            "    uint32_t dirty_page_capacity;",
            "    uint32_t observed_write_range_start;",
            "    uint32_t observed_write_range_end;",
            "    uint32_t* observed_write_eips;",
            "    uint32_t* observed_write_source_addresses;",
            "    uint32_t* observed_write_addresses;",
            "    uint64_t* observed_write_values;",
            "    uint64_t* observed_write_steps;",
            "    uint8_t* observed_write_sizes;",
            "    uint32_t observed_write_count;",
            "    uint32_t observed_write_capacity;",
            "    uint32_t observed_write_packet_header;",
            "    uint32_t observed_write_packet_next_address;",
            "    uint64_t observed_write_packet_yield_count;",
            "    uint64_t zero_read_callback_bypass_count;",
            "    uint64_t direct_observed_write_count;",
            "    uint64_t direct_observed_write_byte_count;",
            "    uint8_t* direct_observed_payload;",
            "    uint32_t direct_observed_payload_size;",
            "    uint32_t direct_observed_payload_capacity;",
            "    uint32_t* direct_observed_span_addresses;",
            "    uint32_t* direct_observed_span_payload_offsets;",
            "    uint32_t* direct_observed_span_payload_sizes;",
            "    uint32_t* direct_observed_span_write_counts;",
            "    uint8_t* direct_observed_span_flags;",
            "    uint32_t direct_observed_span_count;",
            "    uint32_t direct_observed_span_capacity;",
            "    bool direct_observed_span_sealed;",
            "    uint32_t* native_fast_path_address_keys;",
            "    uint64_t* native_fast_path_call_counts;",
            "    uint32_t native_fast_path_address_mask;",
            "};",
            "",
            "enum B2RModuleExitReason : uint32_t {",
            "    B2R_MODULE_EXIT_FALLTHROUGH_OR_UNKNOWN = 0u,",
            "    B2R_MODULE_EXIT_BRANCH = 1u,",
            "    B2R_MODULE_EXIT_CALL = 2u,",
            "    B2R_MODULE_EXIT_RETURN = 3u,",
            "    B2R_MODULE_EXIT_CALLBACK = 4u,",
            "    B2R_MODULE_EXIT_YIELD = 5u,",
            "    B2R_MODULE_EXIT_STEP_BUDGET = 6u,",
            "    B2R_MODULE_EXIT_FAULT = 7u,",
            "    B2R_MODULE_EXIT_SOFTWARE_INTERRUPT = 8u,",
            "};",
            "",
            "static inline bool b2r_begin_instruction(",
            "    B2RContext* ctx, uint32_t address) {",
            "    if (ctx->step_budget != 0u && ctx->steps >= ctx->step_budget) {",
            "        ctx->eip = address;",
            "        ctx->module_exit_reason = B2R_MODULE_EXIT_STEP_BUDGET;",
            "        return false;",
            "    }",
            "    ++ctx->steps;",
            "    return true;",
            "}",
            "",
            "static inline bool b2r_parity8(uint32_t value) {",
            "    value &= 0xffu;",
            "    value ^= value >> 4;",
            "    value &= 0x0fu;",
            "    return ((0x6996u >> value) & 1u) == 0u;",
            "}",
            "",
            "static inline void b2r_record_native_fast_path(",
            "    B2RContext* ctx, uint32_t address) {",
            "    if (ctx->native_fast_path_address_keys == nullptr ||",
            "        ctx->native_fast_path_call_counts == nullptr) {",
            "        return;",
            "    }",
            "    uint32_t slot = (address * 2654435761u) &",
            "        ctx->native_fast_path_address_mask;",
            "    for (uint32_t probe = 0u;",
            "         probe <= ctx->native_fast_path_address_mask; ++probe) {",
            "        const uint32_t key = ctx->native_fast_path_address_keys[slot];",
            "        if (key == address) {",
            "            ++ctx->native_fast_path_call_counts[slot];",
            "            return;",
            "        }",
            "        if (key == 0xffffffffu) { return; }",
            "        slot = (slot + 1u) & ctx->native_fast_path_address_mask;",
            "    }",
            "}",
            "",
            "static inline uint32_t b2r_mask_for_bits(uint32_t bits) {",
            "    return bits >= 32u ? 0xffffffffu : ((1u << bits) - 1u);",
            "}",
            "",
            "static inline uint32_t b2r_sign_bit_for_bits(uint32_t bits) {",
            "    return 1u << (bits - 1u);",
            "}",
            "",
            "static inline uint32_t b2r_sign_extend(uint32_t value, uint32_t bits) {",
            "    const uint32_t mask = b2r_mask_for_bits(bits);",
            "    const uint32_t sign_bit = b2r_sign_bit_for_bits(bits);",
            "    value &= mask;",
            "    return (value ^ sign_bit) - sign_bit;",
            "}",
            "",
            "static inline int64_t b2r_signed_value(uint32_t value, uint32_t bits) {",
            "    return static_cast<int64_t>(static_cast<int32_t>(b2r_sign_extend(value, bits)));",
            "}",
            "",
            "static inline uint32_t b2r_cache_address(B2RContext* ctx, uint32_t address) {",
            "    if (ctx->cache_physical_aliases &&",
            "        address >= 0xa0000000u && address <= 0xbfffffffu) {",
            "        return address & 0x7fffffffu;",
            "    }",
            "    return address;",
            "}",
            "",
            "static inline bool b2r_requires_read_memory_callback(",
            "    B2RContext* ctx, uint32_t address, uint32_t size) {",
            "    if (ctx->callback_pages == nullptr ||",
            "        ctx->callback_address_keys == nullptr || size == 0u) {",
            "        return false;",
            "    }",
            "    for (uint32_t offset = 0u; offset < size; ++offset) {",
            "        const uint32_t current = b2r_cache_address(ctx, address + offset);",
            "        if (ctx->callback_pages[current >> 12u] == 0u) { continue; }",
            "        uint32_t slot =",
            "            (current * 2654435761u) & ctx->callback_address_mask;",
            "        for (;;) {",
            "            const uint32_t key = ctx->callback_address_keys[slot];",
            "            if (key == current) { return true; }",
            "            if (key == 0xffffffffu) { break; }",
            "            slot = (slot + 1u) & ctx->callback_address_mask;",
            "        }",
            "    }",
            "    return false;",
            "}",
            "",
            "static inline bool b2r_requires_write_memory_callback(",
            "    B2RContext* ctx, uint32_t address, uint32_t size) {",
            "    if (ctx->write_callback_pages == nullptr ||",
            "        ctx->write_callback_address_keys == nullptr || size == 0u) {",
            "        return false;",
            "    }",
            "    for (uint32_t offset = 0u; offset < size; ++offset) {",
            "        const uint32_t current = b2r_cache_address(ctx, address + offset);",
            "        if (ctx->write_callback_pages[current >> 12u] == 0u) { continue; }",
            "        uint32_t slot = (current * 2654435761u) &",
            "            ctx->write_callback_address_mask;",
            "        for (;;) {",
            "            const uint32_t key = ctx->write_callback_address_keys[slot];",
            "            if (key == current) { return true; }",
            "            if (key == 0xffffffffu) { break; }",
            "            slot = (slot + 1u) & ctx->write_callback_address_mask;",
            "        }",
            "    }",
            "    return false;",
            "}",
            "",
            "static inline bool b2r_is_zero_guarded_read_callback(",
            "    B2RContext* ctx, uint32_t address) {",
            "    const uint32_t current = b2r_cache_address(ctx, address);",
            "    if (ctx->zero_read_callback_pages == nullptr ||",
            "        ctx->zero_read_callback_address_keys == nullptr ||",
            "        ctx->zero_read_callback_pages[current >> 12u] == 0u) {",
            "        return false;",
            "    }",
            "    uint32_t slot = (current * 2654435761u) &",
            "        ctx->zero_read_callback_address_mask;",
            "    for (;;) {",
            "        const uint32_t key = ctx->zero_read_callback_address_keys[slot];",
            "        if (key == current) { return true; }",
            "        if (key == 0xffffffffu) { return false; }",
            "        slot = (slot + 1u) & ctx->zero_read_callback_address_mask;",
            "    }",
            "}",
            "",
            "static inline uint8_t b2r_read_u8(B2RContext* ctx, uint32_t address) {",
            "    const uint32_t cache_address = b2r_cache_address(ctx, address);",
            "    const uint32_t page = cache_address >> 12u;",
            "    const bool requires_callback =",
            "        b2r_requires_read_memory_callback(ctx, address, 1u);",
            "    if (ctx->read_pages != nullptr &&",
            "        ctx->read_pages[page] != nullptr &&",
            "        !requires_callback) {",
            "        return ctx->read_pages[page][cache_address & 0xfffu];",
            "    }",
            "    uint8_t native_value = 0u;",
            "    const bool page_miss = ctx->read_pages == nullptr ||",
            "        ctx->read_pages[page] == nullptr;",
            "    if ((requires_callback || page_miss) &&",
            "        ctx->native_read_u8 != nullptr &&",
            "        (ctx->native_read_u8)(",
            "            ctx->native_memory_user, address, &native_value)) {",
            "        return native_value;",
            "    }",
            "    return (ctx->read_u8)(ctx->user, address);",
            "}",
            "",
            "static inline uint32_t b2r_read_u32(B2RContext* ctx, uint32_t address) {",
            "    const uint32_t cache_address = b2r_cache_address(ctx, address);",
            "    const uint32_t page = cache_address >> 12u;",
            "    const bool requires_callback =",
            "        b2r_requires_read_memory_callback(ctx, address, 4u);",
            "    if ((cache_address & 0xfffu) <= 0xffcu &&",
            "        ctx->read_pages != nullptr &&",
            "        ctx->read_pages[page] != nullptr) {",
            "        uint32_t value;",
            "        std::memcpy(&value, ctx->read_pages[page] + (cache_address & 0xfffu), sizeof(value));",
            "        if (!requires_callback) { return value; }",
            "        if (value != 0u &&",
            "            b2r_is_zero_guarded_read_callback(ctx, address)) {",
            "            ++ctx->zero_read_callback_bypass_count;",
            "            return value;",
            "        }",
            "    }",
            "    uint32_t native_value = 0u;",
            "    const bool page_miss = (cache_address & 0xfffu) > 0xffcu ||",
            "        ctx->read_pages == nullptr || ctx->read_pages[page] == nullptr;",
            "    if ((requires_callback || page_miss) &&",
            "        ctx->native_read_u32 != nullptr &&",
            "        (ctx->native_read_u32)(",
            "            ctx->native_memory_user, address, &native_value)) {",
            "        return native_value;",
            "    }",
            "    return (ctx->read_u32)(ctx->user, address);",
            "}",
            "",
            "static inline void b2r_mark_dirty_page(",
            "    B2RContext* ctx, uint32_t page, uint16_t offset, uint16_t size) {",
            "    if (ctx->dirty_pages == nullptr) { return; }",
            "    if (ctx->dirty_page_generations != nullptr) {",
            "        ++ctx->dirty_page_generations[page];",
            "        if (ctx->dirty_page_generations[page] == 0u) {",
            "            ctx->dirty_page_generations[page] = 1u;",
            "        }",
            "    }",
            "    const uint16_t end = static_cast<uint16_t>(offset + size);",
            "    if (ctx->dirty_pages[page] == 0u) {",
            "        ctx->dirty_pages[page] = 1u;",
            "        if (ctx->dirty_page_min_offsets != nullptr) {",
            "            ctx->dirty_page_min_offsets[page] = offset;",
            "            ctx->dirty_page_max_offsets[page] = end;",
            "        }",
            "        if (ctx->dirty_page_indices != nullptr &&",
            "            ctx->dirty_page_count < ctx->dirty_page_capacity) {",
            "            ctx->dirty_page_indices[ctx->dirty_page_count++] = page;",
            "        }",
            "        return;",
            "    }",
            "    if (ctx->dirty_page_min_offsets != nullptr) {",
            "        if (offset < ctx->dirty_page_min_offsets[page]) {",
            "            ctx->dirty_page_min_offsets[page] = offset;",
            "        }",
            "        if (end > ctx->dirty_page_max_offsets[page]) {",
            "            ctx->dirty_page_max_offsets[page] = end;",
            "        }",
            "    }",
            "}",
            "",
            "static inline bool b2r_track_observed_write_packet(",
            "    B2RContext* ctx, uint32_t address, uint8_t size, uint64_t value) {",
            "    const uint64_t initial_yield_count =",
            "        ctx->observed_write_packet_yield_count;",
            "    if (ctx->observed_write_packet_header == 0u || size < 4u) { return false; }",
            "    for (uint32_t offset = 0u; offset + 4u <= size; offset += 4u) {",
            "        uint32_t word_address = address + offset;",
            "        if (word_address == ctx->observed_write_range_end) {",
            "            word_address = ctx->observed_write_range_start;",
            "        }",
            "        const uint32_t word = static_cast<uint32_t>(value >> (offset * 8u));",
            "        const uint32_t pending_address =",
            "            ctx->observed_write_packet_next_address;",
            "        if (pending_address != 0u) {",
            "            ctx->observed_write_packet_next_address = 0u;",
            "            if (word_address == pending_address) {",
            "                ++ctx->observed_write_packet_yield_count;",
            "                ctx->yield_requested = true;",
            "            }",
            "        }",
            "        if (word == ctx->observed_write_packet_header) {",
            "            uint32_t next_address = word_address + 4u;",
            "            if (next_address == ctx->observed_write_range_end) {",
            "                next_address = ctx->observed_write_range_start;",
            "            }",
            "            if (next_address >= ctx->observed_write_range_start &&",
            "                next_address < ctx->observed_write_range_end) {",
            "                ctx->observed_write_packet_next_address = next_address;",
            "            }",
            "        }",
            "    }",
            "    return ctx->observed_write_packet_yield_count != initial_yield_count;",
            "}",
            "",
            "static inline void b2r_record_observed_write(",
            "    B2RContext* ctx, uint32_t address, uint8_t size, uint64_t value) {",
            "    if (ctx->observed_write_addresses == nullptr ||",
            "        address < ctx->observed_write_range_start ||",
            "        address >= ctx->observed_write_range_end) {",
            "        return;",
            "    }",
            "    const uint32_t index = ctx->observed_write_count;",
            "    if (index >= ctx->observed_write_capacity) {",
            "        ctx->yield_requested = true;",
            "        return;",
            "    }",
            "    if (ctx->observed_write_eips != nullptr) {",
            "        ctx->observed_write_eips[index] = ctx->eip;",
            "    }",
            "    if (ctx->observed_write_source_addresses != nullptr) {",
            "        ctx->observed_write_source_addresses[index] = ctx->esi;",
            "    }",
            "    ctx->observed_write_addresses[index] = address;",
            "    ctx->observed_write_values[index] = value;",
            "    if (ctx->observed_write_steps != nullptr) {",
            "        ctx->observed_write_steps[index] = ctx->steps;",
            "    }",
            "    ctx->observed_write_sizes[index] = size;",
            "    ctx->observed_write_count = index + 1u;",
            "    b2r_track_observed_write_packet(ctx, address, size, value);",
            "}",
            "",
            "static inline bool b2r_record_direct_observed_write(",
            "    B2RContext* ctx, uint32_t address, uint8_t size, uint64_t value) {",
            "    if (ctx->direct_observed_payload == nullptr ||",
            "        ctx->direct_observed_span_addresses == nullptr ||",
            "        ctx->direct_observed_span_payload_offsets == nullptr ||",
            "        ctx->direct_observed_span_payload_sizes == nullptr ||",
            "        ctx->direct_observed_span_write_counts == nullptr ||",
            "        ctx->direct_observed_span_flags == nullptr ||",
            "        ctx->direct_observed_payload_size",
            "            > ctx->direct_observed_payload_capacity ||",
            "        size > ctx->direct_observed_payload_capacity",
            "            - ctx->direct_observed_payload_size) {",
            "        ctx->yield_requested = true;",
            "        return false;",
            "    }",
            "    bool start_span = ctx->direct_observed_span_count == 0u ||",
            "        ctx->direct_observed_span_sealed;",
            "    if (!start_span) {",
            "        const uint32_t last = ctx->direct_observed_span_count - 1u;",
            "        start_span = ctx->direct_observed_span_addresses[last]",
            "                + ctx->direct_observed_span_payload_sizes[last] != address ||",
            "            ctx->direct_observed_span_payload_offsets[last]",
            "                + ctx->direct_observed_span_payload_sizes[last]",
            "                != ctx->direct_observed_payload_size;",
            "    }",
            "    if (start_span) {",
            "        if (ctx->direct_observed_span_count",
            "            >= ctx->direct_observed_span_capacity) {",
            "            ctx->yield_requested = true;",
            "            return false;",
            "        }",
            "        const uint32_t span = ctx->direct_observed_span_count++;",
            "        ctx->direct_observed_span_addresses[span] = address;",
            "        ctx->direct_observed_span_payload_offsets[span] =",
            "            ctx->direct_observed_payload_size;",
            "        ctx->direct_observed_span_payload_sizes[span] = 0u;",
            "        ctx->direct_observed_span_write_counts[span] = 0u;",
            "        ctx->direct_observed_span_flags[span] = 0u;",
            "        ctx->direct_observed_span_sealed = false;",
            "    }",
            "    const uint32_t span = ctx->direct_observed_span_count - 1u;",
            "    std::memcpy(",
            "        ctx->direct_observed_payload + ctx->direct_observed_payload_size,",
            "        &value, size);",
            "    ctx->direct_observed_payload_size += size;",
            "    ctx->direct_observed_span_payload_sizes[span] += size;",
            "    ++ctx->direct_observed_span_write_counts[span];",
            "    ++ctx->direct_observed_write_count;",
            "    ctx->direct_observed_write_byte_count += size;",
            "    if (b2r_track_observed_write_packet(ctx, address, size, value)) {",
            "        ctx->direct_observed_span_flags[span] |= 1u;",
            "        ctx->direct_observed_span_sealed = true;",
            "    }",
            "    return true;",
            "}",
            "",
            "static inline void b2r_write_u8(B2RContext* ctx, uint32_t address, uint8_t value) {",
            "    const uint32_t cache_address = b2r_cache_address(ctx, address);",
            "    const uint32_t page = cache_address >> 12u;",
            "    const bool requires_callback =",
            "        b2r_requires_write_memory_callback(ctx, address, 1u);",
            "    const bool page_miss = ctx->read_pages == nullptr ||",
            "        ctx->read_pages[page] == nullptr;",
            "    if ((requires_callback || page_miss) &&",
            "        ctx->native_write_u8 != nullptr &&",
            "        (ctx->native_write_u8)(ctx->native_memory_user, address, value)) {",
            "        b2r_record_observed_write(ctx, cache_address, 1u, value);",
            "        return;",
            "    }",
            "    if (ctx->direct_observed_write_transport &&",
            "        ctx->observed_write_range_start < ctx->observed_write_range_end &&",
            "        ctx->observed_write_range_end - ctx->observed_write_range_start >= 1u &&",
            "        cache_address >= ctx->observed_write_range_start &&",
            "        cache_address < ctx->observed_write_range_end &&",
            "        !requires_callback &&",
            "        b2r_record_direct_observed_write(",
            "            ctx, cache_address, 1u, value)) {",
            "        return;",
            "    }",
            "    if (ctx->read_pages != nullptr &&",
            "        ctx->read_pages[page] != nullptr &&",
            "        !requires_callback) {",
            "        b2r_record_observed_write(ctx, cache_address, 1u, value);",
            "        ctx->read_pages[page][cache_address & 0xfffu] = value;",
            "        b2r_mark_dirty_page(ctx, page, static_cast<uint16_t>(cache_address & 0xfffu), 1u);",
            "        return;",
            "    }",
            "    (ctx->write_u8)(ctx->user, address, value);",
            "}",
            "",
            "static inline void b2r_write_u32(B2RContext* ctx, uint32_t address, uint32_t value) {",
            "    const uint32_t cache_address = b2r_cache_address(ctx, address);",
            "    const uint32_t page = cache_address >> 12u;",
            "    const bool requires_callback =",
            "        b2r_requires_write_memory_callback(ctx, address, 4u);",
            "    const bool page_miss = (cache_address & 0xfffu) > 0xffcu ||",
            "        ctx->read_pages == nullptr || ctx->read_pages[page] == nullptr;",
            "    if ((requires_callback || page_miss) &&",
            "        ctx->native_write_u32 != nullptr &&",
            "        (ctx->native_write_u32)(ctx->native_memory_user, address, value)) {",
            "        b2r_record_observed_write(ctx, cache_address, 4u, value);",
            "        return;",
            "    }",
            "    if (ctx->direct_observed_write_transport &&",
            "        ctx->observed_write_range_start < ctx->observed_write_range_end &&",
            "        ctx->observed_write_range_end - ctx->observed_write_range_start >= 4u &&",
            "        cache_address >= ctx->observed_write_range_start &&",
            "        cache_address <= ctx->observed_write_range_end - 4u &&",
            "        !requires_callback &&",
            "        b2r_record_direct_observed_write(",
            "            ctx, cache_address, 4u, value)) {",
            "        return;",
            "    }",
            "    if ((cache_address & 0xfffu) <= 0xffcu &&",
            "        ctx->read_pages != nullptr &&",
            "        ctx->read_pages[page] != nullptr &&",
            "        !requires_callback) {",
            "        b2r_record_observed_write(ctx, cache_address, 4u, value);",
            "        std::memcpy(ctx->read_pages[page] + (cache_address & 0xfffu), &value, sizeof(value));",
            "        b2r_mark_dirty_page(ctx, page, static_cast<uint16_t>(cache_address & 0xfffu), 4u);",
            "        return;",
            "    }",
            "    (ctx->write_u32)(ctx->user, address, value);",
            "}",
            "",
            "static inline void b2r_write_push_u64(",
            "    B2RContext* ctx, uint32_t address, uint64_t value) {",
            "    const uint32_t cache_address = b2r_cache_address(ctx, address);",
            "    if (ctx->direct_observed_write_transport &&",
            "        ctx->observed_write_range_start < ctx->observed_write_range_end &&",
            "        ctx->observed_write_range_end - ctx->observed_write_range_start >= 8u &&",
            "        cache_address >= ctx->observed_write_range_start &&",
            "        cache_address <= ctx->observed_write_range_end - 8u &&",
            "        !b2r_requires_write_memory_callback(ctx, address, 8u) &&",
            "        b2r_record_direct_observed_write(",
            "            ctx, cache_address, 8u, value)) {",
            "        return;",
            "    }",
            "    b2r_write_u32(ctx, address, static_cast<uint32_t>(value));",
            "    b2r_write_u32(ctx, address + 4u, static_cast<uint32_t>(value >> 32u));",
            "}",
            "",
            "static inline uint16_t b2r_read_u16(B2RContext* ctx, uint32_t address) {",
            "    return static_cast<uint16_t>(",
            "        static_cast<uint16_t>(b2r_read_u8(ctx, address)) |",
            "        static_cast<uint16_t>(b2r_read_u8(ctx, address + 1u) << 8));",
            "}",
            "",
            "static inline void b2r_write_u16(B2RContext* ctx, uint32_t address, uint16_t value) {",
            "    b2r_write_u8(ctx, address, static_cast<uint8_t>(value & 0xffu));",
            "    b2r_write_u8(ctx, address + 1u, static_cast<uint8_t>((value >> 8) & 0xffu));",
            "}",
            "",
            "static inline int64_t b2r_read_i64(B2RContext* ctx, uint32_t address) {",
            "    const uint64_t low = static_cast<uint64_t>(b2r_read_u32(ctx, address));",
            "    const uint64_t high = static_cast<uint64_t>(b2r_read_u32(ctx, address + 4u));",
            "    return static_cast<int64_t>((high << 32) | low);",
            "}",
            "",
            "static inline uint64_t b2r_read_u64(B2RContext* ctx, uint32_t address) {",
            "    const uint64_t low = static_cast<uint64_t>(b2r_read_u32(ctx, address));",
            "    const uint64_t high = static_cast<uint64_t>(b2r_read_u32(ctx, address + 4u));",
            "    return (high << 32) | low;",
            "}",
            "",
            "static inline void b2r_write_i64(B2RContext* ctx, uint32_t address, int64_t value) {",
            "    const uint64_t bits = static_cast<uint64_t>(value);",
            "    b2r_write_u32(ctx, address, static_cast<uint32_t>(bits & 0xffffffffu));",
            "    b2r_write_u32(ctx, address + 4u, static_cast<uint32_t>(bits >> 32));",
            "}",
            "",
            "static inline void b2r_write_u64(B2RContext* ctx, uint32_t address, uint64_t value) {",
            "    b2r_write_u32(ctx, address, static_cast<uint32_t>(value & 0xffffffffu));",
            "    b2r_write_u32(ctx, address + 4u, static_cast<uint32_t>(value >> 32));",
            "}",
            "",
            "static inline int64_t b2r_fistp_i64(float value) {",
            "    if (!std::isfinite(value)) {",
            "        return std::numeric_limits<int64_t>::min();",
            "    }",
            "    const double rounded = std::nearbyint(static_cast<double>(value));",
            "    if (rounded < static_cast<double>(std::numeric_limits<int64_t>::min()) ||",
            "        rounded > static_cast<double>(std::numeric_limits<int64_t>::max())) {",
            "        return std::numeric_limits<int64_t>::min();",
            "    }",
            "    return static_cast<int64_t>(rounded);",
            "}",
            "",
            "static inline uint32_t b2r_fistp_i32(float value) {",
            "    if (!std::isfinite(value)) {",
            "        return 0x80000000u;",
            "    }",
            "    const double rounded = std::nearbyint(static_cast<double>(value));",
            "    if (rounded < static_cast<double>(std::numeric_limits<int32_t>::min()) ||",
            "        rounded > static_cast<double>(std::numeric_limits<int32_t>::max())) {",
            "        return 0x80000000u;",
            "    }",
            "    return static_cast<uint32_t>(static_cast<int32_t>(rounded));",
            "}",
            "",
            "static inline void b2r_logic_flags(B2RContext* ctx, uint32_t result, uint32_t bits) {",
            "    const uint32_t mask = b2r_mask_for_bits(bits);",
            "    const uint32_t sign_bit = b2r_sign_bit_for_bits(bits);",
            "    result &= mask;",
            "    ctx->flags.cf = false;",
            "    ctx->flags.of = false;",
            "    ctx->flags.af = false;",
            "    ctx->flags.zf = result == 0u;",
            "    ctx->flags.sf = (result & sign_bit) != 0u;",
            "    ctx->flags.pf = b2r_parity8(result);",
            "}",
            "",
            "static inline void b2r_add_flags(B2RContext* ctx, uint32_t lhs, uint64_t rhs, uint32_t result, uint32_t bits) {",
            "    const uint32_t mask = b2r_mask_for_bits(bits);",
            "    const uint32_t sign_bit = b2r_sign_bit_for_bits(bits);",
            "    lhs &= mask;",
            "    const uint64_t rhs_full = rhs;",
            "    const uint32_t rhs_masked = static_cast<uint32_t>(rhs) & mask;",
            "    result &= mask;",
            "    ctx->flags.cf = (static_cast<uint64_t>(lhs) + rhs_full) > mask;",
            "    ctx->flags.of = ((~(lhs ^ rhs_masked) & (lhs ^ result) & sign_bit) != 0u);",
            "    ctx->flags.af = ((lhs ^ rhs_masked ^ result) & 0x10u) != 0u;",
            "    ctx->flags.zf = result == 0u;",
            "    ctx->flags.sf = (result & sign_bit) != 0u;",
            "    ctx->flags.pf = b2r_parity8(result);",
            "}",
            "",
            "static inline void b2r_sub_flags(B2RContext* ctx, uint32_t lhs, uint64_t rhs, uint32_t result, uint32_t bits) {",
            "    const uint32_t mask = b2r_mask_for_bits(bits);",
            "    const uint32_t sign_bit = b2r_sign_bit_for_bits(bits);",
            "    lhs &= mask;",
            "    const uint64_t rhs_full = rhs;",
            "    const uint32_t rhs_masked = static_cast<uint32_t>(rhs) & mask;",
            "    result &= mask;",
            "    ctx->flags.cf = static_cast<uint64_t>(lhs) < rhs_full;",
            "    ctx->flags.of = (((lhs ^ rhs_masked) & (lhs ^ result) & sign_bit) != 0u);",
            "    ctx->flags.af = ((lhs ^ rhs_masked ^ result) & 0x10u) != 0u;",
            "    ctx->flags.zf = result == 0u;",
            "    ctx->flags.sf = (result & sign_bit) != 0u;",
            "    ctx->flags.pf = b2r_parity8(result);",
            "}",
            "",
            "static inline void b2r_push(B2RContext* ctx, uint32_t value) {",
            "    ctx->esp -= 4u;",
            "    ctx->write_u32(ctx->user, ctx->esp, value);",
            "}",
            "",
            "static inline uint32_t b2r_pop(B2RContext* ctx) {",
            "    const uint32_t value = ctx->read_u32(ctx->user, ctx->esp);",
            "    ctx->esp += 4u;",
            "    return value;",
            "}",
            "",
            "static inline float b2r_read_f32(B2RContext* ctx, uint32_t address) {",
            "    const uint32_t bits = ctx->read_u32(ctx->user, address);",
            "    float value;",
            "    std::memcpy(&value, &bits, sizeof(value));",
            "    return value;",
            "}",
            "",
            "static inline float b2r_read_f64(B2RContext* ctx, uint32_t address) {",
            "    const uint64_t bits = b2r_read_u64(ctx, address);",
            "    double value;",
            "    std::memcpy(&value, &bits, sizeof(value));",
            "    return static_cast<float>(value);",
            "}",
            "",
            "static inline void b2r_write_f32(B2RContext* ctx, uint32_t address, float value) {",
            "    uint32_t bits;",
            "    std::memcpy(&bits, &value, sizeof(bits));",
            "    ctx->write_u32(ctx->user, address, bits);",
            "}",
            "",
            "static inline void b2r_write_f64(B2RContext* ctx, uint32_t address, float value) {",
            "    const double widened = static_cast<double>(value);",
            "    uint64_t bits;",
            "    std::memcpy(&bits, &widened, sizeof(bits));",
            "    b2r_write_u64(ctx, address, bits);",
            "}",
            "",
            "static inline float b2r_read_f80(B2RContext* ctx, uint32_t address) {",
            "    const uint64_t significand = b2r_read_u64(ctx, address);",
            "    const uint16_t exponent_sign = b2r_read_u16(ctx, address + 8u);",
            "    const uint32_t exponent = exponent_sign & 0x7fffu;",
            "    float value;",
            "    if (exponent == 0x7fffu) {",
            "        value = significand == 0x8000000000000000ull",
            "            ? std::numeric_limits<float>::infinity()",
            "            : std::numeric_limits<float>::quiet_NaN();",
            "    } else if (exponent == 0u && significand == 0u) {",
            "        value = 0.0f;",
            "    } else {",
            "        const double fraction = static_cast<double>(significand) / 9223372036854775808.0;",
            "        const int unbiased = exponent == 0u ? 1 - 16383 : static_cast<int>(exponent) - 16383;",
            "        value = static_cast<float>(std::ldexp(fraction, unbiased));",
            "    }",
            "    return (exponent_sign & 0x8000u) != 0u ? -value : value;",
            "}",
            "",
            "static inline void b2r_write_f80(B2RContext* ctx, uint32_t address, float value) {",
            "    uint64_t significand = 0u;",
            "    uint16_t exponent = 0u;",
            "    const float magnitude = std::fabs(value);",
            "    if (std::isnan(value)) {",
            "        significand = 0xc000000000000000ull;",
            "        exponent = 0x7fffu;",
            "    } else if (std::isinf(value)) {",
            "        significand = 0x8000000000000000ull;",
            "        exponent = 0x7fffu;",
            "    } else if (magnitude != 0.0f) {",
            "        int binary_exponent = 0;",
            "        const double fraction = std::frexp(static_cast<double>(magnitude), &binary_exponent);",
            "        exponent = static_cast<uint16_t>(binary_exponent - 1 + 16383);",
            "        significand = static_cast<uint64_t>(std::ldexp(fraction, 64));",
            "    }",
            "    if (std::signbit(value)) { exponent |= 0x8000u; }",
            "    b2r_write_u64(ctx, address, significand);",
            "    b2r_write_u16(ctx, address + 8u, exponent);",
            "}",
            "",
            "static inline B2RXmm b2r_read_xmm(B2RContext* ctx, uint32_t address) {",
            "    B2RXmm value{};",
            "    for (uint32_t lane = 0u; lane < 4u; ++lane) {",
            "        const uint32_t bits = ctx->read_u32(ctx->user, address + lane * 4u);",
            "        std::memcpy(&value.lane[lane], &bits, sizeof(bits));",
            "    }",
            "    return value;",
            "}",
            "",
            "static inline void b2r_write_xmm(B2RContext* ctx, uint32_t address, B2RXmm value) {",
            "    for (uint32_t lane = 0u; lane < 4u; ++lane) {",
            "        uint32_t bits;",
            "        std::memcpy(&bits, &value.lane[lane], sizeof(bits));",
            "        ctx->write_u32(ctx->user, address + lane * 4u, bits);",
            "    }",
            "}",
            "",
            "static inline B2RXmm b2r_xmm_shuffle(B2RXmm destination, B2RXmm source, uint32_t immediate) {",
            "    B2RXmm result{};",
            "    result.lane[0] = destination.lane[immediate & 0x03u];",
            "    result.lane[1] = destination.lane[(immediate >> 2u) & 0x03u];",
            "    result.lane[2] = source.lane[(immediate >> 4u) & 0x03u];",
            "    result.lane[3] = source.lane[(immediate >> 6u) & 0x03u];",
            "    return result;",
            "}",
            "",
            "static inline B2RXmm b2r_xmm_unpack_low(B2RXmm destination, B2RXmm source) {",
            "    return B2RXmm{{destination.lane[0], source.lane[0], destination.lane[1], source.lane[1]}};",
            "}",
            "",
            "static inline B2RXmm b2r_xmm_unpack_high(B2RXmm destination, B2RXmm source) {",
            "    return B2RXmm{{destination.lane[2], source.lane[2], destination.lane[3], source.lane[3]}};",
            "}",
            "",
            "static inline B2RXmm b2r_xmm_move_low_to_high(B2RXmm destination, B2RXmm source) {",
            "    return B2RXmm{{destination.lane[0], destination.lane[1], source.lane[0], source.lane[1]}};",
            "}",
            "",
            "static inline B2RXmm b2r_xmm_move_high_to_low(B2RXmm destination, B2RXmm source) {",
            "    return B2RXmm{{source.lane[2], source.lane[3], destination.lane[2], destination.lane[3]}};",
            "}",
            "",
            "static inline B2RXmm b2r_xmm_add(B2RXmm left, B2RXmm right) {",
            "    B2RXmm result{};",
            "    for (uint32_t lane = 0u; lane < 4u; ++lane) {",
            "        result.lane[lane] = left.lane[lane] + right.lane[lane];",
            "    }",
            "    return result;",
            "}",
            "",
            "static inline B2RXmm b2r_xmm_multiply(B2RXmm left, B2RXmm right) {",
            "    B2RXmm result{};",
            "    for (uint32_t lane = 0u; lane < 4u; ++lane) {",
            "        result.lane[lane] = left.lane[lane] * right.lane[lane];",
            "    }",
            "    return result;",
            "}",
            "",
            "static inline float b2r_sse_minmax_lane(float left, float right, bool minimum) {",
            "    if (std::isnan(left) || std::isnan(right) || left == right) {",
            "        return right;",
            "    }",
            "    return minimum ? (left < right ? left : right) : (left > right ? left : right);",
            "}",
            "",
            "static inline B2RXmm b2r_xmm_minimum(B2RXmm left, B2RXmm right) {",
            "    B2RXmm result{};",
            "    for (uint32_t lane = 0u; lane < 4u; ++lane) {",
            "        result.lane[lane] = b2r_sse_minmax_lane(left.lane[lane], right.lane[lane], true);",
            "    }",
            "    return result;",
            "}",
            "",
            "static inline B2RXmm b2r_xmm_maximum(B2RXmm left, B2RXmm right) {",
            "    B2RXmm result{};",
            "    for (uint32_t lane = 0u; lane < 4u; ++lane) {",
            "        result.lane[lane] = b2r_sse_minmax_lane(left.lane[lane], right.lane[lane], false);",
            "    }",
            "    return result;",
            "}",
            "",
            "static inline B2RXmm b2r_xmm_sqrt(B2RXmm value) {",
            "    B2RXmm result{};",
            "    for (uint32_t lane = 0u; lane < 4u; ++lane) {",
            "        result.lane[lane] = std::sqrt(value.lane[lane]);",
            "    }",
            "    return result;",
            "}",
            "",
            "static inline uint32_t b2r_cvttss2si(float value) {",
            "    if (!std::isfinite(value) || value < -2147483648.0f || value > 2147483647.0f) {",
            "        return 0x80000000u;",
            "    }",
            "    return static_cast<uint32_t>(static_cast<int32_t>(value));",
            "}",
            "",
            "static inline uint32_t b2r_cvtss2si(float value, uint32_t mxcsr) {",
            "    if (!std::isfinite(value)) { return 0x80000000u; }",
            "    double rounded;",
            "    switch ((mxcsr >> 13u) & 0x03u) {",
            "        case 0u: rounded = std::nearbyint(static_cast<double>(value)); break;",
            "        case 1u: rounded = std::floor(static_cast<double>(value)); break;",
            "        case 2u: rounded = std::ceil(static_cast<double>(value)); break;",
            "        default: rounded = std::trunc(static_cast<double>(value)); break;",
            "    }",
            "    if (rounded < static_cast<double>(std::numeric_limits<int32_t>::min()) ||",
            "        rounded > static_cast<double>(std::numeric_limits<int32_t>::max())) {",
            "        return 0x80000000u;",
            "    }",
            "    return static_cast<uint32_t>(static_cast<int32_t>(rounded));",
            "}",
            "",
            "static inline float b2r_rsqrtss(float value) {",
            "    if (value <= 0.0f) {",
            "        return std::numeric_limits<float>::quiet_NaN();",
            "    }",
            "    return 1.0f / std::sqrt(value);",
            "}",
            "",
            "static inline void b2r_fpu_push(B2RContext* ctx, float value) {",
            "    for (uint32_t index = 7u; index > 0u; --index) {",
            "        ctx->fpu_stack[index] = ctx->fpu_stack[index - 1u];",
            "    }",
            "    ctx->fpu_stack[0] = value;",
            "    if (ctx->fpu_depth < 8u) {",
            "        ++ctx->fpu_depth;",
            "    }",
            "}",
            "",
            "static inline float b2r_fpu_pop(B2RContext* ctx) {",
            "    const float value = ctx->fpu_stack[0];",
            "    for (uint32_t index = 0u; index < 7u; ++index) {",
            "        ctx->fpu_stack[index] = ctx->fpu_stack[index + 1u];",
            "    }",
            "    if (ctx->fpu_depth > 0u) {",
            "        --ctx->fpu_depth;",
            "    }",
            "    return value;",
            "}",
            "",
            "static inline void b2r_fpu_free(B2RContext* ctx, uint32_t index) {",
            "    if (index >= ctx->fpu_depth) { return; }",
            "    for (uint32_t cursor = index; cursor + 1u < ctx->fpu_depth; ++cursor) {",
            "        ctx->fpu_stack[cursor] = ctx->fpu_stack[cursor + 1u];",
            "    }",
            "    --ctx->fpu_depth;",
            "}",
            "",
            "static inline uint32_t b2r_fpu_tag(float value) {",
            "    if (std::isnan(value) || std::isinf(value)) { return 2u; }",
            "    return value == 0.0f ? 1u : 0u;",
            "}",
            "",
            "static inline void b2r_fnsave(B2RContext* ctx, uint32_t address) {",
            "    b2r_write_u16(ctx, address, static_cast<uint16_t>(ctx->fpu_control_word));",
            "    b2r_write_u16(ctx, address + 2u, 0u);",
            "    b2r_write_u16(ctx, address + 4u, static_cast<uint16_t>(ctx->fpu_status_word));",
            "    b2r_write_u16(ctx, address + 6u, 0u);",
            "    uint16_t tag_word = 0u;",
            "    for (uint32_t index = 0u; index < 8u; ++index) {",
            "        const uint32_t tag = index < ctx->fpu_depth ? b2r_fpu_tag(ctx->fpu_stack[index]) : 3u;",
            "        tag_word |= static_cast<uint16_t>(tag << (index * 2u));",
            "    }",
            "    b2r_write_u16(ctx, address + 8u, tag_word);",
            "    b2r_write_u16(ctx, address + 10u, 0u);",
            "    for (uint32_t offset = 12u; offset < 28u; offset += 4u) {",
            "        b2r_write_u32(ctx, address + offset, 0u);",
            "    }",
            "    for (uint32_t index = 0u; index < ctx->fpu_depth; ++index) {",
            "        b2r_write_f80(ctx, address + 28u + index * 10u, ctx->fpu_stack[index]);",
            "    }",
            f"    ctx->fpu_control_word = {DEFAULT_X87_CONTROL_WORD}u;",
            "    ctx->fpu_status_word = 0u;",
            "    ctx->fpu_depth = 0u;",
            "}",
            "",
            "static inline void b2r_frstor(B2RContext* ctx, uint32_t address) {",
            "    ctx->fpu_control_word = b2r_read_u16(ctx, address);",
            "    ctx->fpu_status_word = b2r_read_u16(ctx, address + 4u);",
            "    const uint16_t tag_word = b2r_read_u16(ctx, address + 8u);",
            "    ctx->fpu_depth = 0u;",
            "    for (uint32_t index = 0u; index < 8u; ++index) {",
            "        if (((tag_word >> (index * 2u)) & 0x03u) == 3u) { continue; }",
            "        ctx->fpu_stack[ctx->fpu_depth++] = b2r_read_f80(ctx, address + 28u + index * 10u);",
            "    }",
            "}",
            "",
            "static inline uint32_t b2r_fpu_compare_status(float left, float right, uint32_t status_word) {",
            "    status_word &= ~0x4700u;",
            "    if (std::isnan(left) || std::isnan(right)) {",
            "        return status_word | 0x4500u;",
            "    }",
            "    if (left < right) {",
            "        return status_word | 0x0100u;",
            "    }",
            "    if (left > right) {",
            "        return status_word;",
            "    }",
            "    return status_word | 0x4000u;",
            "}",
            "",
            f'extern "C" __declspec(dllexport) uint32_t {symbol}(B2RContext* ctx) {{',
            (
                f"    uint32_t eip = ctx->eip != 0u ? ctx->eip : {_cpp_u32(function.base_address)};"
                if resumable
                else f"    uint32_t eip = {_cpp_u32(function.base_address)};"
            ),
            *(
                [
                    "    uint32_t pending_module_exit_reason = ",
                    "        B2R_MODULE_EXIT_FALLTHROUGH_OR_UNKNOWN;",
                ]
                if resumable
                else []
            ),
            "    for (;;) {",
        ]
        if resumable:
            local_callback_addresses = self._callback_addresses & {
                instruction.address for instruction in function.instructions
            }
            if local_callback_addresses:
                callback_condition = " || ".join(
                    f"eip == {_cpp_u32(address)}"
                    for address in sorted(local_callback_addresses)
                )
                lines.extend(
                    [
                        f"        if ({callback_condition}) {{",
                        "            if (ctx->callback_bypass_target == eip) {",
                        "                ctx->callback_bypass_target = 0u;",
                        "            } else {",
                        "                ctx->eip = eip;",
                        "                ctx->module_exit_reason = B2R_MODULE_EXIT_CALLBACK;",
                        "                return eip;",
                        "            }",
                        "        }",
                    ]
                )
            lines.extend(
                [
                    "        if (ctx->yield_requested) {",
                    "            ctx->eip = eip;",
                    "            ctx->module_exit_reason = B2R_MODULE_EXIT_YIELD;",
                    "            return eip;",
                    "        }",
                ]
            )
        lines.extend(
            [
            "        switch (eip) {",
            ]
        )
        for instruction in function.instructions:
            native_fast_path = self._native_fast_paths.get(instruction.address)
            emitted_lines = self._emit_instruction(instruction)
            instruction_records_memory_write = any(
                token in line
                for line in (
                    *emitted_lines,
                    *(native_fast_path.body if native_fast_path is not None else ()),
                )
                for token in (
                    "ctx->write_u8(ctx->user,",
                    "ctx->write_u32(ctx->user,",
                    "b2r_write_",
                    "b2r_fpu_store",
                )
            )
            if (
                resumable
                and not instruction_records_memory_write
                and native_fast_path is None
                and instruction.address not in self._observer_addresses
                and instruction.next_address not in self._callback_addresses
            ):
                emitted_lines = self._direct_thread_fallthrough(
                    instruction,
                    emitted_lines,
                )
            lines.append(f"        case {_cpp_u32(instruction.address)}: {{")
            if resumable:
                lines.append(
                    "            if (!b2r_begin_instruction(ctx, "
                    f"{_cpp_u32(instruction.address)})) "
                    f"{{ return {_cpp_u32(instruction.address)}; }}"
                )
                lines.append(
                    "            pending_module_exit_reason = "
                    "B2R_MODULE_EXIT_FALLTHROUGH_OR_UNKNOWN;"
                )
            lines.append(f"            // {instruction.text()}")
            # Native observed-write provenance needs the exact producer EIP,
            # but synchronizing it for every guest instruction measurably
            # slows the live loop. Store instructions are the only default
            # path that records it, so keep those precise without imposing
            # the full callback-audit cost.
            if (
                self._synchronize_eip_for_callbacks
                or instruction_records_memory_write
            ):
                lines.append(
                    f"            ctx->eip = {_cpp_u32(instruction.address)};"
                )
            if instruction.address in self._observer_addresses:
                lines.append(
                    "            if (ctx->observe != nullptr) { "
                    f"ctx->eip = {_cpp_u32(instruction.address)}; "
                    "ctx->observe(ctx->user, ctx); }"
                )
            if native_fast_path is not None:
                lines.append(
                    f"            // Native fast path: {native_fast_path.name}"
                )
                lines.append(f"            if ({native_fast_path.guard}) {{")
                lines.extend(
                    f"                {line}" for line in native_fast_path.body
                )
                lines.append("            }")
            lines.extend(f"            {line}" for line in emitted_lines)
            lines.append("        }")
        lines.extend(
            [
                "        default:",
                *(
                    [
                        "            ctx->module_exit_reason = pending_module_exit_reason;",
                    ]
                    if resumable
                    else []
                ),
                *( ["            ctx->eip = eip;"] if resumable else [] ),
                "            return eip;",
                "        }",
                "    }",
                "}",
                "",
            ]
        )
        source = "\n".join(lines)
        return (
            source.replace("ctx->read_u32(ctx->user,", "b2r_read_u32(ctx,")
            .replace("ctx->write_u32(ctx->user,", "b2r_write_u32(ctx,")
            .replace("ctx->read_u8(ctx->user,", "b2r_read_u8(ctx,")
            .replace("ctx->write_u8(ctx->user,", "b2r_write_u8(ctx,")
        )

    def _direct_thread_fallthrough(
        self,
        instruction: X86Instruction,
        emitted_lines: list[str],
    ) -> list[str]:
        if emitted_lines[-2:] != [
            f"eip = {_cpp_u32(instruction.next_address)};",
            "continue;",
        ]:
            return emitted_lines
        if _u32(instruction.address) not in self._direct_fallthrough_addresses:
            return emitted_lines
        return emitted_lines[:-2]

    def _repeat_checkpoint_lines(self, instruction: X86Instruction) -> list[str]:
        if not self._resumable:
            return []
        address = _cpp_u32(instruction.address)
        return [
            "    ++chunk_iterations;",
            (
                "    if (ctx->ecx != 0u && (ctx->yield_requested || "
                f"chunk_iterations >= {RESUMABLE_REPEAT_CHUNK_ITERATIONS}u)) {{"
            ),
            f"        ctx->eip = {address};",
            "        --ctx->steps;",
            "        ctx->yield_requested = true;",
            "        ctx->module_exit_reason = B2R_MODULE_EXIT_YIELD;",
            f"        return {address};",
            "    }",
        ]

    def _emit_instruction(self, instruction: X86Instruction) -> list[str]:
        next_eip = _cpp_u32(instruction.next_address)
        if instruction.mnemonic == "nop":
            return [f"eip = {next_eip};", "continue;"]
        if instruction.mnemonic in {"cld", "std"}:
            return [
                f"ctx->flags.df = {'true' if instruction.mnemonic == 'std' else 'false'};",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "fpatan":
            return [
                "const float x = b2r_fpu_pop(ctx);",
                "ctx->fpu_stack[0] = std::atan2(ctx->fpu_stack[0], x);",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic in {"cli", "sti"}:
            return [
                "ctx->flags.interrupt_enabled = "
                f"{'true' if instruction.mnemonic == 'sti' else 'false'};",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "sahf":
            return [
                "const uint32_t value = (ctx->eax >> 8u) & 0xffu;",
                "ctx->flags.sf = (value & 0x80u) != 0u;",
                "ctx->flags.zf = (value & 0x40u) != 0u;",
                "ctx->flags.af = (value & 0x10u) != 0u;",
                "ctx->flags.pf = (value & 0x04u) != 0u;",
                "ctx->flags.cf = (value & 0x01u) != 0u;",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic in {
            "prefetch",
            "prefetchnta",
            "prefetcht0",
            "prefetcht1",
            "prefetcht2",
        }:
            return [f"eip = {next_eip};", "continue;"]
        if instruction.mnemonic == "emms":
            return [f"eip = {next_eip};", "continue;"]
        if instruction.mnemonic in {"movq", "movntq"}:
            return self._emit_mmx_write(
                instruction.operands[0],
                self._mmx_operand_read(instruction.operands[1]),
            ) + [f"eip = {next_eip};", "continue;"]
        if instruction.mnemonic == "fwait":
            return [f"eip = {next_eip};", "continue;"]
        if instruction.mnemonic == "fnclex":
            return [f"eip = {next_eip};", "continue;"]
        if instruction.mnemonic == "fnstsw":
            return self._emit_write(
                instruction.operands[0],
                "ctx->fpu_status_word",
            ) + [
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "wbinvd":
            return [f"eip = {next_eip};", "continue;"]
        if instruction.mnemonic == "sfence":
            return [f"eip = {next_eip};", "continue;"]
        if instruction.mnemonic == "out":
            return [
                "const uint32_t port = ctx->edx & 0xffffu;",
                "const uint8_t value = static_cast<uint8_t>(ctx->eax & 0xffu);",
                "(void)port;",
                "(void)value;",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "in":
            return [
                "const uint32_t port = ctx->edx & 0xffffu;",
                "(void)port;",
                "ctx->eax &= 0xffffff00u;",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "sgdt":
            address = self._effective_address_expr(instruction.operands[0])
            return [
                f"const uint32_t address = {address};",
                "b2r_write_u16(ctx, address, static_cast<uint16_t>(ctx->gdtr_limit));",
                "b2r_write_u32(ctx, address + 2u, ctx->gdtr_base);",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "fnstcw":
            return self._emit_write(
                instruction.operands[0],
                "ctx->fpu_control_word",
            ) + [
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "fldcw":
            return [
                "ctx->fpu_control_word = "
                f"{self._operand_read(instruction.operands[0])} & 0xffffu;",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "fnsave":
            return [
                f"b2r_fnsave(ctx, {self._effective_address_expr(instruction.operands[0])});",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "frstor":
            return [
                f"b2r_frstor(ctx, {self._effective_address_expr(instruction.operands[0])});",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "fild":
            operand = instruction.operands[0]
            if operand.size == 64:
                value = f"static_cast<float>(b2r_read_i64(ctx, {self._effective_address_expr(operand)}))"
            else:
                value = (
                    "static_cast<float>(static_cast<int32_t>("
                    f"{self._operand_read(operand)}))"
                )
            return [
                f"b2r_fpu_push(ctx, {value});",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "fistp":
            operand = instruction.operands[0]
            if operand.size == 32:
                return [
                    f"b2r_write_u32(ctx, {self._effective_address_expr(operand)}, b2r_fistp_i32(b2r_fpu_pop(ctx)));",
                    f"eip = {next_eip};",
                    "continue;",
                ]
            if operand.size != 64:
                raise X86DecodeError("cannot emit unsupported fistp size")
            return [
                f"b2r_write_i64(ctx, {self._effective_address_expr(operand)}, b2r_fistp_i64(b2r_fpu_pop(ctx)));",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "fld":
            operand = instruction.operands[0]
            return [
                f"b2r_fpu_push(ctx, {self._x87_float_operand_read(operand)});",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic in {
            "fld1",
            "fldl2t",
            "fldl2e",
            "fldpi",
            "fldlg2",
            "fldln2",
            "fldz",
        }:
            constants = {
                "fld1": "1.0f",
                "fldl2t": "3.32192809489f",
                "fldl2e": "1.44269504089f",
                "fldpi": "3.14159265359f",
                "fldlg2": "0.30102999566f",
                "fldln2": "0.69314718056f",
                "fldz": "0.0f",
            }
            return [
                f"b2r_fpu_push(ctx, {constants[instruction.mnemonic]});",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "frndint":
            return [
                "ctx->fpu_stack[0] = std::nearbyint(ctx->fpu_stack[0]);",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "fscale":
            return [
                "ctx->fpu_stack[0] = std::ldexp(ctx->fpu_stack[0], static_cast<int>(std::trunc(ctx->fpu_stack[1])));",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "f2xm1":
            return [
                "ctx->fpu_stack[0] = std::exp2(ctx->fpu_stack[0]) - 1.0f;",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic in {"fsqrt", "fsin", "fcos"}:
            operations = {
                "fsqrt": "sqrt",
                "fsin": "sin",
                "fcos": "cos",
            }
            return [
                f"ctx->fpu_stack[0] = std::{operations[instruction.mnemonic]}(ctx->fpu_stack[0]);",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "fyl2x":
            return [
                "const float x = b2r_fpu_pop(ctx);",
                "ctx->fpu_stack[0] = x > 0.0f ? ctx->fpu_stack[0] * std::log2(x) : std::numeric_limits<float>::quiet_NaN();",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "fptan":
            return [
                "ctx->fpu_stack[0] = std::tan(ctx->fpu_stack[0]);",
                "b2r_fpu_push(ctx, 1.0f);",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "fcom":
            operand = instruction.operands[0]
            return [
                f"const float compare_value = {self._x87_float_operand_read(operand)};",
                "ctx->fpu_status_word = b2r_fpu_compare_status(ctx->fpu_stack[0], compare_value, ctx->fpu_status_word);",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "fcomp":
            operand = instruction.operands[0]
            return [
                f"const float compare_value = {self._x87_float_operand_read(operand)};",
                "ctx->fpu_status_word = b2r_fpu_compare_status(ctx->fpu_stack[0], compare_value, ctx->fpu_status_word);",
                "(void)b2r_fpu_pop(ctx);",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "fcompp":
            return [
                "ctx->fpu_status_word = b2r_fpu_compare_status(ctx->fpu_stack[0], ctx->fpu_stack[1], ctx->fpu_status_word);",
                "(void)b2r_fpu_pop(ctx);",
                "(void)b2r_fpu_pop(ctx);",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "fadd":
            operand = instruction.operands[0]
            return [
                f"ctx->fpu_stack[0] += {self._x87_float_operand_read(operand)};",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "fmul":
            operand = instruction.operands[0]
            return [
                f"ctx->fpu_stack[0] *= {self._x87_float_operand_read(operand)};",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "fsub":
            operand = instruction.operands[0]
            return [
                f"ctx->fpu_stack[0] -= {self._x87_float_operand_read(operand)};",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic in {"fiadd", "fimul"}:
            operand = instruction.operands[0]
            operation = "+=" if instruction.mnemonic == "fiadd" else "*="
            return [
                f"ctx->fpu_stack[0] {operation} static_cast<float>(static_cast<int32_t>("
                f"{self._operand_read(operand)}));",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "fisub":
            operand = instruction.operands[0]
            return [
                "ctx->fpu_stack[0] -= static_cast<float>(static_cast<int32_t>("
                f"{self._operand_read(operand)}));",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "fidiv":
            operand = instruction.operands[0]
            return [
                "ctx->fpu_stack[0] /= static_cast<float>(static_cast<int32_t>("
                f"{self._operand_read(operand)}));",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "fsubr":
            operand = instruction.operands[0]
            return [
                f"ctx->fpu_stack[0] = {self._x87_float_operand_read(operand)} - ctx->fpu_stack[0];",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "fdiv":
            operand = instruction.operands[0]
            return [
                f"ctx->fpu_stack[0] /= {self._x87_float_operand_read(operand)};",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "fdivr":
            operand = instruction.operands[0]
            return [
                f"ctx->fpu_stack[0] = {self._x87_float_operand_read(operand)} / ctx->fpu_stack[0];",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic in {
            "fadd_st",
            "fmul_st",
            "fsubr_st",
            "fsub_st",
            "fdivr_st",
            "fdiv_st",
        }:
            index = self._x87_stack_index(instruction.operands[0])
            expressions = {
                "fadd_st": f"ctx->fpu_stack[{index}u] + ctx->fpu_stack[0]",
                "fmul_st": f"ctx->fpu_stack[{index}u] * ctx->fpu_stack[0]",
                "fsubr_st": f"ctx->fpu_stack[0] - ctx->fpu_stack[{index}u]",
                "fsub_st": f"ctx->fpu_stack[{index}u] - ctx->fpu_stack[0]",
                "fdivr_st": f"ctx->fpu_stack[0] / ctx->fpu_stack[{index}u]",
                "fdiv_st": f"ctx->fpu_stack[{index}u] / ctx->fpu_stack[0]",
            }
            return [
                f"ctx->fpu_stack[{index}u] = {expressions[instruction.mnemonic]};",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "fchs":
            return [
                "ctx->fpu_stack[0] = -ctx->fpu_stack[0];",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "fabs":
            return [
                "ctx->fpu_stack[0] = std::fabs(ctx->fpu_stack[0]);",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "ftst":
            return [
                "ctx->fpu_status_word = b2r_fpu_compare_status(ctx->fpu_stack[0], 0.0f, ctx->fpu_status_word);",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "fxch":
            index = self._x87_stack_index(instruction.operands[0])
            return [
                "const float value = ctx->fpu_stack[0];",
                f"ctx->fpu_stack[0] = ctx->fpu_stack[{index}u];",
                f"ctx->fpu_stack[{index}u] = value;",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "ffree":
            index = self._x87_stack_index(instruction.operands[0])
            return [
                f"b2r_fpu_free(ctx, {index}u);",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "faddp":
            index = self._x87_stack_index(instruction.operands[0])
            return [
                f"ctx->fpu_stack[{index}u] += ctx->fpu_stack[0];",
                "(void)b2r_fpu_pop(ctx);",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "fmulp":
            index = self._x87_stack_index(instruction.operands[0])
            return [
                f"ctx->fpu_stack[{index}u] *= ctx->fpu_stack[0];",
                "(void)b2r_fpu_pop(ctx);",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "fsubp":
            index = self._x87_stack_index(instruction.operands[0])
            return [
                f"ctx->fpu_stack[{index}u] -= ctx->fpu_stack[0];",
                "(void)b2r_fpu_pop(ctx);",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "fsubrp":
            index = self._x87_stack_index(instruction.operands[0])
            return [
                f"ctx->fpu_stack[{index}u] = ctx->fpu_stack[0] - ctx->fpu_stack[{index}u];",
                "(void)b2r_fpu_pop(ctx);",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "fdivrp":
            index = self._x87_stack_index(instruction.operands[0])
            return [
                f"ctx->fpu_stack[{index}u] = ctx->fpu_stack[0] / ctx->fpu_stack[{index}u];",
                "(void)b2r_fpu_pop(ctx);",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "fdivp":
            index = self._x87_stack_index(instruction.operands[0])
            return [
                f"ctx->fpu_stack[{index}u] /= ctx->fpu_stack[0];",
                "(void)b2r_fpu_pop(ctx);",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "fst":
            operand = instruction.operands[0]
            if operand.kind == "imm":
                index = self._x87_stack_index(operand)
                return [
                    f"ctx->fpu_stack[{index}u] = ctx->fpu_stack[0];",
                    f"eip = {next_eip};",
                    "continue;",
                ]
            helper = "b2r_write_f64" if operand.size == 64 else "b2r_write_f32"
            return [
                f"{helper}(ctx, {self._effective_address_expr(operand)}, ctx->fpu_stack[0]);",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "fstp":
            operand = instruction.operands[0]
            if operand.kind == "imm":
                index = self._x87_stack_index(operand)
                return [
                    f"ctx->fpu_stack[{index}u] = ctx->fpu_stack[0];",
                    "(void)b2r_fpu_pop(ctx);",
                    f"eip = {next_eip};",
                    "continue;",
                ]
            helper = "b2r_write_f64" if operand.size == 64 else "b2r_write_f32"
            return [
                f"{helper}(ctx, {self._effective_address_expr(operand)}, b2r_fpu_pop(ctx));",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "stmxcsr":
            return self._emit_write(instruction.operands[0], "ctx->mxcsr") + [
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "ldmxcsr":
            return [
                f"ctx->mxcsr = {self._operand_read(instruction.operands[0])};",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic in {"cvttss2si", "cvtss2si"}:
            helper = (
                "b2r_cvttss2si"
                if instruction.mnemonic == "cvttss2si"
                else "b2r_cvtss2si"
            )
            arguments = self._xmm_scalar_operand_read(instruction.operands[1])
            if instruction.mnemonic == "cvtss2si":
                arguments += ", ctx->mxcsr"
            return self._emit_write(
                instruction.operands[0],
                f"{helper}({arguments})",
            ) + [f"eip = {next_eip};", "continue;"]
        if instruction.mnemonic == "movss":
            destination = instruction.operands[0]
            source = instruction.operands[1]
            value = self._xmm_scalar_operand_read(source)
            if destination.kind == "reg":
                index = self._xmm_register_index(destination)
                lines = [f"ctx->xmm[{index}u].lane[0] = {value};"]
                if source.kind == "mem":
                    lines.extend(
                        [
                            f"ctx->xmm[{index}u].lane[1] = 0.0f;",
                            f"ctx->xmm[{index}u].lane[2] = 0.0f;",
                            f"ctx->xmm[{index}u].lane[3] = 0.0f;",
                        ]
                    )
                return lines + [f"eip = {next_eip};", "continue;"]
            return [
                f"b2r_write_f32(ctx, {self._effective_address_expr(destination)}, {value});",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "rsqrtss":
            destination = instruction.operands[0]
            return [
                f"ctx->xmm[{self._xmm_register_index(destination)}u].lane[0] = b2r_rsqrtss({self._xmm_scalar_operand_read(instruction.operands[1])});",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic in {"movaps", "movups"}:
            return self._emit_xmm_write(
                instruction.operands[0],
                self._xmm_operand_read(instruction.operands[1]),
            ) + [f"eip = {next_eip};", "continue;"]
        if instruction.mnemonic == "sqrtps":
            return self._emit_xmm_write(
                instruction.operands[0],
                f"b2r_xmm_sqrt({self._xmm_operand_read(instruction.operands[1])})",
            ) + [f"eip = {next_eip};", "continue;"]
        if instruction.mnemonic in {"addps", "mulps", "minps", "maxps"}:
            helper = {
                "addps": "b2r_xmm_add",
                "mulps": "b2r_xmm_multiply",
                "minps": "b2r_xmm_minimum",
                "maxps": "b2r_xmm_maximum",
            }[instruction.mnemonic]
            destination = instruction.operands[0]
            return self._emit_xmm_write(
                destination,
                f"{helper}({self._xmm_operand_read(destination)}, {self._xmm_operand_read(instruction.operands[1])})",
            ) + [f"eip = {next_eip};", "continue;"]
        if instruction.mnemonic == "shufps":
            immediate = instruction.operands[2].immediate or 0
            destination = instruction.operands[0]
            return self._emit_xmm_write(
                destination,
                (
                    "b2r_xmm_shuffle("
                    f"{self._xmm_operand_read(destination)}, "
                    f"{self._xmm_operand_read(instruction.operands[1])}, "
                    f"{_cpp_u32(immediate)})"
                ),
            ) + [f"eip = {next_eip};", "continue;"]
        if instruction.mnemonic in {"unpcklps", "unpckhps", "movlhps", "movhlps"}:
            helper = {
                "unpcklps": "b2r_xmm_unpack_low",
                "unpckhps": "b2r_xmm_unpack_high",
                "movlhps": "b2r_xmm_move_low_to_high",
                "movhlps": "b2r_xmm_move_high_to_low",
            }[instruction.mnemonic]
            destination = instruction.operands[0]
            return self._emit_xmm_write(
                destination,
                f"{helper}({self._xmm_operand_read(destination)}, {self._xmm_operand_read(instruction.operands[1])})",
            ) + [f"eip = {next_eip};", "continue;"]
        if instruction.mnemonic in {"movlps_store", "movhps_store"}:
            source = self._xmm_operand_read(instruction.operands[1])
            lane = 0 if instruction.mnemonic == "movlps_store" else 2
            address = self._effective_address_expr(instruction.operands[0])
            return [
                f"b2r_write_f32(ctx, {address}, {source}.lane[{lane}u]);",
                f"b2r_write_f32(ctx, {address} + 4u, {source}.lane[{lane + 1}u]);",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "mov":
            return self._emit_write(
                instruction.operands[0],
                self._operand_read(instruction.operands[1]),
            ) + [f"eip = {next_eip};", "continue;"]
        if instruction.mnemonic == "xchg":
            left = instruction.operands[0]
            right = instruction.operands[1]
            lines = [
                f"const uint32_t left = {self._operand_read(left)};",
                f"const uint32_t right = {self._operand_read(right)};",
            ]
            lines.extend(self._emit_write(left, "right"))
            lines.extend(self._emit_write(right, "left"))
            lines.extend([f"eip = {next_eip};", "continue;"])
            return lines
        if instruction.mnemonic == "xadd":
            destination = instruction.operands[0]
            source = instruction.operands[1]
            bits = destination.size
            lines = [
                f"const uint32_t left = {self._operand_read(destination)};",
                f"const uint32_t right = {self._operand_read(source)};",
                "const uint32_t result = left + right;",
            ]
            lines.extend(self._emit_write(destination, "result"))
            lines.extend(self._emit_write(source, "left"))
            lines.extend(
                [
                    f"b2r_add_flags(ctx, left, right, result, {bits}u);",
                    f"eip = {next_eip};",
                    "continue;",
                ]
            )
            return lines
        if instruction.mnemonic == "movzx":
            return self._emit_write(
                instruction.operands[0],
                self._operand_read(instruction.operands[1]),
            ) + [f"eip = {next_eip};", "continue;"]
        if instruction.mnemonic == "movsx":
            source = instruction.operands[1]
            return self._emit_write(
                instruction.operands[0],
                f"b2r_sign_extend({self._operand_read(source)}, {source.size}u)",
            ) + [f"eip = {next_eip};", "continue;"]
        if instruction.mnemonic == "setcc":
            if instruction.condition is None:
                raise X86DecodeError("setcc missing condition")
            return self._emit_write(
                instruction.operands[0],
                f"({self._condition_expr(instruction.condition)} ? 1u : 0u)",
            ) + [f"eip = {next_eip};", "continue;"]
        if instruction.mnemonic == "lea":
            return self._emit_write(
                instruction.operands[0],
                self._effective_address_expr(instruction.operands[1]),
            ) + [f"eip = {next_eip};", "continue;"]
        if instruction.mnemonic == "bsf":
            bits = f"{instruction.operands[1].size}u"
            lines = [
                f"const uint32_t value = {self._operand_read(instruction.operands[1])} & b2r_mask_for_bits({bits});",
                "if (value == 0u) {",
                "    ctx->flags.zf = true;",
                "} else {",
                "    uint32_t index = 0u;",
                "    uint32_t cursor = value;",
                "    while ((cursor & 1u) == 0u) {",
                "        cursor >>= 1u;",
                "        ++index;",
                "    }",
            ]
            lines.extend(
                f"    {line}"
                for line in self._emit_write(instruction.operands[0], "index")
            )
            lines.extend(
                [
                    "    ctx->flags.zf = false;",
                    "}",
                    f"eip = {next_eip};",
                    "continue;",
                ]
            )
            return lines
        if instruction.mnemonic in {"add", "adc", "sub", "sbb", "xor", "and", "or"}:
            op = { "add": "+", "adc": "+", "sub": "-", "sbb": "-", "xor": "^", "and": "&", "or": "|" }[
                instruction.mnemonic
            ]
            lines = [
                f"const uint32_t lhs = {self._operand_read(instruction.operands[0])};",
                f"const uint32_t rhs = {self._operand_read(instruction.operands[1])};",
            ]
            if instruction.mnemonic == "adc":
                lines.extend(
                    [
                        "const uint32_t carry = ctx->flags.cf ? 1u : 0u;",
                        "const uint64_t rhs_with_carry = static_cast<uint64_t>(rhs) + carry;",
                        "const uint32_t result = lhs + static_cast<uint32_t>(rhs_with_carry);",
                    ]
                )
            elif instruction.mnemonic == "sbb":
                lines.extend(
                    [
                        "const uint32_t borrow = ctx->flags.cf ? 1u : 0u;",
                        "const uint64_t rhs_with_borrow = static_cast<uint64_t>(rhs) + borrow;",
                        "const uint32_t result = lhs - static_cast<uint32_t>(rhs_with_borrow);",
                    ]
                )
            else:
                lines.append(f"const uint32_t result = lhs {op} rhs;")
            lines.extend(self._emit_write(instruction.operands[0], "result"))
            bits = f"{instruction.operands[0].size}u"
            if instruction.mnemonic == "add":
                lines.append(f"b2r_add_flags(ctx, lhs, rhs, result, {bits});")
            elif instruction.mnemonic == "adc":
                lines.append(
                    f"b2r_add_flags(ctx, lhs, rhs_with_carry, result, {bits});"
                )
            elif instruction.mnemonic == "sub":
                lines.append(f"b2r_sub_flags(ctx, lhs, rhs, result, {bits});")
            elif instruction.mnemonic == "sbb":
                lines.append(
                    f"b2r_sub_flags(ctx, lhs, rhs_with_borrow, result, {bits});"
                )
            else:
                lines.append(f"b2r_logic_flags(ctx, result, {bits});")
            lines.extend([f"eip = {next_eip};", "continue;"])
            return lines
        if instruction.mnemonic == "imul":
            if len(instruction.operands) == 1:
                operand = instruction.operands[0]
                if operand.size == 32:
                    return [
                        "const int64_t lhs = static_cast<int32_t>(ctx->eax);",
                        f"const int64_t rhs = static_cast<int32_t>({self._operand_read(operand)});",
                        "const int64_t product = lhs * rhs;",
                        "const uint64_t result = static_cast<uint64_t>(product);",
                        "ctx->eax = static_cast<uint32_t>(result);",
                        "ctx->edx = static_cast<uint32_t>(result >> 32);",
                        "const bool overflow = product != static_cast<int64_t>(static_cast<int32_t>(ctx->eax));",
                        "ctx->flags.cf = overflow;",
                        "ctx->flags.of = overflow;",
                        f"eip = {next_eip};",
                        "continue;",
                    ]
                if operand.size != 8:
                    raise X86DecodeError("unsupported one-operand imul size")
                return [
                    "const int32_t lhs = static_cast<int8_t>(ctx->eax & 0xffu);",
                    f"const int32_t rhs = static_cast<int8_t>({self._operand_read(operand)} & 0xffu);",
                    "const int32_t product = lhs * rhs;",
                    "const uint32_t result = static_cast<uint32_t>(product) & 0xffffu;",
                    "ctx->eax = (ctx->eax & 0xffff0000u) | result;",
                    "const bool overflow = product != static_cast<int8_t>(result & 0xffu);",
                    "ctx->flags.cf = overflow;",
                    "ctx->flags.of = overflow;",
                    f"eip = {next_eip};",
                    "continue;",
                ]
            bits = f"{instruction.operands[0].size}u"
            lhs_operand = (
                instruction.operands[1]
                if len(instruction.operands) == 3
                else instruction.operands[0]
            )
            rhs_operand = (
                instruction.operands[2]
                if len(instruction.operands) == 3
                else instruction.operands[1]
            )
            lines = [
                f"const uint32_t lhs_raw = {self._operand_read(lhs_operand)};",
                f"const uint32_t rhs_raw = {self._operand_read(rhs_operand)};",
                f"const int64_t lhs = b2r_signed_value(lhs_raw, {lhs_operand.size}u);",
                f"const int64_t rhs = b2r_signed_value(rhs_raw, {rhs_operand.size}u);",
                "const int64_t product = lhs * rhs;",
                f"const uint32_t result = static_cast<uint32_t>(product) & b2r_mask_for_bits({bits});",
            ]
            lines.extend(self._emit_write(instruction.operands[0], "result"))
            lines.extend(
                [
                    f"const bool overflow = product != b2r_signed_value(result, {bits});",
                    "ctx->flags.cf = overflow;",
                    "ctx->flags.of = overflow;",
                    f"eip = {next_eip};",
                    "continue;",
                ]
            )
            return lines
        if instruction.mnemonic in {"cmp", "test"}:
            op = "-" if instruction.mnemonic == "cmp" else "&"
            bits = f"{instruction.operands[0].size}u"
            lines = [
                f"const uint32_t lhs = {self._operand_read(instruction.operands[0])};",
                f"const uint32_t rhs = {self._operand_read(instruction.operands[1])};",
                f"const uint32_t result = lhs {op} rhs;",
                f"b2r_sub_flags(ctx, lhs, rhs, result, {bits});"
                if instruction.mnemonic == "cmp"
                else f"b2r_logic_flags(ctx, result, {bits});",
                f"eip = {next_eip};",
                "continue;",
            ]
            return lines
        if instruction.mnemonic in {"inc", "dec"}:
            op = "+" if instruction.mnemonic == "inc" else "-"
            flag_helper = "b2r_add_flags" if instruction.mnemonic == "inc" else "b2r_sub_flags"
            bits = f"{instruction.operands[0].size}u"
            lines = [
                "const bool old_cf = ctx->flags.cf;",
                f"const uint32_t lhs = {self._operand_read(instruction.operands[0])};",
                "const uint32_t rhs = 1u;",
                f"const uint32_t result = lhs {op} rhs;",
            ]
            lines.extend(self._emit_write(instruction.operands[0], "result"))
            lines.extend(
                [
                    f"{flag_helper}(ctx, lhs, rhs, result, {bits});",
                    "ctx->flags.cf = old_cf;",
                    f"eip = {next_eip};",
                    "continue;",
                ]
            )
            return lines
        if instruction.mnemonic == "neg":
            bits = f"{instruction.operands[0].size}u"
            lines = [
                f"const uint32_t lhs = {self._operand_read(instruction.operands[0])};",
                "const uint32_t result = 0u - lhs;",
            ]
            lines.extend(self._emit_write(instruction.operands[0], "result"))
            lines.extend(
                [
                    f"b2r_sub_flags(ctx, 0u, lhs, result, {bits});",
                    f"eip = {next_eip};",
                    "continue;",
                ]
            )
            return lines
        if instruction.mnemonic == "not":
            return self._emit_write(
                instruction.operands[0],
                f"~({self._operand_read(instruction.operands[0])})",
            ) + [f"eip = {next_eip};", "continue;"]
        if instruction.mnemonic in {"shl", "shr", "sar"}:
            op = "<<" if instruction.mnemonic == "shl" else ">>"
            lhs_expr = self._operand_read(instruction.operands[0])
            count_expr = self._operand_read(instruction.operands[1])
            if instruction.mnemonic == "sar":
                result_expr = (
                    f"static_cast<uint32_t>(static_cast<int32_t>(lhs) {op} count)"
                )
            else:
                result_expr = f"lhs {op} count"
            lines = [
                f"const uint32_t lhs = {lhs_expr};",
                f"const uint32_t count = ({count_expr}) & 0x1fu;",
                f"const uint32_t result = {result_expr};",
            ]
            lines.extend(self._emit_write(instruction.operands[0], "result"))
            lines.extend(
                [
                    f"b2r_logic_flags(ctx, result, {instruction.operands[0].size}u);",
                    f"eip = {next_eip};",
                    "continue;",
                ]
            )
            return lines
        if instruction.mnemonic in {"ror", "rcr"}:
            operand = instruction.operands[0]
            bits = operand.size
            width = bits + (1 if instruction.mnemonic == "rcr" else 0)
            raw_count = self._operand_read(instruction.operands[1])
            lines = [
                f"const uint32_t bits = {bits}u;",
                "const uint32_t mask = b2r_mask_for_bits(bits);",
                f"const uint32_t lhs = {self._operand_read(operand)} & mask;",
                f"const uint32_t masked_count = ({raw_count}) & 0x1fu;",
                f"const uint32_t count = masked_count % {width}u;",
                "uint32_t result = lhs;",
                "bool carry = ctx->flags.cf;",
                "if (count != 0u) {",
            ]
            if instruction.mnemonic == "ror":
                lines.extend(
                    [
                        "    result = ((lhs >> count) | (lhs << (bits - count))) & mask;",
                        "    carry = (result & (1u << (bits - 1u))) != 0u;",
                    ]
                )
            else:
                lines.extend(
                    [
                        "    const uint32_t width = bits + 1u;",
                        "    const uint64_t ring_mask = (1ull << width) - 1ull;",
                        "    const uint64_t ring = (static_cast<uint64_t>(ctx->flags.cf) << bits) | lhs;",
                        "    const uint64_t rotated = ((ring >> count) | (ring << (width - count))) & ring_mask;",
                        "    result = static_cast<uint32_t>(rotated) & mask;",
                        "    carry = ((rotated >> bits) & 1ull) != 0ull;",
                    ]
                )
            lines.extend(
                [
                    "    ctx->flags.cf = carry;",
                    "    if (count == 1u) {",
                    "        ctx->flags.of = (((result >> (bits - 1u)) ^ (result >> (bits - 2u))) & 1u) != 0u;",
                    "    }",
                    "}",
                ]
            )
            lines.extend(self._emit_write(operand, "result"))
            lines.extend([f"eip = {next_eip};", "continue;"])
            return lines
        if instruction.mnemonic in {"shld", "shrd"}:
            destination_expr = self._operand_read(instruction.operands[0])
            source_expr = self._operand_read(instruction.operands[1])
            count_expr = self._operand_read(instruction.operands[2])
            if instruction.mnemonic == "shld":
                result_expr = "(lhs << count) | (source >> (32u - count))"
                carry_expr = "(lhs & (1u << (32u - count))) != 0u"
                overflow_expr = "(((result >> 31u) & 1u) != static_cast<uint32_t>(ctx->flags.cf))"
            else:
                result_expr = "(lhs >> count) | (source << (32u - count))"
                carry_expr = "(lhs & (1u << (count - 1u))) != 0u"
                overflow_expr = "(((lhs ^ result) & 0x80000000u) != 0u)"
            lines = [
                f"const uint32_t lhs = {destination_expr};",
                f"const uint32_t source = {source_expr};",
                f"const uint32_t count = ({count_expr}) & 0x1fu;",
                f"const uint32_t result = count == 0u ? lhs : ({result_expr});",
            ]
            lines.extend(self._emit_write(instruction.operands[0], "result"))
            lines.extend(
                [
                    "if (count != 0u) {",
                    "    b2r_logic_flags(ctx, result, 32u);",
                    f"    ctx->flags.cf = {carry_expr};",
                    f"    ctx->flags.of = count == 1u ? {overflow_expr} : false;",
                    "}",
                    f"eip = {next_eip};",
                    "continue;",
                ]
            )
            return lines
        if instruction.mnemonic == "push":
            return [
                f"b2r_push(ctx, {self._operand_read(instruction.operands[0])});",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "pop":
            return self._emit_write(instruction.operands[0], "b2r_pop(ctx)") + [
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "leave":
            return [
                "ctx->esp = ctx->ebp;",
                "ctx->ebp = b2r_pop(ctx);",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "cdq":
            return [
                "ctx->edx = (ctx->eax & 0x80000000u) != 0u ? 0xffffffffu : 0u;",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "rdtsc":
            return [
                "const uint64_t value = ctx->timestamp_counter;",
                "ctx->eax = static_cast<uint32_t>(value & 0xffffffffu);",
                "ctx->edx = static_cast<uint32_t>(value >> 32);",
                f"ctx->timestamp_counter = value + {DETERMINISTIC_TSC_STEP}ull;",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic in {"stosb", "rep_stosb"}:
            count_expr = "ctx->ecx" if instruction.mnemonic == "rep_stosb" else "1u"
            lines = [
                f"uint32_t remaining = {count_expr};",
                "const uint32_t step = ctx->flags.df ? 0xffffffffu : 1u;",
                "const uint8_t value = static_cast<uint8_t>(ctx->eax & 0xffu);",
                *(
                    ["uint32_t chunk_iterations = 0u;"]
                    if instruction.mnemonic == "rep_stosb" and self._resumable
                    else []
                ),
                "while (remaining != 0u) {",
                "    ctx->write_u8(ctx->user, ctx->edi, value);",
                "    ctx->edi += step;",
                "    --remaining;",
                "}",
            ]
            if instruction.mnemonic == "rep_stosb":
                lines.insert(-1, "    --ctx->ecx;")
                lines[-1:-1] = self._repeat_checkpoint_lines(instruction)
            lines.extend([f"eip = {next_eip};", "continue;"])
            return lines
        if instruction.mnemonic in {"stosd", "rep_stosd"}:
            count_expr = "ctx->ecx" if instruction.mnemonic == "rep_stosd" else "1u"
            lines = [
                f"uint32_t remaining = {count_expr};",
                "const uint32_t step = ctx->flags.df ? 0xfffffffcu : 4u;",
                *(
                    ["uint32_t chunk_iterations = 0u;"]
                    if instruction.mnemonic == "rep_stosd" and self._resumable
                    else []
                ),
                "while (remaining != 0u) {",
                "    ctx->write_u32(ctx->user, ctx->edi, ctx->eax);",
                "    ctx->edi += step;",
                "    --remaining;",
                "}",
            ]
            if instruction.mnemonic == "rep_stosd":
                lines.insert(-1, "    --ctx->ecx;")
                lines[-1:-1] = self._repeat_checkpoint_lines(instruction)
            lines.extend([f"eip = {next_eip};", "continue;"])
            return lines
        if instruction.mnemonic in {"movsb", "rep_movsb"}:
            count_expr = "ctx->ecx" if instruction.mnemonic == "rep_movsb" else "1u"
            lines = [
                f"uint32_t remaining = {count_expr};",
                "const uint32_t step = ctx->flags.df ? 0xffffffffu : 1u;",
                *(
                    ["uint32_t chunk_iterations = 0u;"]
                    if instruction.mnemonic == "rep_movsb" and self._resumable
                    else []
                ),
                "while (remaining != 0u) {",
                "    const uint8_t value = ctx->read_u8(ctx->user, ctx->esi);",
                "    ctx->write_u8(ctx->user, ctx->edi, value);",
                "    ctx->esi += step;",
                "    ctx->edi += step;",
                "    --remaining;",
                "}",
            ]
            if instruction.mnemonic == "rep_movsb":
                lines.insert(-1, "    --ctx->ecx;")
                lines[-1:-1] = self._repeat_checkpoint_lines(instruction)
            lines.extend([f"eip = {next_eip};", "continue;"])
            return lines
        if instruction.mnemonic in {"movsd", "rep_movsd"}:
            count_expr = "ctx->ecx" if instruction.mnemonic == "rep_movsd" else "1u"
            lines = [
                f"uint32_t remaining = {count_expr};",
                "const uint32_t step = ctx->flags.df ? 0xfffffffcu : 4u;",
                *(
                    ["uint32_t chunk_iterations = 0u;"]
                    if instruction.mnemonic == "rep_movsd" and self._resumable
                    else []
                ),
                "while (remaining != 0u) {",
                "    const uint32_t value = ctx->read_u32(ctx->user, ctx->esi);",
                "    ctx->write_u32(ctx->user, ctx->edi, value);",
                "    ctx->esi += step;",
                "    ctx->edi += step;",
                "    --remaining;",
                "}",
            ]
            if instruction.mnemonic == "rep_movsd":
                lines.insert(-1, "    --ctx->ecx;")
                lines[-1:-1] = self._repeat_checkpoint_lines(instruction)
            lines.extend([f"eip = {next_eip};", "continue;"])
            return lines
        if instruction.mnemonic in {"cmpsb", "rep_cmpsb", "cmpsd", "rep_cmpsd"}:
            repeated = instruction.mnemonic in {"rep_cmpsb", "rep_cmpsd"}
            dword = instruction.mnemonic in {"cmpsd", "rep_cmpsd"}
            count_expr = "ctx->ecx" if repeated else "1u"
            read_method = "read_u32" if dword else "read_u8"
            pointer_increment = "4u" if dword else "1u"
            result_mask = "0xffffffffu" if dword else "0xffu"
            operand_bits = "32u" if dword else "8u"
            lines = [
                f"const uint32_t requested_count = {count_expr};",
                "uint32_t remaining = requested_count;",
                *(
                    ["uint32_t chunk_iterations = 0u;"]
                    if repeated and self._resumable
                    else []
                ),
                "while (remaining != 0u) {",
                f"    const uint32_t lhs = ctx->{read_method}(ctx->user, ctx->esi);",
                f"    const uint32_t rhs = ctx->{read_method}(ctx->user, ctx->edi);",
                f"    const uint32_t result = (lhs - rhs) & {result_mask};",
                f"    b2r_sub_flags(ctx, lhs, rhs, result, {operand_bits});",
                "    if (ctx->flags.df) {",
                f"        ctx->esi -= {pointer_increment};",
                f"        ctx->edi -= {pointer_increment};",
                "    } else {",
                f"        ctx->esi += {pointer_increment};",
                f"        ctx->edi += {pointer_increment};",
                "    }",
                "    --remaining;",
            ]
            if repeated:
                lines.append("    --ctx->ecx;")
                lines.extend(
                    [
                        "    if (!ctx->flags.zf) {",
                        "        break;",
                        "    }",
                    ]
                )
                lines.extend(self._repeat_checkpoint_lines(instruction))
            else:
                lines.append("    break;")
            lines.extend(
                [
                    "}",
                ]
            )
            lines.extend([f"eip = {next_eip};", "continue;"])
            return lines
        if instruction.mnemonic in {"scasb", "repne_scasb"}:
            count_expr = "ctx->ecx" if instruction.mnemonic == "repne_scasb" else "1u"
            lines = [
                f"const uint32_t requested_count = {count_expr};",
                "const uint32_t needle = ctx->eax & 0xffu;",
                "const uint32_t step = ctx->flags.df ? 0xffffffffu : 1u;",
                "uint32_t remaining = requested_count;",
                *(
                    ["uint32_t chunk_iterations = 0u;"]
                    if instruction.mnemonic == "repne_scasb" and self._resumable
                    else []
                ),
                "while (remaining != 0u) {",
                "    const uint32_t value = ctx->read_u8(ctx->user, ctx->edi);",
                "    const uint32_t result = (needle - value) & 0xffu;",
                "    b2r_sub_flags(ctx, needle, value, result, 8u);",
                "    ctx->edi += step;",
                "    --remaining;",
            ]
            if instruction.mnemonic == "repne_scasb":
                lines.append("    --ctx->ecx;")
                lines.extend(
                    [
                        "    if (ctx->flags.zf) {",
                        "        break;",
                        "    }",
                    ]
                )
                lines.extend(self._repeat_checkpoint_lines(instruction))
            else:
                lines.append("    break;")
            lines.extend(["}"])
            lines.extend([f"eip = {next_eip};", "continue;"])
            return lines
        if instruction.mnemonic == "int3":
            return [
                "ctx->module_exit_reason = B2R_MODULE_EXIT_SOFTWARE_INTERRUPT;",
                f"return {_cpp_u32(instruction.address)};",
            ]
        if instruction.mnemonic == "int":
            return [
                "ctx->module_exit_reason = B2R_MODULE_EXIT_SOFTWARE_INTERRUPT;",
                f"return {_cpp_u32(instruction.address)};",
            ]
        if instruction.mnemonic == "idiv":
            return [
                "const int64_t high = static_cast<int64_t>(static_cast<int32_t>(ctx->edx));",
                "const int64_t dividend = (high * 0x100000000ll) + ctx->eax;",
                f"const int32_t divisor = static_cast<int32_t>({self._operand_read(instruction.operands[0])});",
                f"if (divisor == 0) {{ ctx->fault_code = 1u; ctx->fault_eip = {_cpp_u32(instruction.address)}; ctx->eip = ctx->fault_eip; ctx->module_exit_reason = B2R_MODULE_EXIT_FAULT; return ctx->fault_eip; }}",
                f"if (divisor == -1 && dividend == std::numeric_limits<int64_t>::min()) {{ ctx->fault_code = 2u; ctx->fault_eip = {_cpp_u32(instruction.address)}; ctx->eip = ctx->fault_eip; ctx->module_exit_reason = B2R_MODULE_EXIT_FAULT; return ctx->fault_eip; }}",
                "const int64_t quotient = dividend / divisor;",
                f"if (quotient < std::numeric_limits<int32_t>::min() || quotient > std::numeric_limits<int32_t>::max()) {{ ctx->fault_code = 2u; ctx->fault_eip = {_cpp_u32(instruction.address)}; ctx->eip = ctx->fault_eip; ctx->module_exit_reason = B2R_MODULE_EXIT_FAULT; return ctx->fault_eip; }}",
                "const int64_t remainder = dividend - (quotient * divisor);",
                "ctx->eax = static_cast<uint32_t>(quotient);",
                "ctx->edx = static_cast<uint32_t>(remainder);",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "div":
            return [
                "const uint64_t dividend = (static_cast<uint64_t>(ctx->edx) << 32) | ctx->eax;",
                f"const uint32_t divisor = {self._operand_read(instruction.operands[0])};",
                f"if (divisor == 0u) {{ ctx->fault_code = 1u; ctx->fault_eip = {_cpp_u32(instruction.address)}; ctx->eip = ctx->fault_eip; ctx->module_exit_reason = B2R_MODULE_EXIT_FAULT; return ctx->fault_eip; }}",
                "const uint64_t quotient = dividend / divisor;",
                f"if (quotient > std::numeric_limits<uint32_t>::max()) {{ ctx->fault_code = 2u; ctx->fault_eip = {_cpp_u32(instruction.address)}; ctx->eip = ctx->fault_eip; ctx->module_exit_reason = B2R_MODULE_EXIT_FAULT; return ctx->fault_eip; }}",
                "const uint64_t remainder = dividend - (quotient * divisor);",
                "ctx->eax = static_cast<uint32_t>(quotient);",
                "ctx->edx = static_cast<uint32_t>(remainder);",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "mul":
            return [
                f"const uint64_t product = static_cast<uint64_t>(ctx->eax) * static_cast<uint64_t>({self._operand_read(instruction.operands[0])});",
                "ctx->eax = static_cast<uint32_t>(product & 0xffffffffu);",
                "ctx->edx = static_cast<uint32_t>(product >> 32);",
                "ctx->flags.cf = ctx->flags.of = ctx->edx != 0u;",
                f"eip = {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "call":
            target = (
                _cpp_u32(instruction.target)
                if instruction.target is not None
                else self._operand_read(instruction.operands[0])
            )
            target_setup = (
                []
                if instruction.target is not None
                else [f"const uint32_t call_target = {target};"]
            )
            target_expr = target if instruction.target is not None else "call_target"
            if self._resumable:
                exit_reason = (
                    "B2R_MODULE_EXIT_CALLBACK"
                    if instruction.target in self._callback_addresses
                    else "B2R_MODULE_EXIT_CALL"
                )
                return [
                    *target_setup,
                    f"b2r_push(ctx, {next_eip});",
                    f"pending_module_exit_reason = {exit_reason};",
                    f"eip = {target_expr};",
                    "continue;",
                ]
            return [
                *target_setup,
                f"b2r_push(ctx, {next_eip});",
                f"ctx->call(ctx->user, {target_expr}, ctx);",
                "const uint32_t return_eip = b2r_pop(ctx);",
                "eip = return_eip;",
                "continue;",
            ]
        if instruction.mnemonic == "jmp":
            target = (
                _cpp_u32(instruction.target)
                if instruction.target is not None
                else self._operand_read(instruction.operands[0])
            )
            exit_reason = (
                "B2R_MODULE_EXIT_CALLBACK"
                if instruction.target in self._callback_addresses
                else "B2R_MODULE_EXIT_BRANCH"
            )
            return [
                f"pending_module_exit_reason = {exit_reason};",
                f"eip = {target};",
                "continue;",
            ]
        if instruction.mnemonic == "jcc":
            if instruction.target is None or instruction.condition is None:
                raise X86DecodeError("conditional branch missing target")
            condition = self._condition_expr(instruction.condition)
            exit_reason = (
                "B2R_MODULE_EXIT_CALLBACK"
                if instruction.target in self._callback_addresses
                else "B2R_MODULE_EXIT_BRANCH"
            )
            return [
                f"if ({condition}) {{",
                f"    pending_module_exit_reason = {exit_reason};",
                f"    eip = {_cpp_u32(instruction.target)};",
                "} else {",
                f"    eip = {next_eip};",
                "}",
                "continue;",
            ]
        if instruction.mnemonic == "jmp_far":
            if instruction.target is None:
                raise X86DecodeError("far jump missing target")
            selector = instruction.operands[0].immediate or 0
            exit_reason = (
                "B2R_MODULE_EXIT_CALLBACK"
                if instruction.target in self._callback_addresses
                else "B2R_MODULE_EXIT_BRANCH"
            )
            return [
                f"ctx->cs_selector = {_cpp_u32(selector & 0xFFFF)};",
                f"pending_module_exit_reason = {exit_reason};",
                f"eip = {_cpp_u32(instruction.target)};",
                "continue;",
            ]
        if instruction.mnemonic == "ret":
            lines = ["const uint32_t return_address = b2r_pop(ctx);"]
            if instruction.ret_stack_adjust:
                lines.append(f"ctx->esp += {_cpp_u32(instruction.ret_stack_adjust)};")
            if self._resumable:
                lines.extend(
                    [
                        "ctx->eip = return_address;",
                        "pending_module_exit_reason = B2R_MODULE_EXIT_RETURN;",
                        "eip = return_address;",
                        "continue;",
                    ]
                )
            else:
                lines.append("return return_address;")
            return lines
        raise X86DecodeError(f"cannot emit C++ for {instruction.mnemonic}")

    def _emit_write(self, operand: Operand, value_expr: str) -> list[str]:
        if operand.kind == "reg":
            if operand.size == 8:
                parent, shift = self._byte_register_parent(operand.reg or "")
                mask = "0xffffff00u" if shift == 0 else "0xffff00ffu"
                shifted_value = (
                    f"(({value_expr}) & 0xffu)"
                    if shift == 0
                    else f"((({value_expr}) & 0xffu) << {shift})"
                )
                return [f"ctx->{parent} = (ctx->{parent} & {mask}) | {shifted_value};"]
            if operand.size == 16:
                return [
                    f"ctx->{operand.reg} = (ctx->{operand.reg} & 0xffff0000u) | (({value_expr}) & 0xffffu);"
                ]
            return [f"ctx->{operand.reg} = {value_expr};"]
        if operand.kind == "mem":
            if operand.size == 8:
                return [
                    f"ctx->write_u8(ctx->user, {self._effective_address_expr(operand)}, static_cast<uint8_t>({value_expr}));"
                ]
            if operand.size == 16:
                return [
                    f"b2r_write_u16(ctx, {self._effective_address_expr(operand)}, static_cast<uint16_t>({value_expr}));"
                ]
            return [
                f"ctx->write_u32(ctx->user, {self._effective_address_expr(operand)}, {value_expr});"
            ]
        raise X86DecodeError(f"cannot emit write for {operand.kind}")

    def _operand_read(self, operand: Operand) -> str:
        if operand.kind == "reg":
            if operand.size == 8:
                parent, shift = self._byte_register_parent(operand.reg or "")
                if shift == 0:
                    return f"(ctx->{parent} & 0xffu)"
                return f"((ctx->{parent} >> {shift}) & 0xffu)"
            if operand.size == 16:
                return f"(ctx->{operand.reg} & 0xffffu)"
            return f"ctx->{operand.reg}"
        if operand.kind == "imm":
            return _cpp_u32(operand.immediate or 0)
        if operand.kind == "mem":
            if operand.size == 8:
                return f"ctx->read_u8(ctx->user, {self._effective_address_expr(operand)})"
            if operand.size == 16:
                return f"b2r_read_u16(ctx, {self._effective_address_expr(operand)})"
            value = f"ctx->read_u32(ctx->user, {self._effective_address_expr(operand)})"
            return value
        raise X86DecodeError(f"cannot emit read for {operand.kind}")

    def _x87_float_operand_read(self, operand: Operand) -> str:
        if operand.kind == "imm":
            return f"ctx->fpu_stack[{self._x87_stack_index(operand)}u]"
        if operand.kind == "mem":
            helper = {
                32: "b2r_read_f32",
                64: "b2r_read_f64",
                80: "b2r_read_f80",
            }.get(operand.size)
            if helper is None:
                raise X86DecodeError(
                    f"cannot emit x87 float size {operand.size}"
                )
            return f"{helper}(ctx, {self._effective_address_expr(operand)})"
        raise X86DecodeError(f"cannot emit x87 float read for {operand.kind}")

    def _xmm_operand_read(self, operand: Operand) -> str:
        if operand.kind == "reg":
            return f"ctx->xmm[{self._xmm_register_index(operand)}u]"
        if operand.kind == "mem":
            return f"b2r_read_xmm(ctx, {self._effective_address_expr(operand)})"
        raise X86DecodeError(f"cannot emit XMM read for {operand.kind}")

    def _xmm_scalar_operand_read(self, operand: Operand) -> str:
        if operand.kind == "reg":
            return f"ctx->xmm[{self._xmm_register_index(operand)}u].lane[0]"
        if operand.kind == "mem":
            return f"b2r_read_f32(ctx, {self._effective_address_expr(operand)})"
        raise X86DecodeError(f"cannot emit XMM scalar read for {operand.kind}")

    def _emit_xmm_write(self, operand: Operand, value_expr: str) -> list[str]:
        if operand.kind == "reg":
            return [f"ctx->xmm[{self._xmm_register_index(operand)}u] = {value_expr};"]
        if operand.kind == "mem":
            return [
                f"b2r_write_xmm(ctx, {self._effective_address_expr(operand)}, {value_expr});"
            ]
        raise X86DecodeError(f"cannot emit XMM write for {operand.kind}")

    def _mmx_operand_read(self, operand: Operand) -> str:
        if operand.kind == "reg":
            return f"ctx->mmx[{self._mmx_register_index(operand)}u]"
        if operand.kind == "mem":
            return f"b2r_read_u64(ctx, {self._effective_address_expr(operand)})"
        raise X86DecodeError(f"cannot emit MMX read for {operand.kind}")

    def _emit_mmx_write(self, operand: Operand, value_expr: str) -> list[str]:
        if operand.kind == "reg":
            return [f"ctx->mmx[{self._mmx_register_index(operand)}u] = {value_expr};"]
        if operand.kind == "mem":
            return [
                f"b2r_write_u64(ctx, {self._effective_address_expr(operand)}, {value_expr});"
            ]
        raise X86DecodeError(f"cannot emit MMX write for {operand.kind}")

    @staticmethod
    def _x87_stack_index(operand: Operand) -> int:
        if operand.kind != "imm" or operand.immediate is None or operand.immediate > 7:
            raise X86DecodeError("cannot emit x87 stack operand")
        return operand.immediate

    @staticmethod
    def _xmm_register_index(operand: Operand) -> int:
        if operand.kind != "reg" or operand.reg not in XMM_REGISTER_NAMES:
            raise X86DecodeError("cannot emit XMM register operand")
        return XMM_REGISTER_NAMES.index(operand.reg)

    @staticmethod
    def _mmx_register_index(operand: Operand) -> int:
        if operand.kind != "reg" or operand.reg not in MMX_REGISTER_NAMES:
            raise X86DecodeError("cannot emit MMX register operand")
        return MMX_REGISTER_NAMES.index(operand.reg)

    @staticmethod
    def _byte_register_parent(register: str) -> tuple[str, int]:
        parents = {
            "al": ("eax", 0),
            "cl": ("ecx", 0),
            "dl": ("edx", 0),
            "bl": ("ebx", 0),
            "ah": ("eax", 8),
            "ch": ("ecx", 8),
            "dh": ("edx", 8),
            "bh": ("ebx", 8),
            "eax": ("eax", 0),
            "ecx": ("ecx", 0),
            "edx": ("edx", 0),
            "ebx": ("ebx", 0),
        }
        try:
            return parents[register.casefold()]
        except KeyError as exc:
            raise X86DecodeError(f"cannot emit byte register access for {register}") from exc

    def _effective_address_expr(self, operand: Operand) -> str:
        if operand.kind != "mem":
            raise X86DecodeError("cannot emit effective address for non-memory operand")
        terms: list[str] = []
        if operand.absolute is not None:
            terms.append(_cpp_u32(operand.absolute))
        if operand.base:
            terms.append(f"ctx->{operand.base}")
        if operand.index:
            term = f"ctx->{operand.index}"
            if operand.scale != 1:
                term = f"({term} * {operand.scale}u)"
            terms.append(term)
        if operand.displacement:
            terms.append(_cpp_u32(operand.displacement))
        if operand.segment == "fs":
            terms.insert(0, "ctx->fs_base")
        elif operand.segment is not None:
            raise X86DecodeError(f"cannot emit segment override {operand.segment}")
        if not terms:
            terms.append("0u")
        return "(" + " + ".join(terms) + ")"

    @staticmethod
    def _condition_expr(condition: str) -> str:
        return {
            "o": "ctx->flags.of",
            "no": "!ctx->flags.of",
            "b": "ctx->flags.cf",
            "ae": "!ctx->flags.cf",
            "e": "ctx->flags.zf",
            "ne": "!ctx->flags.zf",
            "be": "(ctx->flags.cf || ctx->flags.zf)",
            "a": "(!ctx->flags.cf && !ctx->flags.zf)",
            "s": "ctx->flags.sf",
            "ns": "!ctx->flags.sf",
            "p": "ctx->flags.pf",
            "np": "!ctx->flags.pf",
            "l": "(ctx->flags.sf != ctx->flags.of)",
            "ge": "(ctx->flags.sf == ctx->flags.of)",
            "le": "(ctx->flags.zf || (ctx->flags.sf != ctx->flags.of))",
            "g": "(!ctx->flags.zf && (ctx->flags.sf == ctx->flags.of))",
            "ecx_zero": "(ctx->ecx == 0u)",
        }[condition]


def emit_cpp(
    function: LiftedFunction,
    *,
    exported_symbol: str | None = None,
    resumable: bool = False,
    observer_addresses: Iterable[int] = (),
    callback_addresses: Iterable[int] = (),
    forwarded_callback_addresses: Iterable[int] = (),
    native_fast_paths: Mapping[int, NativeFastPath] | None = None,
    synchronize_eip_for_callbacks: bool = False,
) -> str:
    return CppEmitter().emit(
        function,
        exported_symbol=exported_symbol,
        resumable=resumable,
        observer_addresses=observer_addresses,
        callback_addresses=callback_addresses,
        forwarded_callback_addresses=forwarded_callback_addresses,
        native_fast_paths=native_fast_paths,
        synchronize_eip_for_callbacks=synchronize_eip_for_callbacks,
    )


def cpp_sha256(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest().upper()


def build_recompilation_summary(
    function: LiftedFunction,
    *,
    cpp_source: str | None = None,
    cpp_output: Path | None = None,
    source_kind: str = "bytes",
    source_name: str | None = None,
    public_safe: bool = True,
) -> dict[str, Any]:
    source = cpp_source or emit_cpp(function)
    summary = {
        "format": "b2-recomp-recompile-range",
        "public_safe": public_safe,
        "source": {
            "kind": source_kind,
            "name": source_name,
        },
        "target_platform": SUPPORTED_TARGET_PLATFORM,
        "generated_language": GENERATED_LANGUAGE,
        "renderer_backend": FIRST_RENDERER_BACKEND,
        "lifted": function.to_dict(include_bytes=False),
        "generated_cpp": {
            "sha256": cpp_sha256(source),
            "byte_count": len(source.encode("utf-8")),
            "output": str(cpp_output) if cpp_output is not None else None,
        },
    }
    return summary


def summary_json(summary: dict[str, Any], *, pretty: bool = False) -> str:
    return json.dumps(summary, indent=2 if pretty else None, sort_keys=True)


def _cpp_u32(value: int) -> str:
    return f"0x{value & 0xFFFFFFFF:08X}u"


def _cpp_identifier(value: str) -> str:
    clean = [char if char.isalnum() or char == "_" else "_" for char in value]
    identifier = "".join(clean)
    if not identifier or identifier[0].isdigit():
        identifier = f"b2r_{identifier}"
    return identifier
