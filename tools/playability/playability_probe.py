#!/usr/bin/env python3
"""Milestone 7 boot/playability probe with runtime shim ABI dispatch."""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import struct
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any

try:
    from runtime.xbox.shims import (
        RuntimeShim,
        XboxRuntimeConfig,
        XboxRuntimeError,
        XboxRuntimeShims,
        XboxStatus,
    )
    from tools.loader.xbe_loader import (
        ImportResolver,
        LoadedXbeImage,
        XbeMemoryAccessError,
        load_xbe_file,
    )
    from tools.recomp.x86_lifter import (
        CpuState,
        ExecutionTrace,
        LiftedFunction,
        Operand,
        SparseMemory,
        X86Instruction,
        X86DecodeError,
        X86ExecutionError,
        execute_lifted_function,
        lift_x86_block,
        lift_x86_function,
    )
    from tools.render.d3d8_stream import (
        extract_render_streams_from_probe_summary,
        write_json,
    )
    from tools.xbe.xbe_info import parse_xbe_file
except ModuleNotFoundError:  # pragma: no cover - direct script execution fallback
    import sys

    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from runtime.xbox.shims import (
        RuntimeShim,
        XboxRuntimeConfig,
        XboxRuntimeError,
        XboxRuntimeShims,
        XboxStatus,
    )
    from tools.loader.xbe_loader import (
        ImportResolver,
        LoadedXbeImage,
        XbeMemoryAccessError,
        load_xbe_file,
    )
    from tools.recomp.x86_lifter import (
        CpuState,
        ExecutionTrace,
        LiftedFunction,
        Operand,
        SparseMemory,
        X86Instruction,
        X86DecodeError,
        X86ExecutionError,
        execute_lifted_function,
        lift_x86_block,
        lift_x86_function,
    )
    from tools.render.d3d8_stream import (
        extract_render_streams_from_probe_summary,
        write_json,
    )
    from tools.xbe.xbe_info import parse_xbe_file


DEFAULT_ENTRY_BYTES = 0x200
DEFAULT_MAX_INSTRUCTIONS = 128
DEFAULT_MAX_BLOCK_INSTRUCTIONS = 224
DEFAULT_MAX_STEPS = 256
DEFAULT_MAX_THREAD_STEPS = 65536
DEFAULT_INTERNAL_DEPTH = 15
DEFAULT_MAX_RECOVERED_BLOCKS = 1024
DEFAULT_MAX_DYNAMIC_BLOCKS = 512
DEFAULT_MAX_GUEST_ARGUMENTS = 16
DEFAULT_MAX_GUEST_THREAD_EXECUTIONS = 8
DEFAULT_STACK_BASE = 0x70000000
DEFAULT_THREAD_STACK_BASE = 0x71000000
DEFAULT_FS_BASE = 0x72000000
THREAD_STACK_STRIDE = 0x00010000
BOOT_PROBE_RETURN = 0xB2000000
BOOT_THREAD_RETURN_BASE = 0xB2100000

SCHEDULER_PRODUCER_ANCHORS = {
    0x00127E9B: {
        "semantic": "initial_node_key_seed",
        "block_entry_hex": "0x00127E8C",
        "instruction_text": "mov [esi + 0x8], eax",
        "field": "work_item.key",
        "field_offset_hex": "0x00000008",
        "source": "0x005B8EDC",
        "hle_role": "seeds the observed scheduler node key field before runtime enqueue activity",
    },
    0x00127ED2: {
        "semantic": "initial_node_pointer_byte_seed",
        "block_entry_hex": "0x00127ECC",
        "instruction_text": "mov [eax + esi + 0x1C], cl",
        "field": "work_item.next_byte",
        "hle_role": "seeds a byte inside the watched scheduler node pointer area",
    },
    0x000F29F6: {
        "semantic": "queue_slot_replacement_writer",
        "block_entry_hex": "0x000F29DE",
        "instruction_text": "mov [esi + ebx*4], edi",
        "field": "watched scheduler slot",
        "hle_role": "latest observed scheduler slot replacement before wait",
    },
    0x000F2A63: {
        "semantic": "work_item_key_writer",
        "block_entry_hex": "0x000F2A59",
        "instruction_text": "mov [eax + 0x8], ecx",
        "field": "work_item.key",
        "field_offset_hex": "0x00000008",
        "source_instruction_address_hex": "0x000F2A5B",
        "source_instruction_text": "mov ecx, [esp + 0x24]",
        "source_stack_offset_hex": "0x00000024",
        "hle_role": "writes queued work-item key from scheduler argument",
    },
    0x000F2AAB: {
        "semantic": "work_item_next_clear",
        "block_entry_hex": "0x000F2AA0",
        "instruction_text": "mov [eax + 0x30], ebp",
        "field": "work_item.next",
        "field_offset_hex": "0x00000030",
        "hle_role": "initializes work-item next pointer before linking",
    },
    0x000F2AF9: {
        "semantic": "work_item_next_link",
        "block_entry_hex": "0x000F2AF6",
        "instruction_text": "mov [edx + 0x30], eax",
        "field": "work_item.next",
        "field_offset_hex": "0x00000030",
        "hle_role": "links the queued work item into the observed scheduler list",
    },
}

GUEST_ARGUMENT_COUNT_OVERRIDES = {
    "KfLowerIrql": 0,
    "NtCreateFile": 11,
    "NtOpenFile": 6,
    "NtOpenSymbolicLinkObject": 2,
    "NtQueryInformationFile": 5,
    "NtQuerySymbolicLinkObject": 3,
    "NtReadFile": 8,
    "NtSetInformationFile": 5,
    "NtWriteFile": 8,
    "PsCreateSystemThreadEx": 10,
}

FILE_APPEND_DATA = 0x00000004
FILE_WRITE_DATA = 0x00000002
GENERIC_WRITE = 0x40000000
FILE_SUPERSEDE = 0
FILE_CREATE = 2
FILE_OPEN_IF = 3
FILE_OVERWRITE = 4
FILE_OVERWRITE_IF = 5


class RuntimeAbiBridgeError(RuntimeError):
    """Raised when a recovered guest call cannot be adapted to a shim handler."""


class BootProbeStop(RuntimeError):
    """Internal sentinel used to stop cleanly at unrecovered code handoffs."""

    def __init__(
        self,
        *,
        target: int,
        stack_args: tuple[int, ...],
        state: CpuState,
        trace: ExecutionTrace,
    ) -> None:
        super().__init__(f"stopped at unrecovered internal call 0x{target:08X}")
        self.target = target
        self.stack_args = stack_args
        self.state = state
        self.trace = trace


class RenderWatchpointStop(RuntimeError):
    """Internal sentinel used to stop after a requested render-write count."""

    def __init__(self, stream: dict[str, Any]) -> None:
        super().__init__("render write watchpoint limit reached")
        self.stream = stream


class RenderWriteWatchpoint:
    """Capture D3D MMIO/push-buffer writes as memory-observer events."""

    def __init__(
        self,
        *,
        max_writes: int = 512,
        stop_after: int | None = None,
    ) -> None:
        self.max_writes = max_writes
        self.stop_after = stop_after
        self.mmio_write_count = 0
        self.push_buffer_write_count = 0
        self.writes: list[dict[str, Any]] = []

    @property
    def write_count(self) -> int:
        return self.mmio_write_count + self.push_buffer_write_count

    def observe(self, address: int, payload: bytes) -> None:
        kind: str | None = None
        offset: int | None = None
        if 0xFED00000 <= address <= 0xFED0FFFF:
            kind = "d3d_mmio"
            offset = address - 0xFED00000
            self.mmio_write_count += 1
        elif 0x80000000 <= address <= 0x8000FFFF:
            kind = "d3d_push_buffer"
            offset = address - 0x80000000
            self.push_buffer_write_count += 1
        if kind is None:
            return
        value = int.from_bytes(payload[: min(len(payload), 4)], "little", signed=False)
        if len(self.writes) < self.max_writes:
            self.writes.append(
                {
                    "sequence": self.write_count - 1,
                    "instruction_address_hex": None,
                    "kind": kind,
                    "address": _u32(address),
                    "address_hex": _hex32(address),
                    "offset": offset,
                    "offset_hex": _hex32(offset) if offset is not None else None,
                    "value": value,
                    "value_hex": _hex32(value),
                    "size": len(payload),
                }
            )
        if self.stop_after is not None and self.write_count >= self.stop_after:
            raise RenderWatchpointStop(self.to_stream())

    def to_stream(self) -> dict[str, Any]:
        return {
            "write_count": self.write_count,
            "mmio_write_count": self.mmio_write_count,
            "push_buffer_write_count": self.push_buffer_write_count,
            "captured_write_count": len(self.writes),
            "truncated": self.write_count > len(self.writes),
            "writes": list(self.writes),
        }


@dataclass(frozen=True)
class RecoveryWorkItem:
    target: int
    depth: int
    caller: int
    kind: str


@dataclass(frozen=True)
class RuntimeAbiInvocation:
    target_address: int
    shim_name: str
    ordinal: int
    subsystem: str
    arguments: tuple[int, ...]
    handler_arguments: tuple[int, ...]
    return_kind: str
    eax: int | None = None
    stack_cleanup_bytes: int = 0
    result: Any = None
    memory_writes: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_address": self.target_address,
            "target_address_hex": _hex32(self.target_address),
            "shim_name": self.shim_name,
            "ordinal": self.ordinal,
            "subsystem": self.subsystem,
            "arguments": [_hex32(argument) for argument in self.arguments],
            "handler_arguments": [
                _hex32(argument) for argument in self.handler_arguments
            ],
            "return_kind": self.return_kind,
            "eax": self.eax,
            "eax_hex": _hex32(self.eax) if self.eax is not None else None,
            "stack_cleanup_bytes": self.stack_cleanup_bytes,
            "result": _json_safe(self.result),
            "memory_writes": [_json_safe(write) for write in self.memory_writes],
        }


@dataclass(frozen=True)
class RuntimeAbiResult:
    invocation: RuntimeAbiInvocation
    returned_value: Any

    def to_dict(self) -> dict[str, Any]:
        return self.invocation.to_dict()


