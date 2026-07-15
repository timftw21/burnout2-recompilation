"""Cached native execution for resumable lifted x86 functions."""

from __future__ import annotations

import ctypes
import hashlib
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from collections import deque
from pathlib import Path
from typing import Any, Callable, Iterable

from tools.recomp.x86_lifter import (
    CpuFlags,
    CpuState,
    ExecutionTrace,
    LiftedFunction,
    SparseMemory,
    emit_cpp,
)


class NativeExecutorError(RuntimeError):
    pass


def _partition_instructions_by_address(
    instructions: list[Any],
    *,
    maximum_count: int,
    initial_span: int = 0x10000,
) -> list[list[Any]]:
    """Build stable address-range modules that do not shift after frontier growth."""
    if maximum_count <= 0 or initial_span <= 0:
        raise ValueError("native module partition limits must be positive")

    def partition(items: list[Any], range_start: int, span: int) -> list[list[Any]]:
        if len(items) <= maximum_count:
            return [items]
        if span <= 1:
            return [
                items[index : index + maximum_count]
                for index in range(0, len(items), maximum_count)
            ]
        half = max(1, span // 2)
        split = range_start + half
        lower = [instruction for instruction in items if instruction.address < split]
        upper = [instruction for instruction in items if instruction.address >= split]
        result: list[list[Any]] = []
        if lower:
            result.extend(partition(lower, range_start, half))
        if upper:
            result.extend(partition(upper, split, span - half))
        return result

    buckets: dict[int, list[Any]] = {}
    for instruction in sorted(instructions, key=lambda item: item.address):
        bucket_start = instruction.address // initial_span * initial_span
        buckets.setdefault(bucket_start, []).append(instruction)
    modules: list[list[Any]] = []
    for bucket_start, items in sorted(buckets.items()):
        modules.extend(partition(items, bucket_start, initial_span))
    return modules


_NATIVE_DISPATCH_SOURCE = r"""
#include <cstdint>

using B2RNativeEntry = uint32_t (__cdecl *)(void*);

extern "C" __declspec(dllexport) uint32_t b2r_native_dispatch(
    void* context,
    uint32_t target,
    const uint32_t* keys,
    B2RNativeEntry const* entries,
    uint32_t mask,
    const bool* yield_requested,
    const uint32_t* fault_code,
    const uint64_t* steps,
    const uint64_t* step_budget,
    uint64_t* module_call_count
) {
    for (;;) {
        uint32_t slot = (target * 2654435761u) & mask;
        while (entries[slot] != nullptr && keys[slot] != target) {
            slot = (slot + 1u) & mask;
        }
        const B2RNativeEntry entry = entries[slot];
        if (entry == nullptr) {
            return target;
        }
        target = entry(context);
        ++*module_call_count;
        if (*yield_requested || *fault_code ||
            (*step_budget != 0u && *steps >= *step_budget)) {
            return target;
        }
    }
}
"""


class _Flags(ctypes.Structure):
    _fields_ = [(name, ctypes.c_bool) for name in ("cf", "pf", "af", "zf", "sf", "of")]


class _Xmm(ctypes.Structure):
    _fields_ = [("lane", ctypes.c_float * 4)]


class _Context(ctypes.Structure):
    pass


_ReadU32 = ctypes.CFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32)
_WriteU32 = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32)
_ReadU8 = ctypes.CFUNCTYPE(ctypes.c_uint8, ctypes.c_void_p, ctypes.c_uint32)
_WriteU8 = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint8)
_Call = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_Context))
_Observe = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.POINTER(_Context))

