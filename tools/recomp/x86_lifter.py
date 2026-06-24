#!/usr/bin/env python3
"""Small IA-32 lifter, executor, and C++ emitter for the first recomp prototype."""

from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import Callable, Iterable
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
SUPPORTED_TARGET_PLATFORM = "windows"
GENERATED_LANGUAGE = "c++17"
FIRST_RENDERER_BACKEND = "vulkan"


class RecompilationError(RuntimeError):
    """Base error for recompilation prototype failures."""


class X86DecodeError(RecompilationError):
    """Raised when the prototype decoder reaches an unsupported instruction."""


class X86ExecutionError(RecompilationError):
    """Raised when lifted code cannot be executed by the trace executor."""


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


def _hex32(value: int) -> str:
    return f"0x{value & 0xFFFFFFFF:08X}"


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

    def to_dict(self) -> dict[str, bool]:
        return {
            "cf": self.cf,
            "pf": self.pf,
            "af": self.af,
            "zf": self.zf,
            "sf": self.sf,
            "of": self.of,
        }


@dataclass
class CpuState:
    registers: dict[str, int] = field(
        default_factory=lambda: {name: 0 for name in REGISTER_NAMES}
    )
    flags: CpuFlags = field(default_factory=CpuFlags)
    eip: int = 0

    @classmethod
    def with_registers(cls, **registers: int) -> "CpuState":
        state = cls()
        for name, value in registers.items():
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "registers": {name: _hex32(self.registers[name]) for name in REGISTER_NAMES},
            "flags": self.flags.to_dict(),
            "eip": self.eip,
            "eip_hex": _hex32(self.eip),
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

    @classmethod
    def register(cls, reg: str) -> "Operand":
        return cls("reg", reg=reg)

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
    ) -> "Operand":
        return cls(
            "mem",
            base=base,
            index=index,
            scale=scale,
            displacement=displacement,
            absolute=absolute,
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
        if self.absolute is not None:
            return f"[{_hex32(self.absolute)}]"
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
        return f"[{text}]"


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
    def __init__(self) -> None:
        self._events: list[ExecutionEvent] = []

    def add(self, address: int | None, operation: str, **details: Any) -> None:
        self._events.append(
            ExecutionEvent(
                len(self._events),
                address,
                operation,
                _json_safe(details),
            )
        )

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

    def __init__(self, initial: dict[int, bytes | int] | None = None) -> None:
        self._data: dict[int, int] = {}
        for address, value in (initial or {}).items():
            if isinstance(value, int):
                self.write_u32(address, value)
            else:
                self.write(address, value)

    def read(self, address: int, size: int) -> bytes:
        if size < 0:
            raise X86ExecutionError("cannot read a negative size")
        return bytes(self._data.get(_u32(address + offset), 0) for offset in range(size))

    def read_u32(self, address: int) -> int:
        return struct.unpack("<I", self.read(address, 4))[0]

    def write(self, address: int, payload: bytes) -> None:
        for offset, byte in enumerate(payload):
            self._data[_u32(address + offset)] = byte

    def write_u32(self, address: int, value: int) -> None:
        self.write(address, struct.pack("<I", _u32(value)))

    def snapshot_u32(self, addresses: Iterable[int]) -> dict[int, int]:
        return {address: self.read_u32(address) for address in addresses}


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
            if stop_at_ret and instruction.mnemonic == "ret":
                break
        if len(instructions) >= max_instructions and offset < len(code):
            raise X86DecodeError("instruction limit reached before function end")
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
        opcode = self._read_u8(code, offset)
        offset += 1

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

        if opcode == 0x90:
            return inst("nop")
        if 0x50 <= opcode <= 0x57:
            return inst("push", (Operand.register(REGISTER_BY_INDEX[opcode - 0x50]),))
        if 0x58 <= opcode <= 0x5F:
            return inst("pop", (Operand.register(REGISTER_BY_INDEX[opcode - 0x58]),))
        if 0x40 <= opcode <= 0x47:
            return inst("inc", (Operand.register(REGISTER_BY_INDEX[opcode - 0x40]),))
        if 0x48 <= opcode <= 0x4F:
            return inst("dec", (Operand.register(REGISTER_BY_INDEX[opcode - 0x48]),))
        if 0xB8 <= opcode <= 0xBF:
            value = _read_u32(code, offset)
            offset += 4
            return inst(
                "mov",
                (
                    Operand.register(REGISTER_BY_INDEX[opcode - 0xB8]),
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
        if opcode == 0xA1:
            absolute = _read_u32(code, offset)
            offset += 4
            return inst(
                "mov",
                (Operand.register("eax"), Operand.memory(absolute=absolute)),
            )
        if opcode == 0xA3:
            absolute = _read_u32(code, offset)
            offset += 4
            return inst(
                "mov",
                (Operand.memory(absolute=absolute), Operand.register("eax")),
            )
        if opcode == 0xC3:
            return inst("ret")
        if opcode == 0xC2:
            adjust = _read_u16(code, offset)
            offset += 2
            return inst("ret", ret_stack_adjust=adjust)
        if opcode == 0xC9:
            return inst("leave")
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
            if 0x80 <= second <= 0x8F:
                rel = _i32(_read_u32(code, offset))
                offset += 4
                condition = _condition_for_short_opcode(second - 0x10)
                return inst(
                    "jcc",
                    target=_u32(base_address + offset + rel),
                    condition=condition,
                )
            raise X86DecodeError(f"unsupported two-byte opcode 0F {second:02X} at {_hex32(address)}")

        if opcode in {0x01, 0x03, 0x09, 0x0B, 0x21, 0x23, 0x29, 0x2B, 0x31, 0x33, 0x39, 0x3B, 0x85, 0x89, 0x8B, 0x8D}:
            reg_index, rm_operand, offset = self._decode_modrm(code, offset)
            reg_operand = Operand.register(REGISTER_BY_INDEX[reg_index])
            if opcode in {0x01, 0x09, 0x21, 0x29, 0x31, 0x39, 0x85, 0x89}:
                operands = (rm_operand, reg_operand)
            else:
                operands = (reg_operand, rm_operand)
            mnemonic = {
                0x01: "add",
                0x03: "add",
                0x09: "or",
                0x0B: "or",
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

        if opcode in {0x05, 0x0D, 0x25, 0x2D, 0x35, 0x3D, 0xA9}:
            value = _read_u32(code, offset)
            offset += 4
            mnemonic = {
                0x05: "add",
                0x0D: "or",
                0x25: "and",
                0x2D: "sub",
                0x35: "xor",
                0x3D: "cmp",
                0xA9: "test",
            }[opcode]
            return inst(
                mnemonic,
                (Operand.register("eax"), Operand.immediate_u32(value)),
            )

        if opcode in {0x81, 0x83}:
            reg_index, rm_operand, offset = self._decode_modrm(code, offset)
            if opcode == 0x81:
                value = _read_u32(code, offset)
                offset += 4
            else:
                value = _u32(_i8(self._read_u8(code, offset)))
                offset += 1
            mnemonic = {
                0: "add",
                1: "or",
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

        if opcode == 0xC7:
            reg_index, rm_operand, offset = self._decode_modrm(code, offset)
            if reg_index != 0:
                raise X86DecodeError(f"unsupported C7 /{reg_index} at {_hex32(address)}")
            value = _read_u32(code, offset)
            offset += 4
            return inst("mov", (rm_operand, Operand.immediate_u32(value)))

        if opcode == 0xFF:
            reg_index, rm_operand, offset = self._decode_modrm(code, offset)
            if reg_index == 2:
                return inst("call", (rm_operand,))
            if reg_index == 4:
                return inst("jmp", (rm_operand,))
            if reg_index == 6:
                return inst("push", (rm_operand,))
            raise X86DecodeError(f"unsupported FF /{reg_index} at {_hex32(address)}")

        raise X86DecodeError(f"unsupported opcode {opcode:02X} at {_hex32(address)}")

    def _decode_modrm(self, code: bytes, offset: int) -> tuple[int, Operand, int]:
        modrm = self._read_u8(code, offset)
        offset += 1
        mod = (modrm >> 6) & 0b11
        reg = (modrm >> 3) & 0b111
        rm = modrm & 0b111

        if mod == 0b11:
            return reg, Operand.register(REGISTER_BY_INDEX[rm]), offset

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
                absolute = _read_u32(code, offset)
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
    max_steps: int = 1024,
) -> ExecutionResult:
    active_state = state or CpuState()
    active_memory = memory or SparseMemory()
    active_state.eip = function.base_address
    handlers = call_handlers or {}
    trace = ExecutionTrace()
    instructions = {instruction.address: instruction for instruction in function.instructions}
    valid_addresses = set(instructions)
    steps = 0

    while steps < max_steps:
        instruction = instructions.get(active_state.eip)
        if instruction is None:
            raise X86ExecutionError(f"no lifted instruction at {_hex32(active_state.eip)}")
        trace.add(instruction.address, "instruction", text=instruction.text())
        steps += 1
        next_eip = instruction.next_address

        if instruction.mnemonic == "nop":
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "mov":
            value = _read_operand(active_state, active_memory, trace, instruction, instruction.operands[1])
            _write_operand(active_state, active_memory, trace, instruction, instruction.operands[0], value)
            active_state.eip = next_eip
            continue
        if instruction.mnemonic == "lea":
            address = _effective_address(active_state, instruction.operands[1])
            _write_operand(active_state, active_memory, trace, instruction, instruction.operands[0], address)
            active_state.eip = next_eip
            continue
        if instruction.mnemonic in {"add", "sub", "xor", "and", "or"}:
            lhs = _read_operand(active_state, active_memory, trace, instruction, instruction.operands[0])
            rhs = _read_operand(active_state, active_memory, trace, instruction, instruction.operands[1])
            result = _binary_result(instruction.mnemonic, lhs, rhs)
            _write_operand(active_state, active_memory, trace, instruction, instruction.operands[0], result)
            _update_flags(active_state.flags, instruction.mnemonic, lhs, rhs, result)
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
            _update_flags(active_state.flags, operation, lhs, rhs, result)
            active_state.flags.cf = old_cf
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
            elif target in valid_addresses:
                active_state.eip = target
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
            active_state.eip = target
            continue
        if instruction.mnemonic == "jcc":
            if instruction.target is None or instruction.condition is None:
                raise X86ExecutionError("conditional branch missing target or condition")
            taken = _evaluate_condition(active_state.flags, instruction.condition)
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
            return ExecutionResult(
                active_state,
                active_memory,
                trace,
                return_address=return_address,
                steps=steps,
            )
        raise X86ExecutionError(f"unsupported lifted mnemonic: {instruction.mnemonic}")

    raise X86ExecutionError("execution step limit reached")


def _read_operand(
    state: CpuState,
    memory: SparseMemory,
    trace: ExecutionTrace,
    instruction: X86Instruction,
    operand: Operand,
) -> int:
    if operand.kind == "reg":
        return state.get_register(operand.reg or "")
    if operand.kind == "imm":
        return operand.immediate or 0
    if operand.kind == "mem":
        address = _effective_address(state, operand)
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
        memory.write_u32(address, value)
        trace.add(
            instruction.address,
            "memory_write",
            memory_address=address,
            memory_address_hex=_hex32(address),
            value=value,
            value_hex=_hex32(value),
        )
        return
    raise X86ExecutionError(f"cannot write operand kind {operand.kind}")


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


def _update_flags(flags: CpuFlags, operation: str, lhs: int, rhs: int, result: int) -> None:
    lhs = _u32(lhs)
    rhs = _u32(rhs)
    result = _u32(result)
    sign_bit = 0x80000000
    flags.zf = result == 0
    flags.sf = bool(result & sign_bit)
    flags.pf = _parity8(result)
    if operation == "add":
        flags.cf = lhs + rhs > 0xFFFFFFFF
        flags.of = bool((~(lhs ^ rhs) & (lhs ^ result) & sign_bit) != 0)
        flags.af = bool((lhs ^ rhs ^ result) & 0x10)
    elif operation == "sub":
        flags.cf = lhs < rhs
        flags.of = bool(((lhs ^ rhs) & (lhs ^ result) & sign_bit) != 0)
        flags.af = bool((lhs ^ rhs ^ result) & 0x10)
    elif operation in {"xor", "and", "or"}:
        flags.cf = False
        flags.of = False
        flags.af = False
    else:
        raise X86ExecutionError(f"cannot update flags for {operation}")


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

    def emit(self, function: LiftedFunction, *, exported_symbol: str | None = None) -> str:
        symbol = _cpp_identifier(exported_symbol or function.symbol)
        lines = [
            "// Generated by b2_recomp Milestone 5 prototype.",
            "// Target: Windows x86 guest semantics, C++17 host translation unit.",
            "#include <cstdint>",
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
            "    B2RFlags flags;",
            "    void* user;",
            "    uint32_t (*read_u32)(void*, uint32_t);",
            "    void (*write_u32)(void*, uint32_t, uint32_t);",
            "    void (*call)(void*, uint32_t, B2RContext*);",
            "};",
            "",
            "static inline bool b2r_parity8(uint32_t value) {",
            "    value &= 0xffu;",
            "    value ^= value >> 4;",
            "    value &= 0x0fu;",
            "    return ((0x6996u >> value) & 1u) == 0u;",
            "}",
            "",
            "static inline void b2r_logic_flags(B2RContext* ctx, uint32_t result) {",
            "    ctx->flags.cf = false;",
            "    ctx->flags.of = false;",
            "    ctx->flags.af = false;",
            "    ctx->flags.zf = result == 0u;",
            "    ctx->flags.sf = (result & 0x80000000u) != 0u;",
            "    ctx->flags.pf = b2r_parity8(result);",
            "}",
            "",
            "static inline void b2r_add_flags(B2RContext* ctx, uint32_t lhs, uint32_t rhs, uint32_t result) {",
            "    ctx->flags.cf = (static_cast<uint64_t>(lhs) + static_cast<uint64_t>(rhs)) > 0xffffffffull;",
            "    ctx->flags.of = ((~(lhs ^ rhs) & (lhs ^ result) & 0x80000000u) != 0u);",
            "    ctx->flags.af = ((lhs ^ rhs ^ result) & 0x10u) != 0u;",
            "    ctx->flags.zf = result == 0u;",
            "    ctx->flags.sf = (result & 0x80000000u) != 0u;",
            "    ctx->flags.pf = b2r_parity8(result);",
            "}",
            "",
            "static inline void b2r_sub_flags(B2RContext* ctx, uint32_t lhs, uint32_t rhs, uint32_t result) {",
            "    ctx->flags.cf = lhs < rhs;",
            "    ctx->flags.of = (((lhs ^ rhs) & (lhs ^ result) & 0x80000000u) != 0u);",
            "    ctx->flags.af = ((lhs ^ rhs ^ result) & 0x10u) != 0u;",
            "    ctx->flags.zf = result == 0u;",
            "    ctx->flags.sf = (result & 0x80000000u) != 0u;",
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
            f'extern "C" __declspec(dllexport) uint32_t {symbol}(B2RContext* ctx) {{',
            f"    uint32_t eip = {_cpp_u32(function.base_address)};",
            "    for (;;) {",
            "        switch (eip) {",
        ]
        for instruction in function.instructions:
            lines.append(f"        case {_cpp_u32(instruction.address)}: {{")
            lines.append(f"            // {instruction.text()}")
            lines.extend(f"            {line}" for line in self._emit_instruction(instruction))
            lines.append("        }")
        lines.extend(
            [
                "        default:",
                "            return eip;",
                "        }",
                "    }",
                "}",
                "",
            ]
        )
        return "\n".join(lines)

    def _emit_instruction(self, instruction: X86Instruction) -> list[str]:
        next_eip = _cpp_u32(instruction.next_address)
        if instruction.mnemonic == "nop":
            return [f"eip = {next_eip};", "continue;"]
        if instruction.mnemonic == "mov":
            return self._emit_write(
                instruction.operands[0],
                self._operand_read(instruction.operands[1]),
            ) + [f"eip = {next_eip};", "continue;"]
        if instruction.mnemonic == "lea":
            return self._emit_write(
                instruction.operands[0],
                self._effective_address_expr(instruction.operands[1]),
            ) + [f"eip = {next_eip};", "continue;"]
        if instruction.mnemonic in {"add", "sub", "xor", "and", "or"}:
            op = { "add": "+", "sub": "-", "xor": "^", "and": "&", "or": "|" }[
                instruction.mnemonic
            ]
            lines = [
                f"const uint32_t lhs = {self._operand_read(instruction.operands[0])};",
                f"const uint32_t rhs = {self._operand_read(instruction.operands[1])};",
                f"const uint32_t result = lhs {op} rhs;",
            ]
            lines.extend(self._emit_write(instruction.operands[0], "result"))
            if instruction.mnemonic == "add":
                lines.append("b2r_add_flags(ctx, lhs, rhs, result);")
            elif instruction.mnemonic == "sub":
                lines.append("b2r_sub_flags(ctx, lhs, rhs, result);")
            else:
                lines.append("b2r_logic_flags(ctx, result);")
            lines.extend([f"eip = {next_eip};", "continue;"])
            return lines
        if instruction.mnemonic in {"cmp", "test"}:
            op = "-" if instruction.mnemonic == "cmp" else "&"
            lines = [
                f"const uint32_t lhs = {self._operand_read(instruction.operands[0])};",
                f"const uint32_t rhs = {self._operand_read(instruction.operands[1])};",
                f"const uint32_t result = lhs {op} rhs;",
                "b2r_sub_flags(ctx, lhs, rhs, result);"
                if instruction.mnemonic == "cmp"
                else "b2r_logic_flags(ctx, result);",
                f"eip = {next_eip};",
                "continue;",
            ]
            return lines
        if instruction.mnemonic in {"inc", "dec"}:
            op = "+" if instruction.mnemonic == "inc" else "-"
            flag_helper = "b2r_add_flags" if instruction.mnemonic == "inc" else "b2r_sub_flags"
            lines = [
                f"const bool old_cf = ctx->flags.cf;",
                f"const uint32_t lhs = {self._operand_read(instruction.operands[0])};",
                "const uint32_t rhs = 1u;",
                f"const uint32_t result = lhs {op} rhs;",
            ]
            lines.extend(self._emit_write(instruction.operands[0], "result"))
            lines.extend(
                [
                    f"{flag_helper}(ctx, lhs, rhs, result);",
                    "ctx->flags.cf = old_cf;",
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
        if instruction.mnemonic == "call":
            target = (
                _cpp_u32(instruction.target)
                if instruction.target is not None
                else self._operand_read(instruction.operands[0])
            )
            return [
                f"b2r_push(ctx, {next_eip});",
                f"ctx->call(ctx->user, {target}, ctx);",
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
            return [f"eip = {target};", "continue;"]
        if instruction.mnemonic == "jcc":
            if instruction.target is None or instruction.condition is None:
                raise X86DecodeError("conditional branch missing target")
            condition = self._condition_expr(instruction.condition)
            return [
                f"eip = ({condition}) ? {_cpp_u32(instruction.target)} : {next_eip};",
                "continue;",
            ]
        if instruction.mnemonic == "ret":
            lines = ["const uint32_t return_address = b2r_pop(ctx);"]
            if instruction.ret_stack_adjust:
                lines.append(f"ctx->esp += {_cpp_u32(instruction.ret_stack_adjust)};")
            lines.append("return return_address;")
            return lines
        raise X86DecodeError(f"cannot emit C++ for {instruction.mnemonic}")

    def _emit_write(self, operand: Operand, value_expr: str) -> list[str]:
        if operand.kind == "reg":
            return [f"ctx->{operand.reg} = {value_expr};"]
        if operand.kind == "mem":
            return [
                f"ctx->write_u32(ctx->user, {self._effective_address_expr(operand)}, {value_expr});"
            ]
        raise X86DecodeError(f"cannot emit write for {operand.kind}")

    def _operand_read(self, operand: Operand) -> str:
        if operand.kind == "reg":
            return f"ctx->{operand.reg}"
        if operand.kind == "imm":
            return _cpp_u32(operand.immediate or 0)
        if operand.kind == "mem":
            return f"ctx->read_u32(ctx->user, {self._effective_address_expr(operand)})"
        raise X86DecodeError(f"cannot emit read for {operand.kind}")

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
        }[condition]


def emit_cpp(function: LiftedFunction, *, exported_symbol: str | None = None) -> str:
    return CppEmitter().emit(function, exported_symbol=exported_symbol)


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