class RuntimeAbiBridge:
    """Adapt lifted IA-32 calls to registered Python runtime shim handlers."""

    def __init__(
        self,
        runtime: XboxRuntimeShims,
        *,
        max_guest_arguments: int = DEFAULT_MAX_GUEST_ARGUMENTS,
    ) -> None:
        self.runtime = runtime
        self.max_guest_arguments = max_guest_arguments
        self._by_target = {
            shim.target_address: shim for shim in runtime.registered_shims
        }
        self._invocations: list[RuntimeAbiInvocation] = []

    @property
    def invocations(self) -> tuple[RuntimeAbiInvocation, ...]:
        return tuple(self._invocations)

    @property
    def shim_targets(self) -> frozenset[int]:
        return frozenset(self._by_target)

    def has_target(self, target: int) -> bool:
        return target in self._by_target

    def call_handlers(
        self,
    ) -> dict[int, Callable[[CpuState, SparseMemory, int, ExecutionTrace], None]]:
        return {target: self.call_handler for target in self._by_target}

    def call_handler(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
    ) -> None:
        self.invoke(cpu, memory, target, trace)

    def invoke(
        self,
        cpu: CpuState,
        memory: SparseMemory,
        target: int,
        trace: ExecutionTrace,
    ) -> RuntimeAbiResult:
        shim = self._by_target.get(target)
        if shim is None:
            raise RuntimeAbiBridgeError(f"no registered runtime shim at {_hex32(target)}")

        guest_argument_count = self._guest_argument_count_for(shim)
        handler_argument_count = self._handler_argument_count_for(shim)
        arguments = _read_stack_arguments(
            memory, cpu.get_register("esp"), guest_argument_count
        )
        if shim.name in {"NtCreateFile", "NtOpenFile"}:
            handler_arguments = arguments
            returned_value = self._invoke_guest_file_api(shim, arguments, memory, trace)
        elif shim.name == "NtOpenSymbolicLinkObject":
            handler_arguments = arguments
            returned_value = self._invoke_guest_open_symbolic_link_api(
                arguments, memory, trace
            )
        elif shim.name == "NtQuerySymbolicLinkObject":
            handler_arguments = arguments
            returned_value = self._invoke_guest_query_symbolic_link_api(arguments, trace)
        elif shim.name == "NtQueryInformationFile":
            handler_arguments = arguments
            returned_value = self._invoke_guest_query_information_file_api(arguments, trace)
        elif shim.name == "NtReadFile":
            handler_arguments = arguments
            returned_value = self._invoke_guest_read_file_api(arguments, memory, trace)
        elif shim.name == "NtSetInformationFile":
            handler_arguments = arguments
            returned_value = self._invoke_guest_set_information_file_api(arguments, memory, trace)
        elif shim.name == "NtWriteFile":
            handler_arguments = arguments
            returned_value = self._invoke_guest_write_file_api(arguments, memory, trace)
        else:
            handler_arguments = arguments[:handler_argument_count]
            try:
                returned_value = shim.handler(*handler_arguments)
            except (TypeError, XboxRuntimeError) as exc:
                trace.add(
                    None,
                    "runtime_abi_error",
                    target=target,
                    target_hex=_hex32(target),
                    shim_name=shim.name,
                    arguments=[_hex32(argument) for argument in arguments],
                    handler_arguments=[
                        _hex32(argument) for argument in handler_arguments
                    ],
                    error=str(exc),
                )
                raise RuntimeAbiBridgeError(
                    f"runtime ABI call to {shim.name} failed: {exc}"
                ) from exc

        memory_writes = self._apply_guest_side_effects(
            shim, arguments, returned_value, memory, trace
        )
        return_kind, eax = _apply_return_value(cpu, returned_value)
        stack_cleanup_bytes = guest_argument_count * 4
        if stack_cleanup_bytes:
            _prepare_stdcall_return(cpu, memory, stack_cleanup_bytes)
        invocation = RuntimeAbiInvocation(
            target_address=target,
            shim_name=shim.name,
            ordinal=shim.ordinal,
            subsystem=shim.subsystem,
            arguments=arguments,
            handler_arguments=handler_arguments,
            return_kind=return_kind,
            eax=eax,
            stack_cleanup_bytes=stack_cleanup_bytes,
            result=_runtime_abi_result_for_summary(returned_value),
            memory_writes=tuple(memory_writes),
        )
        self._invocations.append(invocation)
        trace.add(None, "runtime_abi_call", **invocation.to_dict())
        return RuntimeAbiResult(invocation, returned_value)

    def summary(self) -> dict[str, Any]:
        subsystem_counts = Counter(shim.subsystem for shim in self._by_target.values())
        return {
            "registered_target_count": len(self._by_target),
            "registered_subsystem_counts": dict(sorted(subsystem_counts.items())),
            "invocation_count": len(self._invocations),
            "invocations": [invocation.to_dict() for invocation in self._invocations],
        }

    def _guest_argument_count_for(self, shim: RuntimeShim) -> int:
        return min(
            GUEST_ARGUMENT_COUNT_OVERRIDES.get(
                shim.name, self._handler_argument_count_for(shim)
            ),
            self.max_guest_arguments,
        )

    def _handler_argument_count_for(self, shim: RuntimeShim) -> int:
        signature = inspect.signature(shim.handler)
        count = 0
        for parameter in signature.parameters.values():
            if parameter.kind in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            }:
                count += 1
            elif parameter.kind == inspect.Parameter.VAR_POSITIONAL:
                break
        return min(count, self.max_guest_arguments)

    def _invoke_guest_file_api(
        self,
        shim: RuntimeShim,
        arguments: tuple[int, ...],
        memory: SparseMemory,
        trace: ExecutionTrace,
    ) -> dict[str, Any]:
        if shim.name == "NtOpenFile":
            (
                file_handle_address,
                desired_access,
                object_attributes_address,
                io_status_block_address,
                share_access,
                open_options,
            ) = arguments[:6]
            create_disposition = None
            create_options = open_options
        else:
            (
                file_handle_address,
                desired_access,
                object_attributes_address,
                io_status_block_address,
                _allocation_size,
                _file_attributes,
                share_access,
                create_disposition,
                create_options,
                _ea_buffer,
                _ea_length,
            ) = arguments[:11]
        decoded = _decode_guest_object_path(memory, object_attributes_address)
        mode = _guest_file_mode(desired_access, create_disposition)
        if decoded is None:
            result: dict[str, Any] = {
                "status": XboxStatus.INVALID_PARAMETER,
                "handle": None,
                "guest_path": None,
                "decode_error": "object name not decoded",
            }
        else:
            result = self.runtime.filesystem.open_file(decoded["guest_path"], mode)
            result.update(decoded)
        result.update(
            {
                "file_handle_address": file_handle_address,
                "desired_access": desired_access,
                "object_attributes_address": object_attributes_address,
                "io_status_block_address": io_status_block_address,
                "share_access": share_access,
                "create_disposition": create_disposition,
                "create_options": create_options,
                "mode": mode,
            }
        )
        trace.add(
            None,
            "runtime_file_api",
            shim_name=shim.name,
            guest_path=result.get("guest_path"),
            status=result.get("status"),
            status_hex=_hex32(result.get("status", 0)),
            file_handle_address=file_handle_address,
            file_handle_address_hex=_hex32(file_handle_address),
            object_attributes_address=object_attributes_address,
            object_attributes_address_hex=_hex32(object_attributes_address),
        )
        return result

    def _invoke_guest_open_symbolic_link_api(
        self,
        arguments: tuple[int, ...],
        memory: SparseMemory,
        trace: ExecutionTrace,
    ) -> dict[str, Any]:
        (
            link_handle_address,
            object_attributes_address,
        ) = arguments[:2]
        decoded = _decode_guest_object_path(memory, object_attributes_address)
        if decoded is None:
            result: dict[str, Any] = {
                "status": XboxStatus.INVALID_PARAMETER,
                "handle": None,
                "guest_path": None,
                "decode_error": "object name not decoded",
            }
        else:
            result = self.runtime.nt_open_symbolic_link_object(decoded["guest_path"])
            result.update(decoded)
        result.update(
            {
                "link_handle_address": link_handle_address,
                "desired_access": None,
                "object_attributes_address": object_attributes_address,
            }
        )
        trace.add(
            None,
            "runtime_symbolic_link_api",
            shim_name="NtOpenSymbolicLinkObject",
            guest_path=result.get("guest_path"),
            target=result.get("target"),
            status=result.get("status"),
            status_hex=_hex32(result.get("status", 0)),
            link_handle_address=link_handle_address,
            link_handle_address_hex=_hex32(link_handle_address),
            object_attributes_address=object_attributes_address,
            object_attributes_address_hex=_hex32(object_attributes_address),
        )
        return result

    def _invoke_guest_query_symbolic_link_api(
        self,
        arguments: tuple[int, ...],
        trace: ExecutionTrace,
    ) -> dict[str, Any]:
        (
            handle,
            link_target_address,
            returned_length_address,
        ) = arguments[:3]
        result = self.runtime.nt_query_symbolic_link_object(handle)
        result.update(
            {
                "handle": handle,
                "link_target_address": link_target_address,
                "returned_length_address": returned_length_address,
            }
        )
        trace.add(
            None,
            "runtime_symbolic_link_api",
            shim_name="NtQuerySymbolicLinkObject",
            handle=handle,
            handle_hex=_hex32(handle),
            link_target_address=link_target_address,
            link_target_address_hex=_hex32(link_target_address),
            returned_length_address=returned_length_address,
            returned_length_address_hex=_hex32(returned_length_address),
            target=result.get("target"),
            status=result.get("status"),
            status_hex=_hex32(result.get("status", 0)),
        )
        return result

    def _invoke_guest_query_information_file_api(
        self,
        arguments: tuple[int, ...],
        trace: ExecutionTrace,
    ) -> dict[str, Any]:
        (
            handle,
            io_status_block_address,
            file_information_address,
            length,
            file_information_class,
        ) = arguments[:5]
        result = self.runtime.filesystem.query_file_handle_information(handle)
        result.update(
            {
                "handle": handle,
                "io_status_block_address": io_status_block_address,
                "file_information_address": file_information_address,
                "requested_length": length,
                "file_information_class": file_information_class,
            }
        )
        trace.add(
            None,
            "runtime_file_query_api",
            shim_name="NtQueryInformationFile",
            handle=handle,
            handle_hex=_hex32(handle),
            io_status_block_address=io_status_block_address,
            io_status_block_address_hex=_hex32(io_status_block_address),
            file_information_address=file_information_address,
            file_information_address_hex=_hex32(file_information_address),
            requested_length=length,
            file_information_class=file_information_class,
            status=result.get("status"),
            status_hex=_hex32(result.get("status", 0)),
            file_size=result.get("size", 0),
        )
        return result

    def _invoke_guest_set_information_file_api(
        self,
        arguments: tuple[int, ...],
        memory: SparseMemory,
        trace: ExecutionTrace,
    ) -> dict[str, Any]:
        (
            handle,
            io_status_block_address,
            file_information_address,
            length,
            file_information_class,
        ) = arguments[:5]
        payload = memory.read(file_information_address, length) if file_information_address and length else b""
        result = self.runtime.filesystem.set_file_handle_information(
            handle,
            file_information_class,
            payload,
        )
        result.update(
            {
                "handle": handle,
                "io_status_block_address": io_status_block_address,
                "file_information_address": file_information_address,
                "requested_length": length,
                "file_information_class": file_information_class,
            }
        )
        trace.add(
            None,
            "runtime_file_set_information_api",
            shim_name="NtSetInformationFile",
            handle=handle,
            handle_hex=_hex32(handle),
            io_status_block_address=io_status_block_address,
            io_status_block_address_hex=_hex32(io_status_block_address),
            file_information_address=file_information_address,
            file_information_address_hex=_hex32(file_information_address),
            requested_length=length,
            file_information_class=file_information_class,
            status=result.get("status"),
            status_hex=_hex32(result.get("status", 0)),
            position=result.get("position"),
            bytes_consumed=result.get("bytes_consumed", 0),
        )
        return result

    def _invoke_guest_read_file_api(
        self,
        arguments: tuple[int, ...],
        memory: SparseMemory,
        trace: ExecutionTrace,
    ) -> dict[str, Any]:
        (
            handle,
            event_handle,
            apc_routine,
            apc_context,
            io_status_block_address,
            buffer_address,
            length,
            byte_offset_address,
        ) = arguments[:8]
        offset = _read_guest_byte_offset(memory, byte_offset_address)
        result = self.runtime.nt_read_file(handle, length, offset)
        result.update(
            {
                "handle": handle,
                "event_handle": event_handle,
                "apc_routine": apc_routine,
                "apc_context": apc_context,
                "io_status_block_address": io_status_block_address,
                "buffer_address": buffer_address,
                "requested_length": length,
                "byte_offset_address": byte_offset_address,
                "byte_offset": offset,
            }
        )
        trace.add(
            None,
            "runtime_file_io_api",
            shim_name="NtReadFile",
            handle=handle,
            handle_hex=_hex32(handle),
            io_status_block_address=io_status_block_address,
            io_status_block_address_hex=_hex32(io_status_block_address),
            buffer_address=buffer_address,
            buffer_address_hex=_hex32(buffer_address),
            requested_length=length,
            byte_offset=offset,
            status=result.get("status"),
            status_hex=_hex32(result.get("status", 0)),
            bytes_read=result.get("bytes_read", 0),
        )
        return result

    def _invoke_guest_write_file_api(
        self,
        arguments: tuple[int, ...],
        memory: SparseMemory,
        trace: ExecutionTrace,
    ) -> dict[str, Any]:
        (
            handle,
            event_handle,
            apc_routine,
            apc_context,
            io_status_block_address,
            buffer_address,
            length,
            byte_offset_address,
        ) = arguments[:8]
        offset = _read_guest_byte_offset(memory, byte_offset_address)
        payload = memory.read(buffer_address, length) if buffer_address and length else b""
        result = self.runtime.nt_write_file(handle, payload, offset)
        result.update(
            {
                "handle": handle,
                "event_handle": event_handle,
                "apc_routine": apc_routine,
                "apc_context": apc_context,
                "io_status_block_address": io_status_block_address,
                "buffer_address": buffer_address,
                "requested_length": length,
                "byte_offset_address": byte_offset_address,
                "byte_offset": offset,
            }
        )
        trace.add(
            None,
            "runtime_file_io_api",
            shim_name="NtWriteFile",
            handle=handle,
            handle_hex=_hex32(handle),
            io_status_block_address=io_status_block_address,
            io_status_block_address_hex=_hex32(io_status_block_address),
            buffer_address=buffer_address,
            buffer_address_hex=_hex32(buffer_address),
            requested_length=length,
            byte_offset=offset,
            status=result.get("status"),
            status_hex=_hex32(result.get("status", 0)),
            bytes_written=result.get("bytes_written", 0),
        )
        return result

    def _apply_guest_side_effects(
        self,
        shim: RuntimeShim,
        arguments: tuple[int, ...],
        returned_value: Any,
        memory: SparseMemory,
        trace: ExecutionTrace,
    ) -> list[dict[str, Any]]:
        if not isinstance(returned_value, dict):
            return []
        writes: list[dict[str, Any]] = []
        if shim.name == "PsCreateSystemThreadEx":
            handle = returned_value.get("handle")
            if isinstance(handle, int) and arguments and arguments[0]:
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "thread_handle",
                        arguments[0],
                        handle,
                    )
                )
            thread_id = returned_value.get("thread_id", handle)
            if isinstance(thread_id, int) and len(arguments) >= 5 and arguments[4]:
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "thread_id",
                        arguments[4],
                        thread_id,
                    )
                )
            return writes
        if shim.name == "ExQueryNonVolatileSetting" and len(arguments) >= 5:
            setting_type = returned_value.get("setting_type")
            if isinstance(setting_type, int) and arguments[1]:
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "setting_type",
                        arguments[1],
                        setting_type,
                    )
                )
            required_length = returned_value.get("required_length")
            if isinstance(required_length, int) and arguments[4]:
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "result_length",
                        arguments[4],
                        required_length,
                    )
                )
            value = returned_value.get("value")
            if returned_value.get("status") == 0 and arguments[2] and arguments[3]:
                if isinstance(value, int) and arguments[3] >= 4:
                    writes.append(
                        _write_runtime_out_u32(
                            memory,
                            trace,
                            shim.name,
                            "setting_value",
                            arguments[2],
                            value,
                        )
                    )
                elif isinstance(value, bytes):
                    payload = value[: arguments[3]]
                    memory.write(arguments[2], payload)
                    writes.append(
                        {
                            "shim_name": shim.name,
                            "label": "setting_value",
                            "address": arguments[2],
                            "address_hex": _hex32(arguments[2]),
                            "size": len(payload),
                        }
                    )
            return writes
        if shim.name == "RtlInitAnsiString" and len(arguments) >= 2:
            destination_address = arguments[0]
            source_address = arguments[1]
            if destination_address:
                source_bytes = (
                    _read_guest_c_string(memory, source_address)
                    if source_address
                    else b""
                )
                length = min(len(source_bytes), 0xFFFF)
                maximum_length = min(length + 1 if source_address else 0, 0xFFFF)
                payload = (
                    length.to_bytes(2, "little")
                    + maximum_length.to_bytes(2, "little")
                    + _u32(source_address).to_bytes(4, "little")
                )
                memory.write(destination_address, payload)
                write = {
                    "shim_name": shim.name,
                    "label": "ansi_string",
                    "address": destination_address,
                    "address_hex": _hex32(destination_address),
                    "length": length,
                    "maximum_length": maximum_length,
                    "buffer": source_address,
                    "buffer_hex": _hex32(source_address),
                }
                trace.add(
                    None,
                    "runtime_abi_memory_write",
                    shim_name=shim.name,
                    label="ansi_string",
                    memory_address=destination_address,
                    memory_address_hex=_hex32(destination_address),
                    length=length,
                    maximum_length=maximum_length,
                    buffer=source_address,
                    buffer_hex=_hex32(source_address),
                )
                writes.append(write)
            return writes
        if shim.name == "NtQueryInformationFile":
            status = returned_value.get("status")
            file_information_address = returned_value.get("file_information_address")
            requested_length = returned_value.get("requested_length", 0)
            payload = _guest_file_information_payload(returned_value, requested_length)
            if (
                returned_value.get("status") == XboxStatus.SUCCESS
                and isinstance(file_information_address, int)
                and file_information_address
                and payload
            ):
                memory.write(file_information_address, payload)
                write = {
                    "shim_name": shim.name,
                    "label": "file_information",
                    "address": file_information_address,
                    "address_hex": _hex32(file_information_address),
                    "size": len(payload),
                    "file_size": returned_value.get("size", 0),
                    "file_information_class": returned_value.get("file_information_class"),
                }
                trace.add(
                    None,
                    "runtime_abi_memory_write",
                    shim_name=shim.name,
                    label="file_information",
                    memory_address=file_information_address,
                    memory_address_hex=_hex32(file_information_address),
                    size=len(payload),
                    file_size=returned_value.get("size", 0),
                    file_information_class=returned_value.get("file_information_class"),
                )
                writes.append(write)
            io_status_block_address = returned_value.get("io_status_block_address")
            if isinstance(status, int) and isinstance(io_status_block_address, int) and io_status_block_address:
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "io_status",
                        io_status_block_address,
                        status,
                    )
                )
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "io_information",
                        _u32(io_status_block_address + 4),
                        len(payload) if returned_value.get("status") == XboxStatus.SUCCESS else 0,
                    )
                )
            return writes
        if shim.name == "NtReadFile":
            status = returned_value.get("status")
            data = returned_value.get("data")
            buffer_address = returned_value.get("buffer_address")
            bytes_read = returned_value.get(
                "bytes_read", len(data) if isinstance(data, bytes) else 0
            )
            if (
                isinstance(data, bytes)
                and data
                and isinstance(buffer_address, int)
                and buffer_address
            ):
                payload = data[: bytes_read if isinstance(bytes_read, int) else len(data)]
                memory.write(buffer_address, payload)
                write = {
                    "shim_name": shim.name,
                    "label": "read_buffer",
                    "address": buffer_address,
                    "address_hex": _hex32(buffer_address),
                    "size": len(payload),
                }
                trace.add(
                    None,
                    "runtime_abi_memory_write",
                    shim_name=shim.name,
                    label="read_buffer",
                    memory_address=buffer_address,
                    memory_address_hex=_hex32(buffer_address),
                    size=len(payload),
                )
                writes.append(write)
            io_status_block_address = returned_value.get("io_status_block_address")
            if isinstance(status, int) and isinstance(io_status_block_address, int) and io_status_block_address:
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "io_status",
                        io_status_block_address,
                        status,
                    )
                )
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "io_information",
                        _u32(io_status_block_address + 4),
                        bytes_read if isinstance(bytes_read, int) else 0,
                    )
                )
            return writes
        if shim.name == "NtSetInformationFile":
            status = returned_value.get("status")
            bytes_consumed = returned_value.get("bytes_consumed", 0)
            io_status_block_address = returned_value.get("io_status_block_address")
            if isinstance(status, int) and isinstance(io_status_block_address, int) and io_status_block_address:
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "io_status",
                        io_status_block_address,
                        status,
                    )
                )
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "io_information",
                        _u32(io_status_block_address + 4),
                        bytes_consumed if isinstance(bytes_consumed, int) else 0,
                    )
                )
            return writes
        if shim.name == "NtWriteFile":
            status = returned_value.get("status")
            bytes_written = returned_value.get("bytes_written", 0)
            io_status_block_address = returned_value.get("io_status_block_address")
            if isinstance(status, int) and isinstance(io_status_block_address, int) and io_status_block_address:
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "io_status",
                        io_status_block_address,
                        status,
                    )
                )
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "io_information",
                        _u32(io_status_block_address + 4),
                        bytes_written if isinstance(bytes_written, int) else 0,
                    )
                )
            return writes
        if shim.name == "NtOpenSymbolicLinkObject":
            handle = returned_value.get("handle")
            link_handle_address = returned_value.get("link_handle_address")
            if isinstance(handle, int) and isinstance(link_handle_address, int) and link_handle_address:
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "symbolic_link_handle",
                        link_handle_address,
                        handle,
                    )
                )
            return writes
        if shim.name == "NtQuerySymbolicLinkObject":
            status = returned_value.get("status")
            target = returned_value.get("target")
            link_target_address = returned_value.get("link_target_address")
            written_length = 0
            if (
                status == XboxStatus.SUCCESS
                and isinstance(target, str)
                and isinstance(link_target_address, int)
                and link_target_address
            ):
                write = _write_guest_ansi_string_payload(
                    memory,
                    trace,
                    shim.name,
                    "symbolic_link_target",
                    link_target_address,
                    target.encode("ascii", errors="replace"),
                )
                if write is not None:
                    written_length = write["length"]
                    writes.append(write)
            returned_length_address = returned_value.get("returned_length_address")
            if isinstance(returned_length_address, int) and returned_length_address:
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "returned_length",
                        returned_length_address,
                        written_length,
                    )
                )
            return writes
        if shim.name in {"NtCreateFile", "NtOpenFile"}:
            status = returned_value.get("status")
            handle = returned_value.get("handle")
            file_handle_address = returned_value.get("file_handle_address")
            if isinstance(handle, int) and isinstance(file_handle_address, int) and file_handle_address:
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "file_handle",
                        file_handle_address,
                        handle,
                    )
                )
            io_status_block_address = returned_value.get("io_status_block_address")
            if isinstance(status, int) and isinstance(io_status_block_address, int) and io_status_block_address:
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "io_status",
                        io_status_block_address,
                        status,
                    )
                )
                writes.append(
                    _write_runtime_out_u32(
                        memory,
                        trace,
                        shim.name,
                        "io_information",
                        _u32(io_status_block_address + 4),
                        0,
                    )
                )
        return writes