_Context._fields_ = [
    *[(name, ctypes.c_uint32) for name in ("eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi", "fs_base")],
    ("timestamp_counter", ctypes.c_uint64),
    ("mxcsr", ctypes.c_uint32),
    ("fpu_status_word", ctypes.c_uint32),
    ("fpu_stack", ctypes.c_float * 8),
    ("fpu_depth", ctypes.c_uint32),
    ("xmm", _Xmm * 8),
    ("mmx", ctypes.c_uint64 * 8),
    ("flags", _Flags),
    ("eip", ctypes.c_uint32),
    ("fault_code", ctypes.c_uint32),
    ("fault_eip", ctypes.c_uint32),
    ("steps", ctypes.c_uint64),
    ("step_budget", ctypes.c_uint64),
    ("yield_requested", ctypes.c_bool),
    ("user", ctypes.c_void_p),
    ("read_u32", _ReadU32),
    ("write_u32", _WriteU32),
    ("read_u8", _ReadU8),
    ("write_u8", _WriteU8),
    ("call", _Call),
    ("observe", _Observe),
    ("read_pages", ctypes.POINTER(ctypes.c_void_p)),
    ("callback_pages", ctypes.POINTER(ctypes.c_uint8)),
    ("dirty_pages", ctypes.POINTER(ctypes.c_uint8)),
    ("dirty_page_indices", ctypes.POINTER(ctypes.c_uint32)),
    ("dirty_page_min_offsets", ctypes.POINTER(ctypes.c_uint16)),
    ("dirty_page_max_offsets", ctypes.POINTER(ctypes.c_uint16)),
    ("dirty_page_count", ctypes.c_uint32),
    ("dirty_page_capacity", ctypes.c_uint32),
]