class XbeBackedSparseMemory(SparseMemory):
    """Sparse writable overlay that falls back to mapped XBE image bytes."""

    def __init__(
        self,
        loaded: LoadedXbeImage,
        initial: dict[int, bytes | int] | None = None,
        write_observer: Callable[[int, bytes], None] | None = None,
    ) -> None:
        self._loaded = loaded
        self._write_observer = write_observer
        super().__init__(initial)

    def read(self, address: int, size: int) -> bytes:
        if size < 0:
            raise X86ExecutionError("cannot read a negative size")
        payload = bytearray()
        for offset in range(size):
            current = _u32(address + offset)
            if current in self._data:
                payload.append(self._data[current])
                continue
            try:
                payload.extend(self._loaded.arena.read(current, 1))
            except XbeMemoryAccessError:
                payload.append(0)
        return bytes(payload)

    def write(self, address: int, payload: bytes) -> None:
        super().write(address, payload)
        if self._write_observer is not None:
            self._write_observer(address, payload)

    def write_u32(self, address: int, value: int) -> None:
        payload = struct.pack("<I", _u32(value))
        super().write(address, payload)
        if self._write_observer is not None:
            self._write_observer(address, payload)


class DynamicBlockCache:
    """JSON-backed decoded dynamic-block cache for local probe iteration."""

    VERSION = 1

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.records: dict[str, dict[str, Any]] = {}
        self.load_errors: list[str] = []
        self.hits = 0
        self.misses = 0
        self.stores = 0
        if path is not None and path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("format") == "b2-recomp-dynamic-block-cache":
                    records = data.get("records", {})
                    if isinstance(records, dict):
                        self.records = records
            except (OSError, json.JSONDecodeError) as exc:
                self.load_errors.append(str(exc))

    def key(
        self,
        *,
        image_sha256: str,
        target: int,
        entry_bytes: int,
        max_block_instructions: int,
    ) -> str:
        return ":".join(
            [
                str(self.VERSION),
                image_sha256,
                _hex32(target),
                str(entry_bytes),
                str(max_block_instructions),
            ]
        )

    def get(self, key: str) -> LiftedFunction | None:
        record = self.records.get(key)
        if record is None:
            self.misses += 1
            return None
        try:
            function = _lifted_function_from_cache_record(record)
        except (KeyError, TypeError, ValueError) as exc:
            self.load_errors.append(f"{key}: {exc}")
            self.misses += 1
            return None
        self.hits += 1
        return function

    def put(self, key: str, function: LiftedFunction) -> None:
        if key in self.records:
            return
        self.records[key] = _lifted_function_to_cache_record(function)
        self.stores += 1

    def save(self) -> None:
        if self.path is None or self.stores == 0:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "format": "b2-recomp-dynamic-block-cache",
            "public_safe": False,
            "version": self.VERSION,
            "records": self.records,
        }
        self.path.write_text(
            json.dumps(payload, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def summary(self) -> dict[str, Any]:
        return {
            "enabled": self.path is not None,
            "path": str(self.path) if self.path is not None else None,
            "record_count": len(self.records),
            "hits": self.hits,
            "misses": self.misses,
            "stores": self.stores,
            "load_errors": self.load_errors,
        }


def build_playability_probe_summary(
    xbe_path: Path,
    *,
    extracted_root: Path | None = None,
    entry_bytes: int = DEFAULT_ENTRY_BYTES,
    max_instructions: int = DEFAULT_MAX_INSTRUCTIONS,
    max_block_instructions: int = DEFAULT_MAX_BLOCK_INSTRUCTIONS,
    execute_entry: bool = True,
    max_steps: int = DEFAULT_MAX_STEPS,
    internal_depth: int = DEFAULT_INTERNAL_DEPTH,
    max_blocks: int = DEFAULT_MAX_RECOVERED_BLOCKS,
    max_dynamic_blocks: int = DEFAULT_MAX_DYNAMIC_BLOCKS,
    dynamic_block_cache_path: Path | None = None,
    render_watchpoint_limit: int | None = None,
    save_data_root: Path | None = None,
    dashboard_data_root: Path | None = None,
    cache_data_root: Path | None = None,
) -> dict[str, Any]:
    info = parse_xbe_file(xbe_path)
    image_sha256 = _file_sha256(xbe_path)
    imported_ordinals = [
        import_info["ordinal"] for import_info in info["kernel_imports"]["imports"]
    ]

    runtime = XboxRuntimeShims(
        XboxRuntimeConfig(
            extracted_disc_root=extracted_root,
            save_data_root=save_data_root,
            dashboard_data_root=dashboard_data_root,
            cache_data_root=cache_data_root,
        )
    )
    resolver = ImportResolver()
    runtime.register_kernel_imports(resolver, imported_ordinals)
    loaded = load_xbe_file(xbe_path, resolver=resolver)
    bridge = RuntimeAbiBridge(runtime)
    unresolved = [
        resolution for resolution in loaded.import_resolutions if not resolution.resolved
    ]
    dynamic_block_cache = DynamicBlockCache(dynamic_block_cache_path)

    entry_summary = _recover_entry_summary(
        loaded,
        bridge,
        image_sha256=image_sha256,
        entry_bytes=entry_bytes,
        max_instructions=max_instructions,
        max_block_instructions=max_block_instructions,
        execute_entry=execute_entry,
        max_steps=max_steps,
        internal_depth=internal_depth,
        max_blocks=max_blocks,
        max_dynamic_blocks=max_dynamic_blocks,
        dynamic_block_cache=dynamic_block_cache,
        render_watchpoint_limit=render_watchpoint_limit,
    )
    dynamic_block_cache.save()

    runtime_summary = runtime.summary()
    bridge_summary = bridge.summary()
    asset_io_summary = _asset_io_summary_from_invocations(
        bridge_summary["invocations"]
    )
    return {
        "format": "b2-recomp-playability-probe",
        "public_safe": False,
        "source": {
            "input_file_name": xbe_path.name,
            "input_sha256": image_sha256,
            "title_name": info["certificate"]["title_name"],
            "kernel_import_count": len(imported_ordinals),
        },
        "loader": {
            "entry_point": loaded.entry_point,
            "entry_point_hex": _hex32(loaded.entry_point) if loaded.entry_point else None,
            "patched_import_count": len(loaded.import_resolutions),
            "unresolved_import_count": len(unresolved),
        },
        "runtime": {
            "registered_kernel_shim_count": len(runtime.registered_shims),
            "registered_behavior_counts": runtime_summary["registered_behavior_counts"],
            "registered_subsystem_counts": runtime_summary["registered_subsystem_counts"],
            "open_handles": runtime_summary["open_handles"],
            "open_files": runtime_summary["open_files"],
            "threads": runtime_summary["threads"],
            "input": runtime_summary["input"],
            "audio_initialized": runtime_summary["audio_initialized"],
            "audio_streams": runtime_summary["audio_streams"],
            "clock": runtime_summary["clock"],
            "determinism": runtime_summary["determinism"],
            "deterministic_service_validation": _deterministic_service_validation_summary(
                runtime_summary
            ),
        },
        "runtime_abi_bridge": bridge_summary,
        "asset_io": asset_io_summary,
        "entry_recovery": entry_summary,
        "playability_gaps": _playability_gaps(entry_summary, unresolved),
    }


def summary_json(summary: dict[str, Any], *, pretty: bool = False) -> str:
    return json.dumps(summary, indent=2 if pretty else None, sort_keys=True)


def _recover_entry_summary(
    loaded: LoadedXbeImage,
    bridge: RuntimeAbiBridge,
    *,
    image_sha256: str,
    entry_bytes: int,
    max_instructions: int,
    max_block_instructions: int,
    execute_entry: bool,
    max_steps: int,
    internal_depth: int,
    max_blocks: int,
    max_dynamic_blocks: int,
    dynamic_block_cache: DynamicBlockCache,
    render_watchpoint_limit: int | None,
) -> dict[str, Any]:
    if loaded.entry_point is None:
        return {"status": "missing_entry_point"}

    try:
        code = loaded.arena.read(loaded.entry_point, entry_bytes)
        function = lift_x86_function(
            code,
            base_address=loaded.entry_point,
            symbol="xbe_entry",
            max_instructions=max_instructions,
        )
    except (X86DecodeError, XbeMemoryAccessError) as exc:
        return {
            "status": "decode_failed",
            "entry_point": loaded.entry_point,
            "entry_point_hex": _hex32(loaded.entry_point),
            "error": str(exc),
        }

    summary = {
        "status": "decoded",
        "entry_point": loaded.entry_point,
        "entry_point_hex": _hex32(loaded.entry_point),
        **_lifted_function_summary(function, bridge),
    }
    recovery = _recover_internal_functions(
        loaded,
        function,
        bridge,
        entry_bytes=entry_bytes,
        max_block_instructions=max_block_instructions,
        internal_depth=internal_depth,
        max_blocks=max_blocks,
    )
    summary["control_flow_recovery"] = {
        "internal_depth": internal_depth,
        "max_blocks": max_blocks,
        "max_block_instructions": max_block_instructions,
        "decoded_block_count": len(recovery["functions"]),
        "decoded_block_targets": [
            _hex32(function.base_address) for function in recovery["functions"]
        ],
        "decoded_internal_count": len(recovery["functions"]),
        "decoded_internal_targets": [
            _hex32(function.base_address) for function in recovery["functions"]
        ],
        "frontier_count": len(recovery["frontiers"]),
        "frontier_status_counts": dict(
            sorted(Counter(frontier["status"] for frontier in recovery["frontiers"]).items())
        ),
        "truncated": recovery["truncated"],
    }
    summary["frontier_batch"] = recovery["frontiers"]
    summary["internal_handoffs"] = [
        recovered
        for recovered in recovery["summaries"]
        if recovered.get("depth") == 1 and recovered.get("kind") == "direct_call"
    ]
    summary["recovered_internal_functions"] = recovery["summaries"]
    if execute_entry:
        summary["execution"] = _execute_recovered_control_flow_frame(
            loaded,
            function,
            recovery["functions"],
            bridge,
            image_sha256=image_sha256,
            max_steps=max_steps,
            entry_bytes=entry_bytes,
            max_block_instructions=max_block_instructions,
            max_dynamic_blocks=max_dynamic_blocks,
            dynamic_block_cache=dynamic_block_cache,
            render_watchpoint_limit=render_watchpoint_limit,
        )
    else:
        summary["execution"] = {"status": "skipped"}
    return summary


def _recover_internal_functions(
    loaded: LoadedXbeImage,
    entry_function: LiftedFunction,
    bridge: RuntimeAbiBridge,
    *,
    entry_bytes: int,
    max_block_instructions: int,
    internal_depth: int,
    max_blocks: int,
) -> dict[str, Any]:
    if internal_depth <= 0:
        return {
            "functions": [],
            "summaries": [],
            "frontiers": [],
            "truncated": False,
        }

    queue: list[RecoveryWorkItem] = [
        RecoveryWorkItem(target, 1, entry_function.base_address, "direct_call")
        for target in _internal_call_targets(entry_function, bridge)
    ]
    seen = {entry_function.base_address}
    covered_addresses = {instruction.address for instruction in entry_function.instructions}
    functions: list[LiftedFunction] = []
    summaries: list[dict[str, Any]] = []
    frontiers: list[dict[str, Any]] = []
    truncated = False

    while queue:
        item = queue.pop(0)
        if item.target in seen or item.target in covered_addresses:
            continue
        if item.depth > internal_depth:
            frontier = _recovery_frontier("depth_limit", item)
            summaries.append(frontier)
            frontiers.append(frontier)
            seen.add(item.target)
            continue
        if len(functions) >= max_blocks:
            truncated = True
            frontier = _recovery_frontier("recovery_limit", item)
            summaries.append(frontier)
            frontiers.append(frontier)
            seen.add(item.target)
            continue

        seen.add(item.target)
        try:
            code = loaded.arena.read(item.target, entry_bytes)
            function = lift_x86_block(
                code,
                base_address=item.target,
                symbol=f"block_{item.target:08X}",
                max_instructions=max_block_instructions,
            )
        except (X86DecodeError, XbeMemoryAccessError) as exc:
            frontier = _recovery_frontier("decode_failed", item, error=str(exc))
            summaries.append(frontier)
            frontiers.append(frontier)
            continue

        functions.append(function)
        covered_addresses.update(instruction.address for instruction in function.instructions)
        summaries.append(
            {
                "status": "decoded",
                "target": item.target,
                "target_hex": _hex32(item.target),
                "caller": item.caller,
                "caller_hex": _hex32(item.caller),
                "depth": item.depth,
                "kind": item.kind,
                **_lifted_function_summary(function, bridge),
            }
        )
        _enqueue_block_successors(
            queue,
            function,
            bridge,
            item,
            seen=seen,
            covered_addresses=covered_addresses,
        )

    return {
        "functions": functions,
        "summaries": summaries,
        "frontiers": frontiers,
        "truncated": truncated,
    }


def _recovery_frontier(
    status: str,
    item: RecoveryWorkItem,
    *,
    error: str | None = None,
) -> dict[str, Any]:
    frontier: dict[str, Any] = {
        "status": status,
        "target": item.target,
        "target_hex": _hex32(item.target),
        "caller": item.caller,
        "caller_hex": _hex32(item.caller),
        "depth": item.depth,
        "kind": item.kind,
    }
    if error is not None:
        frontier["error"] = error
    return frontier


def _enqueue_block_successors(
    queue: list[RecoveryWorkItem],
    function: LiftedFunction,
    bridge: RuntimeAbiBridge,
    item: RecoveryWorkItem,
    *,
    seen: set[int],
    covered_addresses: set[int],
) -> None:
    for child in _internal_call_targets(function, bridge):
        _enqueue_recovery_target(
            queue,
            child,
            depth=item.depth + 1,
            caller=function.base_address,
            kind="direct_call",
            seen=seen,
            covered_addresses=covered_addresses,
        )
    for branch_target in function.branch_targets:
        _enqueue_recovery_target(
            queue,
            branch_target,
            depth=item.depth,
            caller=function.base_address,
            kind="branch_target",
            seen=seen,
            covered_addresses=covered_addresses,
        )
    fallthrough = _conditional_fallthrough_target(function)
    if fallthrough is not None:
        _enqueue_recovery_target(
            queue,
            fallthrough,
            depth=item.depth,
            caller=function.base_address,
            kind="branch_fallthrough",
            seen=seen,
            covered_addresses=covered_addresses,
        )


def _enqueue_recovery_target(
    queue: list[RecoveryWorkItem],
    target: int,
    *,
    depth: int,
    caller: int,
    kind: str,
    seen: set[int],
    covered_addresses: set[int],
) -> None:
    if target in seen or target in covered_addresses:
        return
    if any(item.target == target for item in queue):
        return
    queue.append(RecoveryWorkItem(target, depth, caller, kind))


def _conditional_fallthrough_target(function: LiftedFunction) -> int | None:
    if not function.instructions:
        return None
    last = function.instructions[-1]
    if last.mnemonic == "jcc":
        return last.next_address
    return None


def _execute_recovered_control_flow_frame(
    loaded: LoadedXbeImage,
    entry_function: LiftedFunction,
    internal_functions: list[LiftedFunction],
    bridge: RuntimeAbiBridge,
    *,
    image_sha256: str,
    max_steps: int,
    entry_bytes: int,
    max_block_instructions: int,
    max_dynamic_blocks: int,
    dynamic_block_cache: DynamicBlockCache,
    render_watchpoint_limit: int | None,
) -> dict[str, Any]:
    state = CpuState.with_registers(
        esp=DEFAULT_STACK_BASE,
        ebp=0,
        esi=0,
        edi=0,
        fs_base=DEFAULT_FS_BASE,
    )
    render_watchpoint = RenderWriteWatchpoint(stop_after=render_watchpoint_limit)
    memory = XbeBackedSparseMemory(
        loaded,
        {DEFAULT_STACK_BASE: BOOT_PROBE_RETURN},
        write_observer=render_watchpoint.observe,
    )
    frame = _merge_lifted_functions(entry_function, internal_functions)
    covered_addresses = {instruction.address for instruction in frame.instructions}
    handlers = bridge.call_handlers()
    dynamic_functions: list[LiftedFunction] = []
    dynamic_summaries: list[dict[str, Any]] = []
    dynamic_frontiers: list[dict[str, Any]] = []
    dynamic_seen: set[int] = set()

    def dynamic_block_loader(target: int) -> LiftedFunction | None:
        if target in dynamic_seen or target in covered_addresses:
            return None
        if not _is_executable_address(loaded, target):
            return None
        item = RecoveryWorkItem(target, 0, target, "dynamic_execution")
        dynamic_seen.add(target)
        if len(dynamic_functions) >= max_dynamic_blocks:
            frontier = _recovery_frontier("recovery_limit", item)
            dynamic_summaries.append(frontier)
            dynamic_frontiers.append(frontier)
            return None
        cache_key = dynamic_block_cache.key(
            image_sha256=image_sha256,
            target=target,
            entry_bytes=entry_bytes,
            max_block_instructions=max_block_instructions,
        )
        cached = dynamic_block_cache.get(cache_key)
        if cached is not None:
            dynamic_functions.append(cached)
            covered_addresses.update(
                instruction.address for instruction in cached.instructions
            )
            dynamic_summaries.append(
                {
                    "status": "cache_hit",
                    "target": target,
                    "target_hex": _hex32(target),
                    "caller": target,
                    "caller_hex": _hex32(target),
                    "depth": 0,
                    "kind": "dynamic_execution",
                    **_lifted_function_summary(cached, bridge),
                }
            )
            return cached
        try:
            code = _read_dynamic_block_window(
                loaded,
                target,
                minimum_size=entry_bytes,
                preferred_size=max(entry_bytes, max_block_instructions * 4),
            )
            function = lift_x86_block(
                code,
                base_address=target,
                symbol=f"dynamic_block_{target:08X}",
                max_instructions=max_block_instructions,
            )
        except (X86DecodeError, XbeMemoryAccessError) as exc:
            frontier = _recovery_frontier("decode_failed", item, error=str(exc))
            dynamic_summaries.append(frontier)
            dynamic_frontiers.append(frontier)
            return None

        dynamic_functions.append(function)
        dynamic_block_cache.put(cache_key, function)
        covered_addresses.update(
            instruction.address for instruction in function.instructions
        )
        dynamic_summaries.append(
            {
                "status": "decoded",
                "target": target,
                "target_hex": _hex32(target),
                "caller": target,
                "caller_hex": _hex32(target),
                "depth": 0,
                "kind": "dynamic_execution",
                **_lifted_function_summary(function, bridge),
            }
        )
        return function

    try:
        result = execute_lifted_function(
            frame,
            state=state,
            memory=memory,
            call_handlers=handlers,
            unhandled_call_handler=_stop_at_internal_call,
            block_loader=dynamic_block_loader,
            max_steps=max_steps,
        )
    except RenderWatchpointStop as exc:
        return {
            "status": "render_watchpoint_stop",
            "render_watchpoint_stream": exc.stream,
            "dynamic_block_cache": dynamic_block_cache.summary(),
            **_dynamic_recovery_summary(
                dynamic_functions,
                dynamic_summaries,
                dynamic_frontiers,
            ),
        }
    except BootProbeStop as exc:
        trace_events = exc.trace.to_list()
        return {
            "status": "blocked_internal_call",
            "target": exc.target,
            "target_hex": _hex32(exc.target),
            "stack_args": [_hex32(argument) for argument in exc.stack_args],
            "state": exc.state.to_dict(),
            "trace_event_count": len(trace_events),
            "executed_internal_targets": _executed_internal_targets(
                trace_events,
                {
                    function.base_address
                    for function in [*internal_functions, *dynamic_functions]
                },
            ),
            **_dynamic_recovery_summary(
                dynamic_functions,
                dynamic_summaries,
                dynamic_frontiers,
            ),
            "dynamic_block_cache": dynamic_block_cache.summary(),
            "render_watchpoint_stream": render_watchpoint.to_stream(),
        }
    except (X86ExecutionError, RuntimeAbiBridgeError) as exc:
        return {
            "status": "execution_failed",
            "error": str(exc),
            **_dynamic_recovery_summary(
                dynamic_functions,
                dynamic_summaries,
                dynamic_frontiers,
            ),
            "dynamic_block_cache": dynamic_block_cache.summary(),
            "render_watchpoint_stream": render_watchpoint.to_stream(),
        }

    returned_to_probe = result.return_address == BOOT_PROBE_RETURN
    trace_events = result.trace.to_list()
    thread_executions: list[dict[str, Any]] = []
    executed_thread_keys: set[int] = set()
    if returned_to_probe:
        while len(thread_executions) < DEFAULT_MAX_GUEST_THREAD_EXECUTIONS:
            next_thread = _next_unexecuted_guest_thread(
                bridge, loaded, executed_thread_keys
            )
            if next_thread is None:
                break
            thread_key, thread = next_thread
            executed_thread_keys.add(thread_key)
            thread_executions.append(
                _execute_guest_thread_start(
                    loaded,
                    entry_function,
                    internal_functions,
                    dynamic_functions,
                    bridge,
                    thread,
                    thread_index=len(thread_executions),
                    memory=memory,
                    handlers=handlers,
                    block_loader=dynamic_block_loader,
                    max_steps=max(max_steps, DEFAULT_MAX_THREAD_STEPS),
                )
            )
    scheduled_threads = _scheduled_guest_threads(bridge, loaded)
    return {
        "status": "returned" if returned_to_probe else "returned_to_guest",
        "return_address": result.return_address,
        "return_address_hex": _hex32(result.return_address)
        if result.return_address is not None
        else None,
        "steps": result.steps,
        "state": result.state.to_dict(),
        "trace_event_count": len(trace_events),
        "executed_internal_targets": _executed_internal_targets(
            trace_events,
            {
                function.base_address
                for function in [*internal_functions, *dynamic_functions]
            },
        ),
        "guest_thread_count": len(scheduled_threads),
        "guest_threads": scheduled_threads,
        "guest_thread_execution_count": len(thread_executions),
        "guest_thread_executions": thread_executions,
        **_dynamic_recovery_summary(
            dynamic_functions,
            dynamic_summaries,
            dynamic_frontiers,
        ),
        "dynamic_block_cache": dynamic_block_cache.summary(),
        "render_watchpoint_stream": render_watchpoint.to_stream(),
    }


def _next_unexecuted_guest_thread(
    bridge: RuntimeAbiBridge,
    loaded: LoadedXbeImage,
    executed_thread_keys: set[int],
) -> tuple[int, dict[str, Any]] | None:
    for thread in _scheduled_guest_threads(bridge, loaded):
        key = thread.get("handle")
        if not isinstance(key, int):
            key = thread["invocation_index"]
        if key in executed_thread_keys:
            continue
        return key, thread
    return None


def _read_dynamic_block_window(
    loaded: LoadedXbeImage,
    target: int,
    *,
    minimum_size: int,
    preferred_size: int,
) -> bytes:
    try:
        return loaded.arena.read(target, preferred_size)
    except XbeMemoryAccessError:
        return loaded.arena.read(target, minimum_size)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _lifted_function_to_cache_record(function: LiftedFunction) -> dict[str, Any]:
    return function.to_dict(include_bytes=False)


def _lifted_function_from_cache_record(record: dict[str, Any]) -> LiftedFunction:
    instructions = tuple(
        _instruction_from_cache_record(instruction)
        for instruction in record.get("instructions", [])
    )
    return LiftedFunction(
        symbol=str(record["symbol"]),
        base_address=int(record["base_address"]),
        code_size=int(record["code_size"]),
        instructions=instructions,
        target_platform=str(record.get("target_platform", "windows")),
        generated_language=str(record.get("generated_language", "c++17")),
        renderer_backend=str(record.get("renderer_backend", "vulkan")),
    )


def _instruction_from_cache_record(record: dict[str, Any]) -> X86Instruction:
    return X86Instruction(
        address=int(record["address"]),
        size=int(record["size"]),
        mnemonic=str(record["mnemonic"]),
        operands=tuple(
            _operand_from_cache_record(operand)
            for operand in record.get("operands", [])
        ),
        bytes_hex=str(record.get("bytes_hex", "")),
        target=record.get("target"),
        condition=record.get("condition"),
        ret_stack_adjust=int(record.get("ret_stack_adjust", 0)),
    )


def _operand_from_cache_record(record: dict[str, Any]) -> Operand:
    return Operand(
        kind=str(record["kind"]),
        size=int(record.get("size", 32)),
        reg=record.get("reg"),
        immediate=record.get("immediate"),
        base=record.get("base"),
        index=record.get("index"),
        scale=int(record.get("scale", 1)),
        displacement=int(record.get("displacement", 0)),
        absolute=record.get("absolute"),
        segment=record.get("segment"),
    )


def _scheduled_guest_threads(
    bridge: RuntimeAbiBridge,
    loaded: LoadedXbeImage,
) -> list[dict[str, Any]]:
    threads: list[dict[str, Any]] = []
    for index, invocation in enumerate(bridge.invocations):
        if invocation.shim_name != "PsCreateSystemThreadEx":
            continue
        result = invocation.result if isinstance(invocation.result, dict) else {}
        start_address = result.get("start_address")
        if not isinstance(start_address, int):
            start_address = 0
        handle = result.get("handle")
        thread = {
            "invocation_index": index,
            "handle": handle if isinstance(handle, int) else None,
            "handle_hex": _hex32(handle) if isinstance(handle, int) else None,
            "start_address": start_address,
            "start_address_hex": _hex32(start_address),
            "start_context1": result.get("start_context1", 0),
            "start_context1_hex": _hex32(result.get("start_context1", 0)),
            "start_context2": result.get("start_context2", 0),
            "start_context2_hex": _hex32(result.get("start_context2", 0)),
            "suspended": bool(result.get("suspended", False)),
            "executable": bool(start_address and _is_executable_address(loaded, start_address)),
        }
        threads.append(thread)
    return threads


def _execute_guest_thread_start(
    loaded: LoadedXbeImage,
    entry_function: LiftedFunction,
    internal_functions: list[LiftedFunction],
    dynamic_functions: list[LiftedFunction],
    bridge: RuntimeAbiBridge,
    thread: dict[str, Any],
    *,
    thread_index: int,
    memory: SparseMemory,
    handlers: dict[int, Callable[[CpuState, SparseMemory, int, ExecutionTrace], None]],
    block_loader: Callable[[int], LiftedFunction | None],
    max_steps: int,
) -> dict[str, Any]:
    start_address = thread["start_address"]
    summary = {
        "thread_index": thread_index,
        "handle": thread.get("handle"),
        "handle_hex": thread.get("handle_hex"),
        "start_address": start_address,
        "start_address_hex": thread["start_address_hex"],
        "start_context1": thread["start_context1"],
        "start_context1_hex": thread["start_context1_hex"],
        "start_context2": thread["start_context2"],
        "start_context2_hex": thread["start_context2_hex"],
    }
    if thread["suspended"]:
        return {**summary, "status": "skipped_suspended"}
    if not start_address:
        return {**summary, "status": "skipped_missing_start"}
    if not thread["executable"] or not _is_executable_address(loaded, start_address):
        return {**summary, "status": "skipped_non_executable_start"}

    stack_base = DEFAULT_THREAD_STACK_BASE + thread_index * THREAD_STACK_STRIDE
    return_sentinel = BOOT_THREAD_RETURN_BASE + thread_index * 4
    memory.write_u32(stack_base, return_sentinel)
    memory.write_u32(stack_base + 4, thread["start_context1"])
    memory.write_u32(stack_base + 8, thread["start_context2"])
    state = CpuState.with_registers(
        esp=stack_base,
        ebp=0,
        esi=0,
        edi=0,
        fs_base=DEFAULT_FS_BASE + (thread_index + 1) * THREAD_STACK_STRIDE,
    )
    frame = _merge_lifted_functions(
        entry_function,
        [*internal_functions, *dynamic_functions],
        symbol=f"guest_thread_{thread_index:02d}_{start_address:08X}",
        base_address=start_address,
    )
    invocation_start = len(bridge.invocations)
    try:
        result = execute_lifted_function(
            frame,
            state=state,
            memory=memory,
            call_handlers=handlers,
            unhandled_call_handler=_stop_at_internal_call,
            block_loader=block_loader,
            max_steps=max_steps,
        )
    except RenderWatchpointStop as exc:
        return {
            **summary,
            "status": "render_watchpoint_stop",
            "trace_event_count": 0,
            "trace_tail": [],
            "render_command_stream": exc.stream,
            "runtime_abi_invocation_count": len(bridge.invocations)
            - invocation_start,
            "executed_internal_targets": [],
        }
    except BootProbeStop as exc:
        trace_events = exc.trace.to_list()
        return {
            **summary,
            "status": "blocked_internal_call",
            "target": exc.target,
            "target_hex": _hex32(exc.target),
            "stack_args": [_hex32(argument) for argument in exc.stack_args],
            "state": exc.state.to_dict(),
            "trace_event_count": len(trace_events),
            "trace_tail": _trace_tail(trace_events),
            "render_command_stream": _recovered_render_command_stream(trace_events),
            "runtime_abi_invocation_count": len(bridge.invocations)
            - invocation_start,
            "executed_internal_targets": _executed_internal_targets(
                trace_events,
                {
                    function.base_address
                    for function in [*internal_functions, *dynamic_functions]
                },
            ),
        }
    except (X86ExecutionError, RuntimeAbiBridgeError) as exc:
        trace_events = exc.trace.to_list() if isinstance(exc, X86ExecutionError) and exc.trace is not None else []
        scheduler_boundary = (
            _scheduler_boundary_from_step_limit(trace_events, exc.state, exc.steps)
            if isinstance(exc, X86ExecutionError)
            else None
        )
        if scheduler_boundary is not None:
            return {
                **summary,
                **scheduler_boundary,
                "trace_event_count": len(trace_events),
                "trace_tail": _trace_tail(trace_events),
                "render_command_stream": _recovered_render_command_stream(trace_events),
                "runtime_abi_invocation_count": len(bridge.invocations)
                - invocation_start,
                "executed_internal_targets": _executed_internal_targets(
                    trace_events,
                    {
                        function.base_address
                        for function in [*internal_functions, *dynamic_functions]
                    },
                ),
            }
        failure: dict[str, Any] = {
            **summary,
            "status": "execution_failed",
            "error": str(exc),
            "runtime_abi_invocation_count": len(bridge.invocations)
            - invocation_start,
        }
        if isinstance(exc, X86ExecutionError) and exc.trace is not None:
            trace_events = exc.trace.to_list()
            failure["trace_event_count"] = len(trace_events)
            failure["trace_tail"] = _trace_tail(trace_events)
            failure["render_command_stream"] = _recovered_render_command_stream(trace_events)
            if exc.state is not None:
                failure["state"] = exc.state.to_dict()
            failure["steps"] = exc.steps
        return failure

    trace_events = result.trace.to_list()
    returned_to_probe = result.return_address == return_sentinel
    return {
        **summary,
        "status": "returned" if returned_to_probe else "returned_to_guest",
        "return_address": result.return_address,
        "return_address_hex": _hex32(result.return_address)
        if result.return_address is not None
        else None,
        "steps": result.steps,
        "state": result.state.to_dict(),
        "trace_event_count": len(trace_events),
        "trace_tail": _trace_tail(trace_events),
        "render_command_stream": _recovered_render_command_stream(trace_events),
        "runtime_abi_invocation_count": len(bridge.invocations) - invocation_start,
        "runtime_abi_invocations": [
            invocation.to_dict() for invocation in bridge.invocations[invocation_start:]
        ],
        "executed_internal_targets": _executed_internal_targets(
            trace_events,
            {
                function.base_address
                for function in [*internal_functions, *dynamic_functions]
            },
        ),
    }


def _recovered_render_command_stream(
    trace_events: list[dict[str, Any]],
    *,
    max_writes: int = 512,
) -> dict[str, Any]:
    writes: list[dict[str, Any]] = []
    mmio_count = 0
    push_buffer_count = 0
    for event in trace_events:
        if event.get("operation") != "memory_write":
            continue
        details = event.get("details", {})
        address = details.get("memory_address")
        value = details.get("value")
        if not isinstance(address, int) or not isinstance(value, int):
            continue
        kind: str | None = None
        offset: int | None = None
        if 0xFED00000 <= address <= 0xFED0FFFF:
            kind = "d3d_mmio"
            offset = address - 0xFED00000
            mmio_count += 1
        elif 0x80000000 <= address <= 0x8000FFFF:
            kind = "d3d_push_buffer"
            offset = address - 0x80000000
            push_buffer_count += 1
        if kind is None:
            continue
        if len(writes) < max_writes:
            size = int(details.get("size", 4))
            writes.append(
                {
                    "sequence": event.get("sequence"),
                    "instruction_address_hex": event.get("address_hex"),
                    "kind": kind,
                    "address": address,
                    "address_hex": _hex32(address),
                    "offset": offset,
                    "offset_hex": _hex32(offset) if offset is not None else None,
                    "value": value,
                    "value_hex": _hex32(value),
                    "size": size,
                }
            )
    return {
        "write_count": mmio_count + push_buffer_count,
        "mmio_write_count": mmio_count,
        "push_buffer_write_count": push_buffer_count,
        "captured_write_count": len(writes),
        "truncated": mmio_count + push_buffer_count > len(writes),
        "writes": writes,
    }


def _scheduler_boundary_from_step_limit(
    trace_events: list[dict[str, Any]],
    state: CpuState | None,
    steps: int | None,
) -> dict[str, Any] | None:
    if not trace_events or state is None or steps is None:
        return None
    for event in reversed(trace_events[-256:]):
        if event.get("operation") != "branch":
            continue
        details = event.get("details", {})
        if not details.get("taken"):
            continue
        target = details.get("target")
        address = event.get("address")
        if not isinstance(target, int) or not isinstance(address, int):
            continue
        if target > address:
            continue
        queue_scan = _scheduler_queue_scan_summary(
            trace_events,
            loop_entry=target,
            state=state,
        )
        return {
            "status": "scheduler_boundary",
            "boundary_kind": "startup_work_queue_scan",
            "owner": "guest_thread_scheduler",
            "loop_entry": target,
            "loop_entry_hex": _hex32(target),
            "loop_branch": address,
            "loop_branch_hex": _hex32(address),
            "steps": steps,
            "state": state.to_dict(),
            "scheduler_queue_scan": queue_scan,
        }
    return None


def _scheduler_queue_scan_summary(
    trace_events: list[dict[str, Any]],
    *,
    loop_entry: int,
    state: CpuState | None = None,
) -> dict[str, Any]:
    labels = {
        loop_entry: "scan_key",
        loop_entry + 0x04: "current_node_key",
        loop_entry + 0x0D: "next_node",
    }
    reads: list[dict[str, Any]] = []
    seen: set[int] = set()
    for event in reversed(trace_events[-512:]):
        if event.get("operation") != "memory_read":
            continue
        instruction_address = event.get("address")
        if instruction_address not in labels or instruction_address in seen:
            continue
        details = event.get("details", {})
        value = details.get("value")
        memory_address = details.get("memory_address")
        reads.append(
            {
                "label": labels[instruction_address],
                "instruction_address": instruction_address,
                "instruction_address_hex": _hex32(instruction_address),
                "memory_address": memory_address,
                "memory_address_hex": _hex32(memory_address)
                if isinstance(memory_address, int)
                else None,
                "value": value,
                "value_hex": _hex32(value) if isinstance(value, int) else None,
            }
        )
        seen.add(instruction_address)
    label_order = {"scan_key": 0, "current_node_key": 1, "next_node": 2}
    reads.sort(key=lambda read: label_order.get(read["label"], 99))
    by_label = {read["label"]: read for read in reads}
    scan_key = by_label.get("scan_key", {}).get("value")
    current_node_key = by_label.get("current_node_key", {}).get("value")
    current_node_key_address = by_label.get("current_node_key", {}).get("memory_address")
    next_node = by_label.get("next_node", {}).get("value")
    next_node_address = by_label.get("next_node", {}).get("memory_address")
    current_node_base = (
        current_node_key_address - 0x08
        if isinstance(current_node_key_address, int)
        else None
    )
    next_pointer_matches_current_node = (
        isinstance(next_node, int)
        and isinstance(current_node_base, int)
        and next_node == current_node_base
    )
    scan_key_matched_current_node = (
        isinstance(scan_key, int)
        and isinstance(current_node_key, int)
        and scan_key == current_node_key
    )
    producer_candidates = _scheduler_producer_write_candidates(
        trace_events,
        scan_key=scan_key if isinstance(scan_key, int) else None,
        watched_addresses=[
            value
            for value in (
                by_label.get("scan_key", {}).get("memory_address"),
                current_node_key_address,
                next_node_address,
            )
            if isinstance(value, int)
        ],
    )
    producer_trace = _scheduler_producer_trace_summary(
        producer_candidates,
        scan_key=scan_key if isinstance(scan_key, int) else None,
        scan_key_source_address=by_label.get("scan_key", {}).get("memory_address"),
        current_node_key_address=current_node_key_address,
        next_node_address=next_node_address,
    )
    node_key_write_values = _unique_hex_values(
        candidate["value"]
        for candidate in producer_candidates
        if candidate.get("memory_address") == current_node_key_address
        and isinstance(candidate.get("value"), int)
    )
    next_node_write_values = _unique_hex_values(
        candidate["value"]
        for candidate in producer_candidates
        if candidate.get("memory_address") == next_node_address
        and isinstance(candidate.get("value"), int)
    )
    state_registers = state.to_dict()["registers"] if state is not None else {}
    return {
        "producer_consumer_hint": "awaiting_work_item_matching_scan_key",
        "observed_read_count": len(reads),
        "scan_key_hex": by_label.get("scan_key", {}).get("value_hex"),
        "scan_key_source_address_hex": by_label.get("scan_key", {}).get(
            "memory_address_hex"
        ),
        "current_node_key_hex": by_label.get("current_node_key", {}).get("value_hex"),
        "next_node_hex": by_label.get("next_node", {}).get("value_hex"),
        "current_node_base_hex": _hex32(current_node_base)
        if current_node_base is not None
        else None,
        "next_pointer_matches_current_node": next_pointer_matches_current_node,
        "scan_key_matched_current_node": scan_key_matched_current_node,
        "awaited_work_item": {
            "key_hex": by_label.get("scan_key", {}).get("value_hex"),
            "status": "not_present_in_observed_scan"
            if not scan_key_matched_current_node
            else "present",
            "observed_node_key_hex": by_label.get("current_node_key", {}).get("value_hex"),
            "observed_node_base_hex": _hex32(current_node_base)
            if current_node_base is not None
            else None,
            "observed_node_self_loop": next_pointer_matches_current_node,
            "loop_esi_hex": state_registers.get("esi"),
            "loop_ebp_hex": state_registers.get("ebp"),
            "loop_edx_hex": state_registers.get("edx"),
            "loop_edi_hex": state_registers.get("edi"),
        },
        "observed_node_key_write_values": node_key_write_values,
        "observed_next_node_write_values": next_node_write_values,
        "producer_candidate_write_count": len(producer_candidates),
        "producer_candidate_writes": producer_candidates,
        "producer_trace": producer_trace,
        "reads": reads,
    }


def _unique_hex_values(values: Any) -> list[str]:
    result: list[str] = []
    for value in values:
        value_hex = _hex32(value)
        if value_hex not in result:
            result.append(value_hex)
    return result


def _scheduler_producer_write_candidates(
    trace_events: list[dict[str, Any]],
    *,
    scan_key: int | None,
    watched_addresses: list[int],
    limit: int = 8,
) -> list[dict[str, Any]]:
    watched = set(watched_addresses)
    candidates: list[dict[str, Any]] = []
    for event in reversed(trace_events):
        if event.get("operation") != "memory_write":
            continue
        details = event.get("details", {})
        address = details.get("memory_address")
        value = details.get("value")
        if not isinstance(address, int) or not isinstance(value, int):
            continue
        reasons: list[str] = []
        if address in watched:
            reasons.append("watched_scheduler_address")
        if scan_key is not None and value == scan_key:
            reasons.append("wrote_scan_key_value")
        if not reasons:
            continue
        candidates.append(
            {
                "sequence": event.get("sequence"),
                "instruction_address": event.get("address"),
                "instruction_address_hex": event.get("address_hex"),
                "memory_address": address,
                "memory_address_hex": _hex32(address),
                "value": value,
                "value_hex": _hex32(value),
                "size": details.get("size", 4),
                "reasons": reasons,
            }
        )
        if len(candidates) >= limit:
            break
    candidates.reverse()
    return candidates


def _scheduler_producer_trace_summary(
    candidates: list[dict[str, Any]],
    *,
    scan_key: int | None,
    scan_key_source_address: Any,
    current_node_key_address: Any,
    next_node_address: Any,
) -> dict[str, Any]:
    scan_key_source = (
        scan_key_source_address if isinstance(scan_key_source_address, int) else None
    )
    current_node_key = (
        current_node_key_address if isinstance(current_node_key_address, int) else None
    )
    next_node = next_node_address if isinstance(next_node_address, int) else None
    grouped: dict[int, dict[str, Any]] = {}
    watched_transitions: dict[int, dict[str, Any]] = {}
    latest_current_node_writer: dict[str, Any] | None = None
    latest_next_node_writer: dict[str, Any] | None = None
    latest_scan_key_source_writer: dict[str, Any] | None = None
    latest_scan_key_value_writer: dict[str, Any] | None = None

    for candidate in candidates:
        address = candidate.get("memory_address")
        instruction = candidate.get("instruction_address")
        value = candidate.get("value")
        if not isinstance(address, int) or not isinstance(instruction, int):
            continue
        roles = _scheduler_candidate_roles(
            candidate,
            scan_key=scan_key,
            scan_key_source_address=scan_key_source,
            current_node_key_address=current_node_key,
            next_node_address=next_node,
        )
        metadata = _scheduler_anchor_metadata(instruction)
        candidate["producer_roles"] = roles
        if metadata:
            candidate["producer_semantic"] = metadata["semantic"]
            candidate["producer_anchor"] = metadata
        transition = watched_transitions.setdefault(
            address,
            {
                "memory_address": address,
                "memory_address_hex": _hex32(address),
                "roles": [],
                "semantics": [],
                "write_count": 0,
                "values_hex": [],
                "latest_sequence": None,
                "latest_instruction_address_hex": None,
            },
        )
        transition["write_count"] += 1
        transition["latest_sequence"] = candidate.get("sequence")
        transition["latest_instruction_address_hex"] = candidate.get(
            "instruction_address_hex"
        )
        for role in roles:
            if role not in transition["roles"]:
                transition["roles"].append(role)
        if metadata and metadata["semantic"] not in transition["semantics"]:
            transition["semantics"].append(metadata["semantic"])
        if isinstance(value, int):
            value_hex = _hex32(value)
            if value_hex not in transition["values_hex"]:
                transition["values_hex"].append(value_hex)

        group = grouped.setdefault(
            instruction,
            {
                "instruction_address": instruction,
                "instruction_address_hex": candidate.get("instruction_address_hex"),
                "write_count": 0,
                "latest_sequence": None,
                "roles": [],
                "semantic": metadata["semantic"] if metadata else "unknown_scheduler_writer",
                "anchor": metadata,
                "watched_addresses_hex": [],
                "values_hex": [],
            },
        )
        group["write_count"] += 1
        group["latest_sequence"] = candidate.get("sequence")
        address_hex = _hex32(address)
        if address_hex not in group["watched_addresses_hex"]:
            group["watched_addresses_hex"].append(address_hex)
        for role in roles:
            if role not in group["roles"]:
                group["roles"].append(role)
        if metadata and group.get("anchor") is None:
            group["anchor"] = metadata
            group["semantic"] = metadata["semantic"]
        if isinstance(value, int):
            value_hex = _hex32(value)
            if value_hex not in group["values_hex"]:
                group["values_hex"].append(value_hex)

        if address == current_node_key:
            latest_current_node_writer = candidate
        if address == next_node:
            latest_next_node_writer = candidate
        if address == scan_key_source:
            latest_scan_key_source_writer = candidate
        if "awaited_key_value_writer" in roles:
            latest_scan_key_value_writer = candidate

    instruction_groups = sorted(
        grouped.values(),
        key=lambda group: (
            int(group["latest_sequence"])
            if isinstance(group.get("latest_sequence"), int)
            else -1
        ),
    )
    transitions = sorted(
        watched_transitions.values(),
        key=lambda transition: transition["memory_address"],
    )
    scan_key_write_count = sum(
        1
        for candidate in candidates
        if "awaited_key_value_writer" in candidate.get("producer_roles", [])
    )
    observed_work_item_key_values = _unique_hex_values(
        candidate["value"]
        for candidate in candidates
        if candidate.get("producer_semantic") == "work_item_key_writer"
        and isinstance(candidate.get("value"), int)
    )
    awaited_key_enqueued = (
        _hex32(scan_key) in observed_work_item_key_values
        if scan_key is not None
        else False
    )
    semantic_counts = Counter(
        str(candidate.get("producer_semantic", "unknown_scheduler_writer"))
        for candidate in candidates
    )
    if latest_scan_key_value_writer is not None:
        status = "awaited_key_seen_in_candidate_writes"
        anchor = _scheduler_recovery_anchor(
            latest_scan_key_value_writer,
            "latest write of awaited scheduler key",
        )
    else:
        status = "awaited_key_not_seen_in_candidate_writes"
        fallback = latest_current_node_writer or latest_next_node_writer or (
            candidates[-1] if candidates else None
        )
        anchor = _scheduler_recovery_anchor(
            fallback,
            "latest watched scheduler slot writer before the wait",
        )
    return {
        "status": status,
        "awaited_key_hex": _hex32(scan_key) if scan_key is not None else None,
        "scan_key_value_write_count": scan_key_write_count,
        "awaited_key_enqueue_observed": awaited_key_enqueued,
        "observed_work_item_key_values": observed_work_item_key_values,
        "key_flow": {
            "consumer_requested_key": {
                "instruction_address_hex": "0x000F2A10",
                "instruction_text": "mov edx, [esp + 0x24]",
                "stack_offset_hex": "0x00000024",
                "register": "edx",
                "value_hex": _hex32(scan_key) if scan_key is not None else None,
            },
            "producer_enqueued_key": {
                "source_instruction_address_hex": "0x000F2A5B",
                "source_instruction_text": "mov ecx, [esp + 0x24]",
                "write_instruction_address_hex": "0x000F2A63",
                "write_instruction_text": "mov [eax + 0x8], ecx",
                "stack_offset_hex": "0x00000024",
                "register": "ecx",
                "observed_values_hex": observed_work_item_key_values,
            },
            "producer_key_source_matches_consumer_offset": True,
            "awaited_key_enqueued_in_observed_trace": awaited_key_enqueued,
            "missing_key_hypothesis": "producer path for awaited key not reached before current scheduler wait"
            if not awaited_key_enqueued
            else "awaited key was produced in the observed bounded trace",
        },
        "producer_semantic_counts": dict(sorted(semantic_counts.items())),
        "queue_node_layout": {
            "key_offset_hex": "0x00000008",
            "next_offset_hex": "0x00000030",
            "inferred_from": [
                "scheduler scan compares [node + 0x8] with requested key",
                "scheduler scan follows [node + 0x30] as next pointer",
                "producer anchor 0x000F2A63 writes [eax + 0x8]",
                "producer anchor 0x000F2AF9 writes [edx + 0x30]",
            ],
        },
        "candidate_instruction_group_count": len(instruction_groups),
        "watched_address_transition_count": len(transitions),
        "latest_scan_key_source_writer": _scheduler_recovery_anchor(
            latest_scan_key_source_writer,
            "latest write to scan-key source address",
        ),
        "latest_current_node_key_writer": _scheduler_recovery_anchor(
            latest_current_node_writer,
            "latest write to observed node key",
        ),
        "latest_next_node_pointer_writer": _scheduler_recovery_anchor(
            latest_next_node_writer,
            "latest write to observed next-node pointer",
        ),
        "next_recovery_anchor": anchor,
        "instruction_groups": instruction_groups,
        "watched_address_transitions": transitions,
    }


def _scheduler_candidate_roles(
    candidate: dict[str, Any],
    *,
    scan_key: int | None,
    scan_key_source_address: int | None,
    current_node_key_address: int | None,
    next_node_address: int | None,
) -> list[str]:
    roles: list[str] = []
    address = candidate.get("memory_address")
    value = candidate.get("value")
    if address == scan_key_source_address:
        roles.append("scan_key_source_writer")
    if address == current_node_key_address:
        roles.append("current_node_key_writer")
    if address == next_node_address:
        roles.append("next_node_pointer_writer")
    if scan_key is not None and value == scan_key:
        roles.append("awaited_key_value_writer")
    return roles


def _scheduler_anchor_metadata(instruction_address: int) -> dict[str, Any] | None:
    metadata = SCHEDULER_PRODUCER_ANCHORS.get(instruction_address)
    if metadata is None:
        return None
    return {
        "instruction_address": instruction_address,
        "instruction_address_hex": _hex32(instruction_address),
        **metadata,
    }


def _scheduler_recovery_anchor(
    candidate: dict[str, Any] | None,
    rationale: str,
) -> dict[str, Any] | None:
    if candidate is None:
        return None
    return {
        "instruction_address": candidate.get("instruction_address"),
        "instruction_address_hex": candidate.get("instruction_address_hex"),
        "sequence": candidate.get("sequence"),
        "memory_address": candidate.get("memory_address"),
        "memory_address_hex": candidate.get("memory_address_hex"),
        "value": candidate.get("value"),
        "value_hex": candidate.get("value_hex"),
        "roles": candidate.get("producer_roles", []),
        "semantic": candidate.get("producer_semantic"),
        "anchor": candidate.get("producer_anchor"),
        "rationale": rationale,
    }


def _trace_tail(trace_events: list[dict[str, Any]], limit: int = 64) -> list[dict[str, Any]]:
    return trace_events[-limit:]


def _dynamic_recovery_summary(
    dynamic_functions: list[LiftedFunction],
    dynamic_summaries: list[dict[str, Any]],
    dynamic_frontiers: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "dynamic_block_count": len(dynamic_functions),
        "dynamic_block_targets": [
            _hex32(function.base_address) for function in dynamic_functions
        ],
        "dynamic_recovered_blocks": dynamic_summaries,
        "dynamic_frontier_count": len(dynamic_frontiers),
        "dynamic_frontier_status_counts": dict(
            sorted(
                Counter(frontier["status"] for frontier in dynamic_frontiers).items()
            )
        ),
        "dynamic_frontiers": dynamic_frontiers,
    }


def _is_executable_address(loaded: LoadedXbeImage, address: int) -> bool:
    return any(
        region.contains(address) and "execute" in region.permissions
        for region in loaded.arena.regions
    )


def _merge_lifted_functions(
    entry_function: LiftedFunction,
    internal_functions: list[LiftedFunction],
    *,
    symbol: str = "recovered_control_flow_frame",
    base_address: int | None = None,
) -> LiftedFunction:
    instructions = []
    seen_addresses: set[int] = set()
    for function in [entry_function, *internal_functions]:
        for instruction in function.instructions:
            if instruction.address in seen_addresses:
                continue
            seen_addresses.add(instruction.address)
            instructions.append(instruction)
    instructions.sort(key=lambda instruction: instruction.address)
    if instructions:
        code_size = max(instruction.next_address for instruction in instructions) - min(
            instruction.address for instruction in instructions
        )
    else:
        code_size = 0
    return LiftedFunction(
        symbol=symbol,
        base_address=entry_function.base_address
        if base_address is None
        else base_address,
        code_size=code_size,
        instructions=tuple(instructions),
    )


def _lifted_function_summary(
    function: LiftedFunction,
    bridge: RuntimeAbiBridge,
) -> dict[str, Any]:
    return {
        "instruction_count": function.instruction_count,
        "code_size": function.code_size,
        "call_targets": [_hex32(target) for target in function.call_targets],
        "branch_targets": [_hex32(target) for target in function.branch_targets],
        "runtime_call_targets": [
            _hex32(target) for target in function.call_targets if bridge.has_target(target)
        ],
        "internal_call_targets": [
            _hex32(target)
            for target in _internal_call_targets(function, bridge)
        ],
        "first_instruction": function.instructions[0].text()
        if function.instructions
        else None,
        "last_instruction": function.instructions[-1].text()
        if function.instructions
        else None,
    }


def _internal_call_targets(
    function: LiftedFunction,
    bridge: RuntimeAbiBridge,
) -> list[int]:
    targets: list[int] = []
    for target in function.call_targets:
        if bridge.has_target(target) or target in targets:
            continue
        targets.append(target)
    return targets


def _executed_internal_targets(
    trace_events: list[dict[str, Any]],
    internal_function_targets: set[int],
) -> list[str]:
    targets: list[str] = []
    for event in trace_events:
        if event.get("operation") != "call":
            continue
        target = event.get("details", {}).get("target")
        if target not in internal_function_targets:
            continue
        target_hex = _hex32(target)
        if target_hex not in targets:
                targets.append(target_hex)
    return targets


def _asset_io_summary_from_invocations(
    invocations: list[dict[str, Any]],
) -> dict[str, Any]:
    edges: list[dict[str, Any]] = []
    file_handles: dict[int, dict[str, Any]] = {}
    symbolic_link_handles: dict[int, dict[str, Any]] = {}

    for index, invocation in enumerate(invocations):
        shim_name = invocation.get("shim_name")
        result = invocation.get("result")
        if shim_name in {"NtCreateFile", "NtOpenFile"} and isinstance(result, dict):
            status = _coerce_int(result.get("status"))
            handle = _coerce_int(result.get("handle"))
            edge = _asset_edge_base(index, invocation, result, "file_open")
            edge.update(
                {
                    "operation": "file_open"
                    if status == XboxStatus.SUCCESS
                    else "file_probe",
                    "guest_path": result.get("guest_path"),
                    "root_kind": result.get("root_kind"),
                    "mode": result.get("mode"),
                    "handle": handle,
                    "handle_hex": _hex32(handle) if handle is not None else None,
                    "create_disposition": result.get("create_disposition"),
                    "create_options": result.get("create_options"),
                    "desired_access_hex": _hex32(_coerce_int(result.get("desired_access")) or 0),
                }
            )
            if handle is not None and status == XboxStatus.SUCCESS:
                file_handles[handle] = edge
            edges.append(edge)
        elif shim_name == "NtReadFile" and isinstance(result, dict):
            handle = _coerce_int(result.get("handle"))
            opened = file_handles.get(handle, {})
            edge = _asset_edge_base(index, invocation, result, "file_read")
            edge.update(
                {
                    "operation": "file_read",
                    "guest_path": opened.get("guest_path"),
                    "root_kind": opened.get("root_kind"),
                    "handle": handle,
                    "handle_hex": _hex32(handle) if handle is not None else None,
                    "requested_length": result.get("requested_length"),
                    "bytes_read": result.get("bytes_read", 0),
                    "byte_offset": result.get("byte_offset"),
                    "data_elided": bool(result.get("data_elided", False)),
                }
            )
            edges.append(edge)
        elif shim_name == "NtQueryInformationFile" and isinstance(result, dict):
            handle = _coerce_int(result.get("handle"))
            opened = file_handles.get(handle, {})
            edge = _asset_edge_base(index, invocation, result, "file_query_information")
            edge.update(
                {
                    "operation": "file_query_information",
                    "guest_path": result.get("guest_path") or opened.get("guest_path"),
                    "root_kind": opened.get("root_kind"),
                    "handle": handle,
                    "handle_hex": _hex32(handle) if handle is not None else None,
                    "file_information_class": result.get("file_information_class"),
                    "size": result.get("size"),
                    "position": result.get("position"),
                }
            )
            edges.append(edge)
        elif shim_name == "NtSetInformationFile" and isinstance(result, dict):
            handle = _coerce_int(result.get("handle"))
            opened = file_handles.get(handle, {})
            edge = _asset_edge_base(index, invocation, result, "file_set_information")
            edge.update(
                {
                    "operation": "file_set_position"
                    if result.get("file_information_class") == 14
                    else "file_set_information",
                    "guest_path": result.get("guest_path") or opened.get("guest_path"),
                    "root_kind": opened.get("root_kind"),
                    "handle": handle,
                    "handle_hex": _hex32(handle) if handle is not None else None,
                    "file_information_class": result.get("file_information_class"),
                    "position": result.get("position"),
                    "bytes_consumed": result.get("bytes_consumed", 0),
                }
            )
            edges.append(edge)
        elif shim_name == "NtOpenSymbolicLinkObject" and isinstance(result, dict):
            status = _coerce_int(result.get("status"))
            handle = _coerce_int(result.get("handle"))
            edge = _asset_edge_base(index, invocation, result, "symbolic_link_open")
            edge.update(
                {
                    "operation": "symbolic_link_open",
                    "guest_path": result.get("guest_path"),
                    "handle": handle,
                    "handle_hex": _hex32(handle) if handle is not None else None,
                    "target": result.get("target"),
                }
            )
            if handle is not None and status == XboxStatus.SUCCESS:
                symbolic_link_handles[handle] = edge
            edges.append(edge)
        elif shim_name == "NtQuerySymbolicLinkObject" and isinstance(result, dict):
            handle = _coerce_int(result.get("handle"))
            opened = symbolic_link_handles.get(handle, {})
            edge = _asset_edge_base(index, invocation, result, "symbolic_link_query")
            edge.update(
                {
                    "operation": "symbolic_link_query",
                    "guest_path": result.get("name") or opened.get("guest_path"),
                    "handle": handle,
                    "handle_hex": _hex32(handle) if handle is not None else None,
                    "target": result.get("target"),
                }
            )
            edges.append(edge)
        elif shim_name == "NtClose":
            handle = _first_invocation_argument(invocation)
            if handle in file_handles:
                opened = file_handles.pop(handle)
                status = _coerce_int(invocation.get("result"))
                edge = _asset_edge_base(index, invocation, {"status": status}, "file_close")
                edge.update(
                    {
                        "operation": "file_close",
                        "guest_path": opened.get("guest_path"),
                        "root_kind": opened.get("root_kind"),
                        "handle": handle,
                        "handle_hex": _hex32(handle),
                    }
                )
                edges.append(edge)
            elif handle in symbolic_link_handles:
                opened = symbolic_link_handles.pop(handle)
                status = _coerce_int(invocation.get("result"))
                edge = _asset_edge_base(
                    index,
                    invocation,
                    {"status": status},
                    "symbolic_link_close",
                )
                edge.update(
                    {
                        "operation": "symbolic_link_close",
                        "guest_path": opened.get("guest_path"),
                        "handle": handle,
                        "handle_hex": _hex32(handle),
                        "target": opened.get("target"),
                    }
                )
                edges.append(edge)

    status_counts = Counter(edge["status_label"] for edge in edges)
    root_kind_counts = Counter(
        edge.get("root_kind") for edge in edges if edge.get("root_kind")
    )
    operation_counts = Counter(edge["operation"] for edge in edges)
    observed_paths: list[str] = []
    for edge in edges:
        path = edge.get("guest_path")
        if isinstance(path, str) and path not in observed_paths:
            observed_paths.append(path)

    dashupdate_sequence = _dashupdate_asset_sequence(edges)
    symbolic_link_sequence = _symbolic_link_sequence(edges, "\\??\\D:")
    next_edge = _next_asset_edge_after_sequences(
        edges,
        dashupdate_sequence,
        symbolic_link_sequence,
    )
    dashboard_cache_assessment = _dashboard_cache_probe_assessment(edges, next_edge)
    return {
        "format": "b2-recomp-asset-io-summary",
        "public_safe": False,
        "edge_count": len(edges),
        "operation_counts": dict(sorted(operation_counts.items())),
        "status_counts": dict(sorted(status_counts.items())),
        "root_kind_counts": dict(sorted(root_kind_counts.items())),
        "observed_paths": observed_paths,
        "dashupdate_sequence": dashupdate_sequence,
        "symbolic_link_sequence": symbolic_link_sequence,
        "next_edge_after_dashupdate_and_d_link": next_edge,
        "dashboard_cache_probe_assessment": dashboard_cache_assessment,
        "edges": edges,
    }


def _asset_edge_base(
    index: int,
    invocation: dict[str, Any],
    result: dict[str, Any],
    edge_kind: str,
) -> dict[str, Any]:
    status = _coerce_int(result.get("status"))
    return {
        "invocation_index": index,
        "shim_name": invocation.get("shim_name"),
        "edge_kind": edge_kind,
        "status": status,
        "status_hex": _hex32(status) if status is not None else None,
        "status_label": _ntstatus_label(status),
    }


def _dashupdate_asset_sequence(edges: list[dict[str, Any]]) -> dict[str, Any]:
    path = "d:\\dashupdate.xbe"
    sequence = [
        edge
        for edge in edges
        if isinstance(edge.get("guest_path"), str)
        and edge["guest_path"].casefold() == path
    ]
    operations = [edge["operation"] for edge in sequence]
    read_edges = [edge for edge in sequence if edge["operation"] == "file_read"]
    close_edges = [edge for edge in sequence if edge["operation"] == "file_close"]
    complete = (
        "file_open" in operations
        and len(read_edges) >= 2
        and "file_query_information" in operations
        and "file_set_position" in operations
        and bool(close_edges)
        and all(edge.get("status") == XboxStatus.SUCCESS for edge in sequence)
    )
    return {
        "guest_path": path,
        "complete": complete,
        "edge_count": len(sequence),
        "read_count": len(read_edges),
        "bytes_read_total": sum(int(edge.get("bytes_read") or 0) for edge in read_edges),
        "operations": operations,
        "first_invocation_index": sequence[0]["invocation_index"] if sequence else None,
        "close_invocation_index": close_edges[-1]["invocation_index"]
        if close_edges
        else None,
    }


def _symbolic_link_sequence(
    edges: list[dict[str, Any]],
    guest_path: str,
) -> dict[str, Any]:
    sequence = [
        edge
        for edge in edges
        if isinstance(edge.get("guest_path"), str)
        and edge["guest_path"].casefold() == guest_path.casefold()
    ]
    operations = [edge["operation"] for edge in sequence]
    query_edges = [
        edge for edge in sequence if edge["operation"] == "symbolic_link_query"
    ]
    close_edges = [
        edge for edge in sequence if edge["operation"] == "symbolic_link_close"
    ]
    target = query_edges[-1].get("target") if query_edges else None
    complete = (
        "symbolic_link_open" in operations
        and bool(query_edges)
        and bool(close_edges)
        and target == "\\Device\\Cdrom0"
        and all(edge.get("status") == XboxStatus.SUCCESS for edge in sequence)
    )
    return {
        "guest_path": guest_path,
        "complete": complete,
        "edge_count": len(sequence),
        "operations": operations,
        "target": target,
        "query_invocation_index": query_edges[-1]["invocation_index"]
        if query_edges
        else None,
        "close_invocation_index": close_edges[-1]["invocation_index"]
        if close_edges
        else None,
    }


def _next_asset_edge_after_sequences(
    edges: list[dict[str, Any]],
    dashupdate_sequence: dict[str, Any],
    symbolic_link_sequence: dict[str, Any],
) -> dict[str, Any] | None:
    completed_indices = [
        index
        for index in (
            dashupdate_sequence.get("close_invocation_index"),
            symbolic_link_sequence.get("close_invocation_index")
            or symbolic_link_sequence.get("query_invocation_index"),
        )
        if isinstance(index, int)
    ]
    if (
        not dashupdate_sequence.get("complete")
        or not symbolic_link_sequence.get("complete")
        or not completed_indices
    ):
        return None
    boundary = max(completed_indices)
    for edge in edges:
        if edge["invocation_index"] > boundary:
            return dict(edge)
    return None


def _dashboard_cache_probe_assessment(
    edges: list[dict[str, Any]],
    next_edge: dict[str, Any] | None,
) -> dict[str, Any]:
    relevant = [
        edge
        for edge in edges
        if edge.get("root_kind") in {"dashboard", "cache"}
    ]
    clean_not_found = [
        edge
        for edge in relevant
        if edge.get("status_label") == "not_found"
        and edge.get("operation") == "file_probe"
    ]
    unexpected = [
        edge
        for edge in relevant
        if edge.get("status_label") not in {"not_found", "success"}
    ]
    successful = [
        edge for edge in relevant if edge.get("status_label") == "success"
    ]
    observed_paths: list[str] = []
    for edge in relevant:
        path = edge.get("guest_path")
        if isinstance(path, str) and path not in observed_paths:
            observed_paths.append(path)
    if unexpected:
        status = "unexpected_dashboard_or_cache_status"
    elif successful:
        status = "configured_dashboard_or_cache_root_used"
    elif relevant and len(relevant) == len(clean_not_found):
        status = "deterministic_not_found_preserved_for_observed_probes"
    else:
        status = "no_dashboard_or_cache_probe_observed"
    return {
        "status": status,
        "dashboard_probe_count": sum(
            1 for edge in relevant if edge.get("root_kind") == "dashboard"
        ),
        "cache_probe_count": sum(
            1 for edge in relevant if edge.get("root_kind") == "cache"
        ),
        "clean_not_found_probe_count": len(clean_not_found),
        "successful_probe_count": len(successful),
        "unexpected_status_count": len(unexpected),
        "observed_paths": observed_paths,
        "next_post_link_edge_root_kind": next_edge.get("root_kind")
        if isinstance(next_edge, dict)
        else None,
        "next_post_link_edge_status": next_edge.get("status_label")
        if isinstance(next_edge, dict)
        else None,
        "requires_configured_roots_for_observed_edges": bool(successful),
        "later_gameplay_requirement": "unproven_until_execution_advances_past_current_scheduler_boundary",
    }


def _deterministic_service_validation_summary(
    runtime_summary: dict[str, Any],
) -> dict[str, Any]:
    trace_events = runtime_summary.get("trace", [])
    filesystem_reads = [
        event
        for event in trace_events
        if event.get("subsystem") == "filesystem"
        and event.get("operation") == "read_file"
    ]
    filesystem_writes = [
        event
        for event in trace_events
        if event.get("subsystem") == "filesystem"
        and event.get("operation") == "write_file"
    ]
    input_ports = runtime_summary.get("input", {}).get("ports", {})
    audio_streams = runtime_summary.get("audio_streams", [])
    return {
        "streaming": {
            "read_event_count": len(filesystem_reads),
            "bytes_read_total": sum(
                int(event.get("details", {}).get("bytes_read") or 0)
                for event in filesystem_reads
            ),
            "deterministic_latency_100ns_total": sum(
                int(event.get("details", {}).get("deterministic_latency_100ns") or 0)
                for event in filesystem_reads
            ),
            "open_stream_count": sum(
                1 for file in runtime_summary.get("open_files", []) if file.get("streaming")
            ),
        },
        "save_data": {
            "write_event_count": len(filesystem_writes),
            "bytes_written_total": sum(
                int(event.get("details", {}).get("bytes_written") or 0)
                for event in filesystem_writes
            ),
            "open_save_file_count": sum(
                1 for file in runtime_summary.get("open_files", []) if file.get("save_data")
            ),
            "open_cache_file_count": sum(
                1 for file in runtime_summary.get("open_files", []) if file.get("cache_data")
            ),
        },
        "audio": {
            "initialized": bool(runtime_summary.get("audio_initialized", False)),
            "stream_count": len(audio_streams),
            "submitted_buffer_count": sum(
                int(stream.get("submitted_buffer_count") or 0)
                for stream in audio_streams
            ),
            "queued_bytes": sum(int(stream.get("queued_bytes") or 0) for stream in audio_streams),
            "played_bytes": sum(int(stream.get("played_bytes") or 0) for stream in audio_streams),
        },
        "input_latency": {
            "sequence": runtime_summary.get("input", {}).get("sequence", 0),
            "poll_count": sum(
                int(port.get("poll_count") or 0)
                for port in input_ports.values()
                if isinstance(port, dict)
            ),
            "max_last_latency_samples": max(
                (
                    int(port.get("last_latency_samples") or 0)
                    for port in input_ports.values()
                    if isinstance(port, dict)
                ),
                default=0,
            ),
        },
        "clock": runtime_summary.get("clock", {}),
        "determinism": runtime_summary.get("determinism", {}),
    }


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value:
        return int(value, 0)
    return None


def _first_invocation_argument(invocation: dict[str, Any]) -> int | None:
    arguments = invocation.get("arguments")
    if not isinstance(arguments, list) or not arguments:
        return None
    return _coerce_int(arguments[0])


def _ntstatus_label(status: int | None) -> str:
    labels = {
        XboxStatus.SUCCESS: "success",
        XboxStatus.NO_SUCH_FILE: "not_found",
        XboxStatus.OBJECT_NAME_NOT_FOUND: "not_found",
        XboxStatus.END_OF_FILE: "end_of_file",
        XboxStatus.INVALID_HANDLE: "invalid_handle",
        XboxStatus.INVALID_PARAMETER: "invalid_parameter",
        XboxStatus.ACCESS_DENIED: "access_denied",
    }
    return labels.get(status, "unknown")


def _stop_at_internal_call(
    cpu: CpuState,
    memory: SparseMemory,
    target: int,
    trace: ExecutionTrace,
) -> None:
    stack_args = _read_stack_arguments(memory, cpu.get_register("esp"), 8)
    trace.add(
        None,
        "boot_internal_call_gap",
        target=target,
        target_hex=_hex32(target),
        stack_args=[_hex32(argument) for argument in stack_args],
    )
    raise BootProbeStop(target=target, stack_args=stack_args, state=cpu, trace=trace)


def _playability_gaps(
    entry_summary: dict[str, Any],
    unresolved_imports: list[Any],
) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    if unresolved_imports:
        gaps.append(
            {
                "area": "imports",
                "status": "unresolved_imports",
                "count": len(unresolved_imports),
            }
        )
    if entry_summary.get("status") == "decode_failed":
        gaps.append(
            {
                "area": "instruction_coverage",
                "status": "entry_decode_failed",
                "error": entry_summary.get("error"),
            }
        )
    execution = entry_summary.get("execution", {})
    if execution.get("status") == "blocked_internal_call":
        gaps.append(
            {
                "area": "control_flow",
                "status": "internal_call_needs_recovery",
                "target_hex": execution.get("target_hex"),
            }
        )
    elif execution.get("status") == "execution_failed":
        gaps.append(
            {
                "area": "control_flow",
                "status": "execution_failed",
                "error": execution.get("error"),
            }
        )
    for thread in execution.get("guest_thread_executions", []):
        status = thread.get("status")
        if status == "blocked_internal_call":
            gaps.append(
                {
                    "area": "control_flow",
                    "status": "thread_internal_call_needs_recovery",
                    "thread_index": thread.get("thread_index"),
                    "target_hex": thread.get("target_hex"),
                }
            )
        elif status == "execution_failed":
            gaps.append(
                {
                    "area": "control_flow",
                    "status": "thread_execution_failed",
                    "thread_index": thread.get("thread_index"),
                    "start_address_hex": thread.get("start_address_hex"),
                    "error": thread.get("error"),
                }
            )
    for frontier in execution.get("dynamic_frontiers", []):
        status = frontier.get("status")
        if status == "decode_failed":
            gaps.append(
                {
                    "area": "instruction_coverage",
                    "status": "dynamic_block_decode_failed",
                    "target_hex": frontier.get("target_hex"),
                    "error": frontier.get("error"),
                }
            )
        elif status == "recovery_limit":
            gaps.append(
                {
                    "area": "control_flow",
                    "status": "dynamic_recovery_limit",
                    "target_hex": frontier.get("target_hex"),
                }
            )
    static_frontiers_block_playability = execution.get("status") != "returned"
    for frontier in entry_summary.get("frontier_batch", []):
        if not static_frontiers_block_playability:
            continue
        status = frontier.get("status")
        if status == "decode_failed":
            gaps.append(
                {
                    "area": "instruction_coverage",
                    "status": "block_decode_failed",
                    "target_hex": frontier.get("target_hex"),
                    "error": frontier.get("error"),
                }
            )
        elif status in {"depth_limit", "recovery_limit"}:
            gaps.append(
                {
                    "area": "control_flow",
                    "status": status,
                    "target_hex": frontier.get("target_hex"),
                    "caller_hex": frontier.get("caller_hex"),
                }
            )
    return gaps


def _read_stack_arguments(
    memory: SparseMemory,
    esp: int,
    count: int,
) -> tuple[int, ...]:
    return tuple(memory.read_u32(_u32(esp + 4 + index * 4)) for index in range(count))


def _read_guest_byte_offset(memory: SparseMemory, address: int) -> int | None:
    if not address:
        return None
    low = memory.read_u32(address)
    high = memory.read_u32(_u32(address + 4))
    value = (high << 32) | low
    if value & (1 << 63):
        value -= 1 << 64
    if value < 0:
        return None
    return min(value, 0x7FFFFFFF)


def _guest_file_mode(desired_access: int, create_disposition: int | None) -> str:
    writable = bool(desired_access & (GENERIC_WRITE | FILE_WRITE_DATA | FILE_APPEND_DATA))
    if create_disposition in {
        FILE_SUPERSEDE,
        FILE_CREATE,
        FILE_OPEN_IF,
        FILE_OVERWRITE,
        FILE_OVERWRITE_IF,
    }:
        writable = True
    if not writable:
        return "rb"
    if create_disposition in {FILE_SUPERSEDE, FILE_OVERWRITE, FILE_OVERWRITE_IF}:
        return "wb"
    return "ab"


def _guest_file_information_payload(
    file_info: dict[str, Any],
    requested_length: int,
) -> bytes:
    if not isinstance(requested_length, int) or requested_length <= 0:
        return b""
    size = max(int(file_info.get("size", 0) or 0), 0)
    position = max(int(file_info.get("position", 0) or 0), 0)
    allocation_size = (size + 0x7FF) & ~0x7FF if size else 0
    is_directory = 1 if file_info.get("is_directory") else 0
    attributes = 0x10 if is_directory else 0x80
    information_class = file_info.get("file_information_class")
    basic_information = (
        struct.pack("<QQQQI", 0, 0, 0, 0, attributes)
        + b"\x00" * 4
    )
    standard_information = struct.pack(
        "<QQIBB2x",
        allocation_size,
        size,
        1,
        0,
        is_directory,
    )
    if information_class == 4:
        payload = basic_information
    elif information_class == 5:
        payload = standard_information
    elif information_class == 14:
        payload = struct.pack("<Q", position)
    elif information_class == 19:
        payload = struct.pack("<Q", allocation_size)
    elif information_class == 20:
        payload = struct.pack("<Q", size)
    elif information_class == 18:
        payload = (
            basic_information
            + standard_information
            + struct.pack("<Q", 0)
            + struct.pack("<I", 0)
        )
    else:
        payload = standard_information
    return payload[: min(requested_length, len(payload))]


def _runtime_abi_result_for_summary(returned_value: Any) -> Any:
    if not isinstance(returned_value, dict):
        return returned_value
    result = dict(returned_value)
    data = result.pop("data", None)
    if isinstance(data, bytes):
        result.setdefault("bytes_read", len(data))
        result["data_elided"] = True
    return result


def _prepare_stdcall_return(
    cpu: CpuState,
    memory: SparseMemory,
    stack_cleanup_bytes: int,
) -> None:
    esp = cpu.get_register("esp")
    return_address = memory.read_u32(esp)
    adjusted_esp = _u32(esp + stack_cleanup_bytes)
    memory.write_u32(adjusted_esp, return_address)
    cpu.set_register("esp", adjusted_esp)


def _apply_return_value(cpu: CpuState, returned_value: Any) -> tuple[str, int | None]:
    if returned_value is None:
        return "none", None
    if isinstance(returned_value, bool):
        eax = 1 if returned_value else 0
        cpu.set_register("eax", eax)
        return "bool", eax
    if isinstance(returned_value, int):
        eax = _u32(returned_value)
        cpu.set_register("eax", eax)
        return "int", eax
    if isinstance(returned_value, dict):
        status = returned_value.get("status")
        if isinstance(status, int):
            eax = _u32(status)
            cpu.set_register("eax", eax)
            return "status_dict", eax
        handle = returned_value.get("handle")
        if isinstance(handle, int):
            eax = _u32(handle)
            cpu.set_register("eax", eax)
            return "handle_dict", eax
    return type(returned_value).__name__, None


def _write_runtime_out_u32(
    memory: SparseMemory,
    trace: ExecutionTrace,
    shim_name: str,
    label: str,
    address: int,
    value: int,
) -> dict[str, Any]:
    memory.write_u32(address, value)
    write = {
        "shim_name": shim_name,
        "label": label,
        "address": address,
        "address_hex": _hex32(address),
        "value": _u32(value),
        "value_hex": _hex32(value),
    }
    trace.add(
        None,
        "runtime_abi_memory_write",
        shim_name=shim_name,
        label=label,
        memory_address=address,
        memory_address_hex=_hex32(address),
        value=_u32(value),
        value_hex=_hex32(value),
    )
    return write


def _read_guest_c_string(
    memory: SparseMemory,
    address: int,
    *,
    max_bytes: int = 4096,
) -> bytes:
    payload = bytearray()
    for offset in range(max_bytes):
        byte = memory.read(_u32(address + offset), 1)[0]
        if byte == 0:
            break
        payload.append(byte)
    return bytes(payload)


def _decode_guest_object_path(
    memory: SparseMemory,
    object_attributes_address: int,
) -> dict[str, Any] | None:
    if not object_attributes_address:
        return None
    candidates = [object_attributes_address]
    for offset in (0, 4, 8, 12, 16):
        candidate = memory.read_u32(_u32(object_attributes_address + offset))
        if candidate:
            candidates.append(candidate)
    seen: set[int] = set()
    for ansi_string_address in candidates:
        if ansi_string_address in seen:
            continue
        seen.add(ansi_string_address)
        decoded = _read_guest_ansi_string(memory, ansi_string_address)
        if decoded is not None:
            guest_path, length, maximum_length, buffer = decoded
            return {
                "guest_path": guest_path,
                "object_name_address": ansi_string_address,
                "object_name_address_hex": _hex32(ansi_string_address),
                "object_name_length": length,
                "object_name_maximum_length": maximum_length,
                "object_name_buffer": buffer,
                "object_name_buffer_hex": _hex32(buffer),
            }
    return None


def _read_guest_ansi_string(
    memory: SparseMemory,
    ansi_string_address: int,
    *,
    max_bytes: int = 4096,
) -> tuple[str, int, int, int] | None:
    if not ansi_string_address:
        return None
    length = int.from_bytes(memory.read(ansi_string_address, 2), "little")
    maximum_length = int.from_bytes(memory.read(_u32(ansi_string_address + 2), 2), "little")
    buffer = memory.read_u32(_u32(ansi_string_address + 4))
    if length <= 0 or maximum_length < length or maximum_length > max_bytes or not buffer:
        return None
    payload = memory.read(buffer, length)
    try:
        guest_path = payload.decode("ascii")
    except UnicodeDecodeError:
        return None
    if "\\" not in guest_path and ":" not in guest_path and "/" not in guest_path:
        return None
    return guest_path, length, maximum_length, buffer


def _write_guest_ansi_string_payload(
    memory: SparseMemory,
    trace: ExecutionTrace,
    shim_name: str,
    label: str,
    ansi_string_address: int,
    payload: bytes,
) -> dict[str, Any] | None:
    if not ansi_string_address:
        return None
    maximum_length = int.from_bytes(memory.read(_u32(ansi_string_address + 2), 2), "little")
    buffer = memory.read_u32(_u32(ansi_string_address + 4))
    if maximum_length <= 0 or not buffer:
        return None
    truncated = payload[:maximum_length]
    memory.write(buffer, truncated)
    memory.write(ansi_string_address, len(truncated).to_bytes(2, "little"))
    write = {
        "shim_name": shim_name,
        "label": label,
        "address": buffer,
        "address_hex": _hex32(buffer),
        "descriptor_address": ansi_string_address,
        "descriptor_address_hex": _hex32(ansi_string_address),
        "length": len(truncated),
        "maximum_length": maximum_length,
        "truncated": len(truncated) < len(payload),
    }
    trace.add(
        None,
        "runtime_abi_memory_write",
        shim_name=shim_name,
        label=label,
        memory_address=buffer,
        memory_address_hex=_hex32(buffer),
        descriptor_address=ansi_string_address,
        descriptor_address_hex=_hex32(ansi_string_address),
        length=len(truncated),
        maximum_length=maximum_length,
        truncated=len(truncated) < len(payload),
    )
    return write


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, bytes):
        return {"bytes": len(value), "hex": value.hex().upper()}
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, Path):
        return str(value)
    return value


def _hex32(value: int) -> str:
    return f"0x{value & 0xFFFFFFFF:08X}"


def _u32(value: int) -> int:
    return value & 0xFFFFFFFF


def _parse_int(value: str) -> int:
    return int(value, 0)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe recovered boot/control-flow against runtime shim ABI bindings."
    )
    parser.add_argument("xbe", type=Path, help="Path to the local XBE file to probe.")
    parser.add_argument(
        "--extracted-root",
        type=Path,
        help="Optional extracted disc root used by filesystem shims.",
    )
    parser.add_argument(
        "--save-data-root",
        type=Path,
        help="Optional writable save-data root used by filesystem shims.",
    )
    parser.add_argument(
        "--dashboard-root",
        type=Path,
        help="Optional dashboard volume root for C: / Harddisk0 Partition2 probes.",
    )
    parser.add_argument(
        "--cache-root",
        type=Path,
        help="Optional cache volume root for X:, Y:, and Z: probes.",
    )
    parser.add_argument(
        "--entry-bytes",
        type=_parse_int,
        default=DEFAULT_ENTRY_BYTES,
        help="Maximum bytes to read from the entry point for prefix recovery.",
    )
    parser.add_argument(
        "--max-instructions",
        type=int,
        default=DEFAULT_MAX_INSTRUCTIONS,
        help="Maximum entry instructions to lift before failing.",
    )
    parser.add_argument(
        "--max-block-instructions",
        type=int,
        default=DEFAULT_MAX_BLOCK_INSTRUCTIONS,
        help="Maximum instructions to decode per recovered basic block.",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=DEFAULT_MAX_STEPS,
        help="Maximum lifted entry steps to execute.",
    )
    parser.add_argument(
        "--internal-depth",
        type=int,
        default=DEFAULT_INTERNAL_DEPTH,
        help="Maximum direct internal call depth to recover from the entry frame.",
    )
    parser.add_argument(
        "--max-blocks",
        type=int,
        default=DEFAULT_MAX_RECOVERED_BLOCKS,
        help="Maximum recovered basic blocks to batch before reporting frontiers.",
    )
    parser.add_argument(
        "--max-dynamic-blocks",
        type=int,
        default=DEFAULT_MAX_DYNAMIC_BLOCKS,
        help="Maximum executable blocks to decode on demand during execution.",
    )
    parser.add_argument(
        "--dynamic-block-cache",
        type=Path,
        help="Optional ignored JSON cache for dynamically decoded blocks.",
    )
    parser.add_argument(
        "--render-watchpoint-limit",
        type=int,
        help="Stop execution after capturing this many D3D render writes.",
    )
    parser.add_argument(
        "--no-execute-entry",
        action="store_true",
        help="Only decode the entry prefix; skip bounded execution.",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        help="Optional path for the JSON probe summary.",
    )
    parser.add_argument(
        "--render-stream-output",
        type=Path,
        help="Optional path for the first normalized recovered render stream.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    args = parser.parse_args()

    summary = build_playability_probe_summary(
        args.xbe,
        extracted_root=args.extracted_root,
        entry_bytes=args.entry_bytes,
        max_instructions=args.max_instructions,
        max_block_instructions=args.max_block_instructions,
        execute_entry=not args.no_execute_entry,
        max_steps=args.max_steps,
        internal_depth=args.internal_depth,
        max_blocks=args.max_blocks,
        max_dynamic_blocks=args.max_dynamic_blocks,
        dynamic_block_cache_path=args.dynamic_block_cache,
        render_watchpoint_limit=args.render_watchpoint_limit,
        save_data_root=args.save_data_root,
        dashboard_data_root=args.dashboard_root,
        cache_data_root=args.cache_root,
    )
    output = summary_json(summary, pretty=args.pretty)
    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(output + "\n", encoding="utf-8", newline="\n")
    if args.render_stream_output is not None:
        streams = extract_render_streams_from_probe_summary(summary)
        if not streams:
            raise RuntimeError("probe summary did not contain a recovered render stream")
        write_json(args.render_stream_output, streams[0], pretty=args.pretty)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