class NativeResumableExecutor:
    """Compile a lifted frame once, then resume it across host-service boundaries."""

    SYMBOL = "b2r_native_loop"
    MAX_INSTRUCTIONS_PER_MODULE = 3000

    def __init__(
        self,
        function: LiftedFunction,
        *,
        build_dir: Path,
        observer_addresses: Iterable[int] = (),
        callback_addresses: Iterable[int] = (),
        memory_callback_addresses: Iterable[int] = (),
        synchronize_eip_for_callbacks: bool = False,
        module_functions: Iterable[LiftedFunction] | None = None,
        compiler: str | None = None,
    ) -> None:
        compile_profile = "clang-cl-o1-address-stable-v5-page-range-worklists"
        build_dir.mkdir(parents=True, exist_ok=True)
        compiler_path = compiler or shutil.which("clang-cl")
        if compiler_path is None:
            raise NativeExecutorError("clang-cl is required to build the native guest loop")
        instructions = list(function.instructions)
        callback_addresses = {int(address) for address in callback_addresses}
        memory_callback_addresses = {
            int(address) for address in memory_callback_addresses
        }
        module_sources = (
            tuple(module_functions) if module_functions is not None else (function,)
        )
        seen_addresses: set[int] = set()
        chunks: list[list] = []
        for module_source in module_sources:
            unique_instructions = [
                instruction
                for instruction in module_source.instructions
                if instruction.address not in seen_addresses
            ]
            seen_addresses.update(
                instruction.address for instruction in unique_instructions
            )
            chunks.extend(
                _partition_instructions_by_address(
                    unique_instructions,
                    maximum_count=self.MAX_INSTRUCTIONS_PER_MODULE,
                )
            )
        if not chunks:
            chunks = [[]]
        modules: list[tuple[LiftedFunction, str, Path, Path]] = []
        for chunk in chunks:
            module_identity = (
                f"{chunk[0].address:08X}_{chunk[-1].address:08X}"
                if chunk
                else f"{function.base_address:08X}_empty"
            )
            module = LiftedFunction(
                symbol=f"{function.symbol}_native_{module_identity}",
                base_address=chunk[0].address if chunk else function.base_address,
                code_size=(chunk[-1].next_address - chunk[0].address) if chunk else 0,
                instructions=tuple(chunk),
            )
            source = emit_cpp(
                module,
                exported_symbol=self.SYMBOL,
                resumable=True,
                observer_addresses=observer_addresses,
                callback_addresses=callback_addresses
                & {instruction.address for instruction in module.instructions},
                synchronize_eip_for_callbacks=synchronize_eip_for_callbacks,
            )
            digest = hashlib.sha256(
                (compile_profile + "\n" + source).encode("utf-8")
            ).hexdigest()[:16]
            source_path = build_dir / f"native-loop-{digest}.cpp"
            dll_path = build_dir / f"native-loop-{digest}.dll"
            modules.append((module, source, source_path, dll_path))

        def compile_module(item: tuple[LiftedFunction, str, Path, Path]) -> None:
            _module, source, source_path, dll_path = item
            if dll_path.exists():
                return
            source_path.write_text(source, encoding="utf-8", newline="\n")
            completed = subprocess.run(
                [
                    compiler_path,
                    "/nologo",
                    "/std:c++17",
                    "/O1",
                    "/LD",
                    str(source_path),
                    f"/Fe:{dll_path}",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                raise NativeExecutorError(
                    "native guest-loop compilation failed:\n"
                    + (completed.stdout + completed.stderr).strip()
                )
        with ThreadPoolExecutor(max_workers=min(4, len(modules))) as pool:
            list(pool.map(compile_module, modules))

        dispatcher_digest = hashlib.sha256(
            ("clang-cl-o2-native-module-dispatch-v1\n" + _NATIVE_DISPATCH_SOURCE).encode(
                "utf-8"
            )
        ).hexdigest()[:16]
        dispatcher_source_path = build_dir / f"native-dispatch-{dispatcher_digest}.cpp"
        dispatcher_dll_path = build_dir / f"native-dispatch-{dispatcher_digest}.dll"
        if not dispatcher_dll_path.exists():
            dispatcher_source_path.write_text(
                _NATIVE_DISPATCH_SOURCE,
                encoding="utf-8",
                newline="\n",
            )
            completed = subprocess.run(
                [
                    compiler_path,
                    "/nologo",
                    "/std:c++17",
                    "/O2",
                    "/LD",
                    str(dispatcher_source_path),
                    f"/Fe:{dispatcher_dll_path}",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                raise NativeExecutorError(
                    "native module dispatcher compilation failed:\n"
                    + (completed.stdout + completed.stderr).strip()
                )

        self.dll_path = modules[0][3]
        self._base_address = function.base_address
        self._valid_addresses = {instruction.address for instruction in function.instructions}
        # Execution callback targets yield through generated control flow; they
        # do not make every data access sharing the target's 4 KiB code page
        # volatile. Only addresses whose memory semantics change on reads must
        # bypass the native page cache.
        self._callback_pages = {
            int(address) >> 12 for address in memory_callback_addresses
        }
        self._libraries: list[ctypes.CDLL] = []
        self._entries_by_address: dict[int, Callable] = {}
        self.last_run_summary: dict[str, Any] | None = None
        for module, _source, _source_path, dll_path in modules:
            library = ctypes.CDLL(str(dll_path))
            entry = getattr(library, self.SYMBOL)
            entry.argtypes = [ctypes.POINTER(_Context)]
            entry.restype = ctypes.c_uint32
            self._libraries.append(library)
            for instruction in module.instructions:
                self._entries_by_address[instruction.address] = entry
        self._dispatcher_library = ctypes.CDLL(str(dispatcher_dll_path))
        self._dispatcher = getattr(self._dispatcher_library, "b2r_native_dispatch")
        self._dispatcher.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_bool),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
        ]
        self._dispatcher.restype = ctypes.c_uint32
        table_capacity = 2
        while table_capacity < max(1, len(self._entries_by_address)) * 2:
            table_capacity <<= 1
        self._dispatch_keys = (ctypes.c_uint32 * table_capacity)()
        self._dispatch_entries = (ctypes.c_void_p * table_capacity)()
        self._dispatch_mask = table_capacity - 1
        for address, entry in self._entries_by_address.items():
            if address in callback_addresses:
                continue
            slot = (address * 2654435761) & self._dispatch_mask
            while self._dispatch_entries[slot]:
                slot = (slot + 1) & self._dispatch_mask
            self._dispatch_keys[slot] = address
            self._dispatch_entries[slot] = ctypes.cast(entry, ctypes.c_void_p).value

    def run(
        self,
        state: CpuState,
        memory: SparseMemory,
        *,
        call_handlers: dict[int, Callable] | None = None,
        step_observer: Callable | None = None,
        memory_write_observer: Callable[[int, int, int, int, int], None]
        | None = None,
        max_steps: int = 0,
        max_memory_pages: int = 65536,
        slice_steps: int = 0,
        yield_handler: Callable[[CpuState, SparseMemory, int], bool | None] | None = None,
        yield_predicate: Callable[[], bool] | None = None,
    ) -> int:
        """Run until the step budget, an unhandled target, or callback exception."""
        run_started_ns = time.perf_counter_ns()
        performance_counts = {
            "native_module_count": len(self._libraries),
            "native_dispatch_count": 0,
            "native_module_call_count": 0,
            "handler_call_count": 0,
            "slice_yield_count": 0,
            "predicate_yield_count": 0,
            "read_u32_callback_count": 0,
            "read_u8_callback_count": 0,
            "write_u32_callback_count": 0,
            "write_u8_callback_count": 0,
            "observer_callback_count": 0,
            "page_cache_fill_count": 0,
            "dirty_sync_call_count": 0,
            "dirty_page_scan_count": 0,
            "dirty_page_writeback_count": 0,
            "dirty_byte_writeback_count": 0,
            "dirty_page_list_peak": 0,
            "invalidation_call_count": 0,
            "invalidation_page_scan_count": 0,
            "invalidated_page_count": 0,
        }
        performance_timings: dict[str, dict[str, int]] = {}
        handler_timings: dict[int, dict[str, Any]] = {}

        def record_performance(name: str, started_ns: int) -> None:
            elapsed_us = max(0, (time.perf_counter_ns() - started_ns) // 1_000)
            metric = performance_timings.setdefault(
                name,
                {"count": 0, "total_us": 0, "max_us": 0},
            )
            metric["count"] += 1
            metric["total_us"] += elapsed_us
            metric["max_us"] = max(metric["max_us"], elapsed_us)

        def record_handler_performance(
            target: int,
            handler: Callable,
            started_ns: int,
        ) -> None:
            elapsed_us = max(0, (time.perf_counter_ns() - started_ns) // 1_000)
            owner = getattr(handler, "__self__", None)
            owner_name = owner.__class__.__name__ if owner is not None else None
            callable_name = getattr(
                handler,
                "__name__",
                handler.__class__.__name__,
            )
            name = (
                f"{owner_name}.{callable_name}"
                if owner_name is not None
                else str(callable_name)
            )
            metric = handler_timings.setdefault(
                target,
                {
                    "target": target,
                    "target_hex": f"0x{target:08X}",
                    "name": name,
                    "count": 0,
                    "total_us": 0,
                    "max_us": 0,
                },
            )
            metric["count"] += 1
            metric["total_us"] += elapsed_us
            metric["max_us"] = max(metric["max_us"], elapsed_us)
        handlers = call_handlers or {}
        trace = ExecutionTrace(enabled=False)
        context = _context_from_state(state)
        if context.eip == 0:
            context.eip = self._base_address
        callback_error: list[BaseException] = []
        page_table = (ctypes.c_void_p * (1 << 20))()
        callback_pages = (ctypes.c_uint8 * (1 << 20))()
        dirty_pages = (ctypes.c_uint8 * (1 << 20))()
        dirty_page_indices = (ctypes.c_uint32 * (1 << 20))()
        dirty_page_min_offsets = (ctypes.c_uint16 * (1 << 20))()
        dirty_page_max_offsets = (ctypes.c_uint16 * (1 << 20))()
        page_buffers: dict[int, ctypes.Array] = {}
        page_generations: dict[int, int] = {}
        for page in self._callback_pages:
            callback_pages[page] = 1
        consume_changed_pages = getattr(memory, "consume_changed_pages", None)
        if callable(consume_changed_pages):
            # Pre-run writes predate every native cache entry and cannot make
            # one stale.  Establish the worklist boundary before filling it.
            consume_changed_pages()

        def cache_page(address: int) -> None:
            if address >= 0x80000000:
                return
            page = address >> 12
            if callback_pages[page] or page in page_buffers:
                return
            started_ns = time.perf_counter_ns()
            page_address = page << 12
            page_snapshot = getattr(memory, "native_page_snapshot", None)
            if callable(page_snapshot):
                payload = page_snapshot(page_address)
            else:
                payload = SparseMemory.read(memory, page_address, 4096)
            buffer = (ctypes.c_uint8 * 4096).from_buffer_copy(payload)
            page_buffers[page] = buffer
            page_table[page] = ctypes.addressof(buffer)
            page_generations[page] = memory.page_generation(address)
            performance_counts["page_cache_fill_count"] += 1
            record_performance("page_cache_fill", started_ns)

        def invalidate_changed_pages() -> None:
            started_ns = time.perf_counter_ns()
            performance_counts["invalidation_call_count"] += 1
            if callable(consume_changed_pages):
                candidates = consume_changed_pages()
            else:
                # Compatibility fallback for SparseMemory-like test doubles.
                candidates = page_generations.keys()
            performance_counts["invalidation_page_scan_count"] += len(candidates)
            changed = [
                page
                for page in candidates
                if page in page_generations
                and memory.page_generation(page << 12) != page_generations[page]
            ]
            performance_counts["invalidated_page_count"] += len(changed)
            for page in changed:
                page_table[page] = None
                page_buffers.pop(page, None)
                page_generations.pop(page, None)
            record_performance("page_invalidation", started_ns)

        def sync_dirty_pages() -> None:
            started_ns = time.perf_counter_ns()
            performance_counts["dirty_sync_call_count"] += 1
            dirty_page_count = int(context.dirty_page_count)
            performance_counts["dirty_page_scan_count"] += dirty_page_count
            performance_counts["dirty_page_list_peak"] = max(
                performance_counts["dirty_page_list_peak"],
                dirty_page_count,
            )
            candidates = (
                (
                    int(dirty_page_indices[index]),
                    page_buffers.get(int(dirty_page_indices[index])),
                )
                for index in range(dirty_page_count)
            )
            write_native_page_range = getattr(
                memory,
                "write_native_page_range",
                memory.write,
            )
            for page, buffer in candidates:
                if buffer is None or not dirty_pages[page]:
                    continue
                start = int(dirty_page_min_offsets[page])
                end = int(dirty_page_max_offsets[page])
                if not 0 <= start < end <= 4096:
                    raise NativeExecutorError(
                        f"invalid native dirty range {start}:{end} for page 0x{page:05X}"
                    )
                write_native_page_range(
                    (page << 12) + start,
                    bytes(buffer[start:end]),
                )
                dirty_pages[page] = 0
                dirty_page_min_offsets[page] = 0
                dirty_page_max_offsets[page] = 0
                page_generations[page] = memory.page_generation(page << 12)
                performance_counts["dirty_page_writeback_count"] += 1
                performance_counts["dirty_byte_writeback_count"] += end - start
            context.dirty_page_count = 0
            record_performance("dirty_page_sync", started_ns)

        def mark_dirty_page(page: int, offset: int, size: int) -> None:
            end = offset + size
            if dirty_pages[page]:
                dirty_page_min_offsets[page] = min(
                    dirty_page_min_offsets[page], offset
                )
                dirty_page_max_offsets[page] = max(
                    dirty_page_max_offsets[page], end
                )
                return
            dirty_pages[page] = 1
            dirty_page_min_offsets[page] = offset
            dirty_page_max_offsets[page] = end
            index = int(context.dirty_page_count)
            if index >= int(context.dirty_page_capacity):
                raise NativeExecutorError("native dirty-page worklist overflow")
            dirty_page_indices[index] = page
            context.dirty_page_count = index + 1

        def guard(callback: Callable, fallback: int | None = None) -> Callable:
            def wrapped(*args):
                try:
                    return callback(*args)
                except BaseException as exc:  # propagate safely after native code yields
                    callback_error.append(exc)
                    context.yield_requested = True
                    return fallback
            return wrapped

        def read32(_user, address: int) -> int:
            performance_counts["read_u32_callback_count"] += 1
            value = memory.read_u32(address)
            cache_page(address)
            return value

        def read8(_user, address: int) -> int:
            performance_counts["read_u8_callback_count"] += 1
            value = memory.read(address, 1)[0]
            cache_page(address)
            return value

        read_u32 = _ReadU32(guard(read32, 0))
        def check_write(address: int) -> None:
            if (
                max_memory_pages
                and memory.allocated_page_count >= max_memory_pages
                and not memory.has_allocated_page(address)
            ):
                raise NativeExecutorError(
                    f"native guest loop exceeded {max_memory_pages} memory pages "
                    f"while writing 0x{address:08X} at eip 0x{context.eip:08X}"
                )

        def write32(_user, address: int, value: int) -> None:
            performance_counts["write_u32_callback_count"] += 1
            check_write(address)
            if memory_write_observer is not None:
                memory_write_observer(
                    int(context.eip), address, 4, value, int(context.steps)
                )
            page = address >> 12
            if address < 0x80000000 and (address & 0xFFF) <= 0xFFC:
                cache_page(address)
            if page in page_buffers and (address & 0xFFF) <= 0xFFC:
                ctypes.c_uint32.from_address(
                    int(page_table[page]) + (address & 0xFFF)
                ).value = value
                mark_dirty_page(page, address & 0xFFF, 4)
                return
            memory.write_u32(address, value)
            if yield_predicate is not None and yield_predicate():
                context.yield_requested = True

        def write8(_user, address: int, value: int) -> None:
            performance_counts["write_u8_callback_count"] += 1
            check_write(address)
            if memory_write_observer is not None:
                memory_write_observer(
                    int(context.eip), address, 1, value, int(context.steps)
                )
            page = address >> 12
            if address < 0x80000000:
                cache_page(address)
            if page in page_buffers:
                ctypes.c_uint8.from_address(
                    int(page_table[page]) + (address & 0xFFF)
                ).value = value
                mark_dirty_page(page, address & 0xFFF, 1)
                return
            memory.write(address, bytes((value,)))
            if yield_predicate is not None and yield_predicate():
                context.yield_requested = True

        write_u32 = _WriteU32(guard(write32))
        read_u8 = _ReadU8(guard(read8, 0))
        write_u8 = _WriteU8(guard(write8))
        unused_call = _Call(lambda _user, _target, _context: None)

        def observe(_user, context_pointer) -> None:
            started_ns = time.perf_counter_ns()
            performance_counts["observer_callback_count"] += 1
            if step_observer is None:
                record_performance("observer_callback", started_ns)
                return
            try:
                sync_dirty_pages()
                _state_from_context(state, context_pointer.contents)
                step_observer(state, memory, trace, int(context_pointer.contents.steps))
                _update_context(context_pointer.contents, state)
                invalidate_changed_pages()
            finally:
                record_performance("observer_callback", started_ns)

        observe_callback = _Observe(guard(observe))
        context.read_u32 = read_u32
        context.write_u32 = write_u32
        context.read_u8 = read_u8
        context.write_u8 = write_u8
        context.call = unused_call
        context.observe = observe_callback
        context.read_pages = ctypes.cast(page_table, ctypes.POINTER(ctypes.c_void_p))
        context.callback_pages = ctypes.cast(
            callback_pages, ctypes.POINTER(ctypes.c_uint8)
        )
        context.dirty_pages = ctypes.cast(dirty_pages, ctypes.POINTER(ctypes.c_uint8))
        context.dirty_page_indices = ctypes.cast(
            dirty_page_indices, ctypes.POINTER(ctypes.c_uint32)
        )
        context.dirty_page_min_offsets = ctypes.cast(
            dirty_page_min_offsets, ctypes.POINTER(ctypes.c_uint16)
        )
        context.dirty_page_max_offsets = ctypes.cast(
            dirty_page_max_offsets, ctypes.POINTER(ctypes.c_uint16)
        )
        context.dirty_page_count = 0
        context.dirty_page_capacity = len(dirty_page_indices)
        if slice_steps < 0:
            raise NativeExecutorError("slice_steps must not be negative")
        next_slice = slice_steps if slice_steps else 0
        context.step_budget = (
            min(max_steps, next_slice) if max_steps and next_slice else max_steps or next_slice
        )
        yield_requested_pointer = ctypes.cast(
            ctypes.byref(context, _Context.yield_requested.offset),
            ctypes.POINTER(ctypes.c_bool),
        )
        fault_code_pointer = ctypes.cast(
            ctypes.byref(context, _Context.fault_code.offset),
            ctypes.POINTER(ctypes.c_uint32),
        )
        steps_pointer = ctypes.cast(
            ctypes.byref(context, _Context.steps.offset),
            ctypes.POINTER(ctypes.c_uint64),
        )
        step_budget_pointer = ctypes.cast(
            ctypes.byref(context, _Context.step_budget.offset),
            ctypes.POINTER(ctypes.c_uint64),
        )
        module_call_count = ctypes.c_uint64()
        transitions: deque[dict[str, int]] = deque(maxlen=32)

        def finish(reason: str, target: int, dispatch_eip: int) -> int:
            _state_from_context(state, context)
            stack_pointer = int(context.esp)
            elapsed_ns = max(1, time.perf_counter_ns() - run_started_ns)
            elapsed_seconds = elapsed_ns / 1_000_000_000.0
            timing_summary = {
                name: {
                    **metric,
                    "average_us": round(
                        metric["total_us"] / max(1, metric["count"]),
                        3,
                    ),
                }
                for name, metric in sorted(performance_timings.items())
            }
            self.last_run_summary = {
                "reason": reason,
                "target": target,
                "target_hex": f"0x{target:08X}",
                "dispatch_eip": dispatch_eip,
                "dispatch_eip_hex": f"0x{dispatch_eip:08X}",
                "steps": int(context.steps),
                "step_budget": int(context.step_budget),
                "performance": {
                    "elapsed_us": elapsed_ns // 1_000,
                    "steps_per_second": round(
                        int(context.steps) / elapsed_seconds,
                        3,
                    ),
                    **performance_counts,
                    "timings": timing_summary,
                    "hot_paths": [
                        {"name": name, **timing_summary[name]}
                        for name in sorted(
                            timing_summary,
                            key=lambda item: (
                                -timing_summary[item]["total_us"],
                                item,
                            ),
                        )
                    ],
                    "handler_hot_paths": [
                        {
                            **handler_timings[target],
                            "average_us": round(
                                handler_timings[target]["total_us"]
                                / max(1, handler_timings[target]["count"]),
                                3,
                            ),
                        }
                        for target in sorted(
                            handler_timings,
                            key=lambda item: (
                                -handler_timings[item]["total_us"],
                                item,
                            ),
                        )
                    ],
                },
                "registers": {
                    name: f"0x{int(getattr(context, name)):08X}"
                    for name in ("eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi")
                },
                "stack_words": [
                    {
                        "address": (stack_pointer + offset) & 0xFFFFFFFF,
                        "address_hex": f"0x{(stack_pointer + offset) & 0xFFFFFFFF:08X}",
                        "value": memory.read_u32((stack_pointer + offset) & 0xFFFFFFFF),
                        "value_hex": f"0x{memory.read_u32((stack_pointer + offset) & 0xFFFFFFFF):08X}",
                    }
                    for offset in range(0, 64, 4)
                ],
                "transitions": list(transitions),
            }
            return target

        while True:
            performance_counts["native_dispatch_count"] += 1
            dispatch_eip = int(context.eip)
            dispatch_started_ns = time.perf_counter_ns()
            module_call_count.value = 0
            target = int(
                self._dispatcher(
                    ctypes.byref(context),
                    dispatch_eip,
                    self._dispatch_keys,
                    self._dispatch_entries,
                    self._dispatch_mask,
                    yield_requested_pointer,
                    fault_code_pointer,
                    steps_pointer,
                    step_budget_pointer,
                    ctypes.byref(module_call_count),
                )
            )
            performance_counts["native_module_call_count"] += module_call_count.value
            record_performance("native_dispatch", dispatch_started_ns)
            if context.fault_code:
                fault_code = int(context.fault_code)
                fault_eip = int(context.fault_eip)
                finish("guest_arithmetic_fault", fault_eip, dispatch_eip)
                fault_label = {
                    1: "division by zero",
                    2: "division overflow",
                }.get(fault_code, f"arithmetic fault {fault_code}")
                raise NativeExecutorError(
                    f"native guest {fault_label} at 0x{fault_eip:08X} "
                    f"from dispatch 0x{dispatch_eip:08X}"
                )
            transitions.append(
                {
                    "dispatch_eip": dispatch_eip,
                    "target": target,
                    "steps": int(context.steps),
                }
            )
            if callback_error:
                sync_dirty_pages()
                _state_from_context(state, context)
                raise callback_error[0]
            handled_requested_yield = False
            if context.yield_requested:
                performance_counts["predicate_yield_count"] += 1
                sync_dirty_pages()
                _state_from_context(state, context)
                context.yield_requested = False
                if yield_handler is not None:
                    yield_started_ns = time.perf_counter_ns()
                    try:
                        stop_requested = yield_handler(
                            state, memory, int(context.steps)
                        ) is False
                    finally:
                        record_performance("yield_handler", yield_started_ns)
                    if stop_requested:
                        return finish("yield_handler_stop", int(context.eip), dispatch_eip)
                performance_counts["slice_yield_count"] += 1
                _update_context(context, state)
                invalidate_changed_pages()
                handled_requested_yield = True
            if next_slice and context.steps >= next_slice:
                if not handled_requested_yield:
                    sync_dirty_pages()
                    _state_from_context(state, context)
                    if yield_handler is not None:
                        yield_started_ns = time.perf_counter_ns()
                        try:
                            stop_requested = yield_handler(
                                state, memory, int(context.steps)
                            ) is False
                        finally:
                            record_performance("yield_handler", yield_started_ns)
                        if stop_requested:
                            return finish("yield_handler_stop", int(context.eip), dispatch_eip)
                    performance_counts["slice_yield_count"] += 1
                    _update_context(context, state)
                    invalidate_changed_pages()
                if max_steps and context.steps >= max_steps:
                    return finish("step_budget", target, dispatch_eip)
                while next_slice <= context.steps:
                    next_slice += slice_steps
                context.step_budget = min(max_steps, next_slice) if max_steps else next_slice
            if max_steps and context.steps >= max_steps:
                sync_dirty_pages()
                return finish("step_budget", target, dispatch_eip)
            handler = handlers.get(target)
            if handler is None:
                if target in self._valid_addresses:
                    continue
                sync_dirty_pages()
                return finish("unhandled_target", target, dispatch_eip)
            sync_dirty_pages()
            _state_from_context(state, context)
            performance_counts["handler_call_count"] += 1
            handler_started_ns = time.perf_counter_ns()
            try:
                handler(state, memory, target, trace)
            finally:
                record_handler_performance(target, handler, handler_started_ns)
                record_performance("call_handler", handler_started_ns)
            state.eip = memory.read_u32(state.get_register("esp"))
            state.set_register("esp", state.get_register("esp") + 4)
            _update_context(context, state)
            invalidate_changed_pages()


def _context_from_state(state: CpuState) -> _Context:
    context = _Context()
    _update_context(context, state)
    return context


def _update_context(context: _Context, state: CpuState) -> None:
    for name in ("eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi"):
        setattr(context, name, state.get_register(name))
    context.fs_base = state.fs_base
    context.timestamp_counter = state.timestamp_counter
    context.mxcsr = state.mxcsr
    context.fpu_status_word = state.fpu_status_word
    context.fpu_depth = min(len(state.fpu_stack), 8)
    for index in range(8):
        context.fpu_stack[index] = state.fpu_stack[index] if index < len(state.fpu_stack) else 0.0
        lanes = state.get_xmm_register(f"xmm{index}")
        for lane in range(4):
            context.xmm[index].lane[lane] = lanes[lane]
        context.mmx[index] = state.get_mmx_register(f"mm{index}")
    context.flags = _Flags(**state.flags.to_dict())
    context.eip = state.eip


def _state_from_context(state: CpuState, context: _Context) -> None:
    for name in ("eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi"):
        state.set_register(name, getattr(context, name))
    state.fs_base = context.fs_base
    state.timestamp_counter = context.timestamp_counter
    state.mxcsr = context.mxcsr
    state.fpu_status_word = context.fpu_status_word
    state.fpu_stack = [float(context.fpu_stack[index]) for index in range(context.fpu_depth)]
    for index in range(8):
        state.set_xmm_register(f"xmm{index}", context.xmm[index].lane)
        state.set_mmx_register(f"mm{index}", context.mmx[index])
    state.flags = CpuFlags(**{name: bool(getattr(context.flags, name)) for name in ("cf", "pf", "af", "zf", "sf", "of")})
    state.eip = context.eip
