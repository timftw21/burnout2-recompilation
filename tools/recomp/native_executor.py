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

NATIVE_READ_CALLBACK_SAMPLE_INTERVAL = 1024
NATIVE_READ_CALLBACK_HOT_ADDRESS_LIMIT = 32


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
#include <cstring>

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
    uint64_t* module_call_count,
    uint64_t* target_call_counts,
    uint64_t* target_step_counts,
    uint32_t* touched_target_slots,
    uint32_t* touched_target_count
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
        if (target_call_counts[slot] == 0u) {
            touched_target_slots[(*touched_target_count)++] = slot;
        }
        const uint64_t steps_before = *steps;
        target = entry(context);
        ++*module_call_count;
        ++target_call_counts[slot];
        target_step_counts[slot] += *steps - steps_before;
        if (*yield_requested || *fault_code ||
            (*step_budget != 0u && *steps >= *step_budget)) {
            return target;
        }
    }
}

extern "C" __declspec(dllexport) uint32_t b2r_pack_observed_write_records(
    uint8_t* output,
    uint32_t output_capacity,
    const uint32_t* addresses,
    const uint32_t* values,
    const uint8_t* sizes,
    uint32_t count,
    uint32_t range_start,
    uint32_t range_end,
    uint32_t normalized_base
) {
    if (output == nullptr || addresses == nullptr || values == nullptr ||
        sizes == nullptr || count > output_capacity || range_end < range_start) {
        return 0u;
    }
    bool contiguous_u32 = count != 0u;
    uint32_t expected_address = count != 0u ? addresses[0] : 0u;
    for (uint32_t index = 0u; index < count; ++index) {
        const uint32_t address = addresses[index];
        const uint32_t size = sizes[index];
        if ((size != 1u && size != 4u) ||
            address < range_start || address > range_end ||
            size > range_end - address) {
            return 0u;
        }
        contiguous_u32 = contiguous_u32 &&
            size == 4u && address == expected_address;
        expected_address = address + size;
        uint8_t* record = output + static_cast<size_t>(index) * 16u;
        record[0] = 1u;
        record[1] = static_cast<uint8_t>(size);
        record[2] = 0u;
        record[3] = 0u;
        const uint32_t normalized_address =
            normalized_base + (address - range_start);
        std::memcpy(record + 4u, &normalized_address, sizeof(normalized_address));
        std::memset(record + 8u, 0, 8u);
        std::memcpy(record + 8u, &values[index], size);
    }
    return 1u | (contiguous_u32 ? 2u : 0u);
}
"""


class _Flags(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_bool)
        for name in (
            "cf",
            "pf",
            "af",
            "zf",
            "sf",
            "of",
            "df",
            "interrupt_enabled",
        )
    ]


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
    *[
        (name, ctypes.c_uint32)
        for name in (
            "eax",
            "ecx",
            "edx",
            "ebx",
            "esp",
            "ebp",
            "esi",
            "edi",
            "fs_base",
            "cs_selector",
            "gdtr_base",
            "gdtr_limit",
        )
    ],
    ("timestamp_counter", ctypes.c_uint64),
    ("mxcsr", ctypes.c_uint32),
    ("fpu_control_word", ctypes.c_uint32),
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
    ("callback_address_keys", ctypes.POINTER(ctypes.c_uint32)),
    ("callback_address_mask", ctypes.c_uint32),
    ("cache_physical_aliases", ctypes.c_bool),
    ("dirty_pages", ctypes.POINTER(ctypes.c_uint8)),
    ("dirty_page_indices", ctypes.POINTER(ctypes.c_uint32)),
    ("dirty_page_min_offsets", ctypes.POINTER(ctypes.c_uint16)),
    ("dirty_page_max_offsets", ctypes.POINTER(ctypes.c_uint16)),
    ("dirty_page_count", ctypes.c_uint32),
    ("dirty_page_capacity", ctypes.c_uint32),
    ("observed_write_range_start", ctypes.c_uint32),
    ("observed_write_range_end", ctypes.c_uint32),
    ("observed_write_eips", ctypes.POINTER(ctypes.c_uint32)),
    ("observed_write_source_addresses", ctypes.POINTER(ctypes.c_uint32)),
    ("observed_write_addresses", ctypes.POINTER(ctypes.c_uint32)),
    ("observed_write_values", ctypes.POINTER(ctypes.c_uint32)),
    ("observed_write_steps", ctypes.POINTER(ctypes.c_uint64)),
    ("observed_write_sizes", ctypes.POINTER(ctypes.c_uint8)),
    ("observed_write_count", ctypes.c_uint32),
    ("observed_write_capacity", ctypes.c_uint32),
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
        compile_profile = "clang-cl-o2-address-stable-v7-native-write-log"
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
                    "/O2",
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
            ("clang-cl-o2-native-module-dispatch-v2\n" + _NATIVE_DISPATCH_SOURCE).encode(
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
        self._callback_byte_addresses = {
            (int(address) + offset) & 0xFFFFFFFF
            for address in memory_callback_addresses
            for offset in range(4)
        }
        self._callback_pages = {
            address >> 12 for address in self._callback_byte_addresses
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
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
        ]
        self._dispatcher.restype = ctypes.c_uint32
        self._observed_write_packer = getattr(
            self._dispatcher_library,
            "b2r_pack_observed_write_records",
        )
        self._observed_write_packer.argtypes = [
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        self._observed_write_packer.restype = ctypes.c_uint32
        table_capacity = 2
        while table_capacity < max(1, len(self._entries_by_address)) * 2:
            table_capacity <<= 1
        self._dispatch_keys = (ctypes.c_uint32 * table_capacity)()
        self._dispatch_entries = (ctypes.c_void_p * table_capacity)()
        self._dispatch_addresses_by_slot = [0] * table_capacity
        self._dispatch_target_call_counts = (ctypes.c_uint64 * table_capacity)()
        self._dispatch_target_step_counts = (ctypes.c_uint64 * table_capacity)()
        self._dispatch_touched_slots = (ctypes.c_uint32 * table_capacity)()
        self._dispatch_touched_count = ctypes.c_uint32()
        self._dispatch_mask = table_capacity - 1
        for address, entry in self._entries_by_address.items():
            if address in callback_addresses:
                continue
            slot = (address * 2654435761) & self._dispatch_mask
            while self._dispatch_entries[slot]:
                slot = (slot + 1) & self._dispatch_mask
            self._dispatch_keys[slot] = address
            self._dispatch_entries[slot] = ctypes.cast(entry, ctypes.c_void_p).value
            self._dispatch_addresses_by_slot[slot] = address

        page_capacity = 1 << 20
        self._page_table = (ctypes.c_void_p * page_capacity)()
        self._callback_pages_table = (ctypes.c_uint8 * page_capacity)()
        callback_address_capacity = 2
        while callback_address_capacity < max(
            1, len(self._callback_byte_addresses)
        ) * 2:
            callback_address_capacity <<= 1
        self._callback_address_keys = (
            ctypes.c_uint32 * callback_address_capacity
        )(*([0xFFFFFFFF] * callback_address_capacity))
        self._callback_address_mask = callback_address_capacity - 1
        for address in self._callback_byte_addresses:
            slot = (address * 2654435761) & self._callback_address_mask
            while self._callback_address_keys[slot] != 0xFFFFFFFF:
                slot = (slot + 1) & self._callback_address_mask
            self._callback_address_keys[slot] = address
        self._dirty_pages = (ctypes.c_uint8 * page_capacity)()
        self._dirty_page_indices = (ctypes.c_uint32 * page_capacity)()
        self._dirty_page_min_offsets = (ctypes.c_uint16 * page_capacity)()
        self._dirty_page_max_offsets = (ctypes.c_uint16 * page_capacity)()
        self._page_buffers: dict[int, ctypes.Array] = {}
        self._page_generations: dict[int, int] = {}
        self._cache_memory_identity: SparseMemory | None = None
        self._observed_write_capacity = 0
        self._observed_write_eips = (ctypes.c_uint32 * 1)()
        self._observed_write_source_addresses = (ctypes.c_uint32 * 1)()
        self._observed_write_addresses = (ctypes.c_uint32 * 1)()
        self._observed_write_values = (ctypes.c_uint32 * 1)()
        self._observed_write_steps = (ctypes.c_uint64 * 1)()
        self._observed_write_sizes = (ctypes.c_uint8 * 1)()
        self._packed_observed_write_records = (ctypes.c_uint8 * 16)()
        for page in self._callback_pages:
            self._callback_pages_table[page] = 1

    def seed_page_cache_from(
        self,
        previous: "NativeResumableExecutor",
        memory: SparseMemory,
    ) -> int:
        """Reuse clean page snapshots when frontier recovery rebuilds the executor."""
        if previous is self or previous._cache_memory_identity is not memory:
            return 0
        for page in self._page_buffers:
            self._page_table[page] = None
        self._page_buffers.clear()
        self._page_generations.clear()
        for page, buffer in previous._page_buffers.items():
            generation = previous._page_generations.get(page)
            if generation is None or memory.page_generation(page << 12) != generation:
                continue
            self._page_buffers[page] = buffer
            self._page_generations[page] = generation
            self._page_table[page] = ctypes.addressof(buffer)
        self._cache_memory_identity = memory
        return len(self._page_buffers)

    def run(
        self,
        state: CpuState,
        memory: SparseMemory,
        *,
        call_handlers: dict[int, Callable] | None = None,
        step_observer: Callable | None = None,
        memory_write_observer: Callable[[int, int, int, int, int], None]
        | None = None,
        memory_write_batch_observer: Callable[
            [Any, Any, Any, Any, Any, Any, Any, int, int, bool],
            None,
        ]
        | None = None,
        memory_write_batch_address_base: int = 0,
        memory_write_observer_ranges_provider: Callable[
            [], Iterable[tuple[int, int]]
        ]
        | None = None,
        max_steps: int = 0,
        max_memory_pages: int = 65536,
        slice_steps: int = 0,
        yield_handler: Callable[[CpuState, SparseMemory, int], bool | None] | None = None,
        yield_predicate: Callable[[], bool] | None = None,
        call_handler_yield_predicate: Callable[[int], bool] | None = None,
    ) -> int:
        """Run until the step budget, an unhandled target, or callback exception."""
        run_started_ns = time.perf_counter_ns()
        for index in range(int(self._dispatch_touched_count.value)):
            slot = int(self._dispatch_touched_slots[index])
            self._dispatch_target_call_counts[slot] = 0
            self._dispatch_target_step_counts[slot] = 0
        self._dispatch_touched_count.value = 0
        performance_counts = {
            "native_module_count": len(self._libraries),
            "native_dispatch_count": 0,
            "native_module_call_count": 0,
            "handler_call_count": 0,
            "call_handler_yield_count": 0,
            "slice_yield_count": 0,
            "predicate_yield_count": 0,
            "read_u32_callback_count": 0,
            "read_u8_callback_count": 0,
            "exact_read_u32_callback_count": 0,
            "exact_read_u8_callback_count": 0,
            "page_miss_read_u32_callback_count": 0,
            "page_miss_read_u8_callback_count": 0,
            "write_u32_callback_count": 0,
            "write_u8_callback_count": 0,
            "native_observed_write_count": 0,
            "native_observed_write_drain_count": 0,
            "native_observed_write_batch_count": 0,
            "observer_callback_count": 0,
            "page_cache_fill_count": 0,
            "dirty_sync_call_count": 0,
            "dirty_sync_no_work_count": 0,
            "selective_dirty_sync_call_count": 0,
            "selective_dirty_sync_no_match_count": 0,
            "selective_dirty_sync_retained_page_count": 0,
            "selective_dirty_sync_page_writeback_count": 0,
            "dirty_page_scan_count": 0,
            "dirty_page_writeback_count": 0,
            "dirty_byte_writeback_count": 0,
            "dirty_page_list_peak": 0,
            "invalidation_call_count": 0,
            "invalidation_page_scan_count": 0,
            "invalidated_page_count": 0,
            "cached_range_refresh_count": 0,
            "cached_range_refresh_byte_count": 0,
            "cached_range_refresh_no_change_count": 0,
        }
        performance_timings: dict[str, dict[str, int]] = {}
        handler_timings: dict[int, dict[str, Any]] = {}
        read_callback_samples: dict[tuple[str, int], int] = {}

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

        def sample_read_callback(
            kind: str,
            address: int,
            callback_count: int,
            *,
            exact: bool,
        ) -> None:
            if (callback_count - 1) % NATIVE_READ_CALLBACK_SAMPLE_INTERVAL != 0:
                return
            sampled_address = address if exact else address & ~0xFFF
            key = (kind, sampled_address & 0xFFFFFFFF)
            read_callback_samples[key] = read_callback_samples.get(key, 0) + 1

        handlers = call_handlers or {}
        trace = ExecutionTrace(enabled=False)
        context = _context_from_state(state)
        if context.eip == 0:
            context.eip = self._base_address
        callback_error: list[BaseException] = []
        cache_context_started_ns = time.perf_counter_ns()
        cache_context_reused = self._cache_memory_identity is memory
        if not cache_context_reused:
            for page in self._page_buffers:
                self._page_table[page] = None
            self._page_buffers.clear()
            self._page_generations.clear()
            self._cache_memory_identity = memory
        page_table = self._page_table
        callback_pages = self._callback_pages_table
        dirty_pages = self._dirty_pages
        dirty_page_indices = self._dirty_page_indices
        dirty_page_min_offsets = self._dirty_page_min_offsets
        dirty_page_max_offsets = self._dirty_page_max_offsets
        page_buffers = self._page_buffers
        page_generations = self._page_generations
        observed_write_capacity = (
            262_144
            if (
                memory_write_observer is not None
                or memory_write_batch_observer is not None
            )
            and memory_write_observer_ranges_provider is not None
            else 0
        )
        if observed_write_capacity > self._observed_write_capacity:
            self._observed_write_eips = (
                ctypes.c_uint32 * observed_write_capacity
            )()
            self._observed_write_source_addresses = (
                ctypes.c_uint32 * observed_write_capacity
            )()
            self._observed_write_addresses = (
                ctypes.c_uint32 * observed_write_capacity
            )()
            self._observed_write_values = (
                ctypes.c_uint32 * observed_write_capacity
            )()
            self._observed_write_steps = (
                ctypes.c_uint64 * observed_write_capacity
            )()
            self._observed_write_sizes = (
                ctypes.c_uint8 * observed_write_capacity
            )()
            self._packed_observed_write_records = (
                ctypes.c_uint8 * (observed_write_capacity * 16)
            )()
            self._observed_write_capacity = observed_write_capacity
        observed_write_eips = self._observed_write_eips
        observed_write_source_addresses = self._observed_write_source_addresses
        observed_write_addresses = self._observed_write_addresses
        observed_write_values = self._observed_write_values
        observed_write_steps = self._observed_write_steps
        observed_write_sizes = self._observed_write_sizes
        packed_observed_write_records = self._packed_observed_write_records
        performance_counts["native_cache_context_reused"] = cache_context_reused
        record_performance("cache_context_setup", cache_context_started_ns)
        consume_changed_pages = getattr(memory, "consume_changed_pages", None)
        consume_changed_page_ranges = getattr(
            memory,
            "consume_changed_page_ranges",
            None,
        )
        native_cache_address = getattr(memory, "native_cache_address", None)
        native_cacheable_address = getattr(
            memory,
            "native_cacheable_address",
            None,
        )
        native_callback_dependency_addresses = getattr(
            memory,
            "native_memory_callback_dependency_addresses",
            None,
        )
        physical_alias_cache_enabled = bool(
            getattr(memory, "native_cache_physical_aliases", False)
        )
        performance_counts["physical_alias_page_cache_enabled"] = (
            physical_alias_cache_enabled
        )

        def cache_address(address: int) -> int:
            if callable(native_cache_address):
                return int(native_cache_address(address)) & 0xFFFFFFFF
            return int(address) & 0xFFFFFFFF

        def cacheable_address(address: int) -> bool:
            address = cache_address(address)
            if callable(native_cacheable_address):
                return bool(native_cacheable_address(address))
            return address < 0x80000000

        def cache_page(address: int) -> None:
            address = cache_address(address)
            if not cacheable_address(address):
                return
            page = address >> 12
            if page in page_buffers:
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

        def refresh_observed_write_range() -> None:
            context.observed_write_range_start = 0
            context.observed_write_range_end = 0
            if memory_write_observer_ranges_provider is None:
                return
            for range_start, range_end in memory_write_observer_ranges_provider():
                start = max(0, int(range_start))
                end = min(0x80000000, int(range_end))
                if start < end:
                    context.observed_write_range_start = start
                    context.observed_write_range_end = end
                    return

        def invalidate_changed_pages() -> None:
            started_ns = time.perf_counter_ns()
            performance_counts["invalidation_call_count"] += 1
            refresh_observed_write_range()
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

        def callback_dependency_pages(
            address: int,
            size: int,
            *,
            is_write: bool,
        ) -> set[int] | None:
            if not callable(native_callback_dependency_addresses):
                return None
            dependencies = native_callback_dependency_addresses(
                address,
                size,
                is_write=is_write,
            )
            if dependencies is None:
                return None
            return {
                cache_address(dependency) >> 12
                for dependency in dependencies
                if cacheable_address(dependency)
            }

        def sync_dirty_pages(required_pages: set[int] | None = None) -> None:
            performance_counts["dirty_sync_call_count"] += 1
            selective = required_pages is not None
            if selective:
                performance_counts["selective_dirty_sync_call_count"] += 1
            if not context.dirty_page_count and not context.observed_write_count:
                performance_counts["dirty_sync_no_work_count"] += 1
                return
            if selective and not required_pages and not context.observed_write_count:
                performance_counts["selective_dirty_sync_no_match_count"] += 1
                return
            started_ns = time.perf_counter_ns()
            drain_native_write_log()
            if selective and not required_pages:
                performance_counts["selective_dirty_sync_no_match_count"] += 1
                record_performance("dirty_page_sync", started_ns)
                return
            dirty_page_count = int(context.dirty_page_count)
            performance_counts["dirty_page_scan_count"] += dirty_page_count
            performance_counts["dirty_page_list_peak"] = max(
                performance_counts["dirty_page_list_peak"],
                dirty_page_count,
            )
            write_native_page_range = getattr(
                memory,
                "write_native_page_range",
                memory.write,
            )
            retained_page_count = 0
            selective_writeback_count = 0
            for index in range(dirty_page_count):
                page = int(dirty_page_indices[index])
                if selective and page not in required_pages:
                    dirty_page_indices[retained_page_count] = page
                    retained_page_count += 1
                    continue
                buffer = page_buffers.get(page)
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
                selective_writeback_count += 1
            context.dirty_page_count = retained_page_count
            if selective:
                performance_counts["selective_dirty_sync_retained_page_count"] += (
                    retained_page_count
                )
                performance_counts["selective_dirty_sync_page_writeback_count"] += (
                    selective_writeback_count
                )
                if selective_writeback_count == 0:
                    performance_counts["selective_dirty_sync_no_match_count"] += 1
            record_performance("dirty_page_sync", started_ns)

        def drain_native_write_log() -> None:
            count = int(context.observed_write_count)
            if count == 0:
                return
            performance_counts["native_observed_write_drain_count"] += 1
            performance_counts["native_observed_write_count"] += count
            if memory_write_batch_observer is not None:
                batch_started_ns = time.perf_counter_ns()
                packed_flags = int(
                    self._observed_write_packer(
                        packed_observed_write_records,
                        observed_write_capacity,
                        observed_write_addresses,
                        observed_write_values,
                        observed_write_sizes,
                        count,
                        int(context.observed_write_range_start),
                        int(context.observed_write_range_end),
                        int(memory_write_batch_address_base) & 0xFFFFFFFF,
                    )
                )
                if not packed_flags & 1:
                    raise NativeExecutorError(
                        "native observed-write batch escaped its configured range"
                    )
                memory_write_batch_observer(
                    packed_observed_write_records,
                    observed_write_addresses,
                    observed_write_values,
                    observed_write_sizes,
                    observed_write_eips,
                    observed_write_source_addresses,
                    observed_write_steps,
                    count,
                    int(context.observed_write_range_start),
                    bool(packed_flags & 2),
                )
                performance_counts["native_observed_write_batch_count"] += 1
                record_performance(
                    "memory_write_observer_batch",
                    batch_started_ns,
                )
            elif memory_write_observer is not None:
                for index in range(count):
                    memory_write_observer(
                        int(observed_write_eips[index]),
                        int(observed_write_addresses[index]),
                        int(observed_write_sizes[index]),
                        int(observed_write_values[index]),
                        int(observed_write_steps[index]),
                    )
            context.observed_write_count = 0

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

        def requires_memory_callback(address: int, size: int) -> bool:
            return any(
                cache_address(address + offset)
                in self._callback_byte_addresses
                for offset in range(size)
            )

        def refresh_cached_changes(address: int) -> None:
            address = cache_address(address)
            if not cacheable_address(address):
                return
            if not callable(consume_changed_page_ranges):
                page = address >> 12
                if (
                    page in page_generations
                    and memory.page_generation(address) != page_generations[page]
                ):
                    page_table[page] = None
                    page_buffers.pop(page, None)
                    page_generations.pop(page, None)
                    performance_counts["invalidated_page_count"] += 1
                else:
                    performance_counts["cached_range_refresh_no_change_count"] += 1
                return
            changed_ranges = consume_changed_page_ranges()
            if not changed_ranges:
                performance_counts["cached_range_refresh_no_change_count"] += 1
                return
            range_snapshot = getattr(memory, "native_page_range_snapshot", None)
            for page, (start, end) in changed_ranges.items():
                buffer = page_buffers.get(page)
                if buffer is None:
                    continue
                if not 0 <= start < end <= 4096:
                    raise NativeExecutorError(
                        f"invalid host changed range {start}:{end} "
                        f"for page 0x{page:05X}"
                    )
                range_address = (page << 12) + start
                if callable(range_snapshot):
                    payload = range_snapshot(range_address, end - start)
                else:
                    payload = SparseMemory.read(memory, range_address, end - start)
                ctypes.memmove(
                    int(page_table[page]) + start,
                    payload,
                    len(payload),
                )
                page_generations[page] = memory.page_generation(page << 12)
                performance_counts["cached_range_refresh_count"] += 1
                performance_counts["cached_range_refresh_byte_count"] += len(payload)

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
            memory_address = cache_address(address)
            exact_callback = requires_memory_callback(address, 4)
            if exact_callback:
                performance_counts["exact_read_u32_callback_count"] += 1
                sample_read_callback(
                    "exact_u32",
                    memory_address,
                    performance_counts["exact_read_u32_callback_count"],
                    exact=True,
                )
                sync_dirty_pages(
                    callback_dependency_pages(
                        memory_address,
                        4,
                        is_write=False,
                    )
                )
            else:
                performance_counts["page_miss_read_u32_callback_count"] += 1
                sample_read_callback(
                    "page_miss_u32",
                    memory_address,
                    performance_counts["page_miss_read_u32_callback_count"],
                    exact=False,
                )
            value = memory.read_u32(memory_address)
            if exact_callback:
                refresh_cached_changes(memory_address)
            else:
                cache_page(memory_address)
            return value

        def read8(_user, address: int) -> int:
            performance_counts["read_u8_callback_count"] += 1
            memory_address = cache_address(address)
            exact_callback = requires_memory_callback(address, 1)
            if exact_callback:
                performance_counts["exact_read_u8_callback_count"] += 1
                sample_read_callback(
                    "exact_u8",
                    memory_address,
                    performance_counts["exact_read_u8_callback_count"],
                    exact=True,
                )
                sync_dirty_pages(
                    callback_dependency_pages(
                        memory_address,
                        1,
                        is_write=False,
                    )
                )
            else:
                performance_counts["page_miss_read_u8_callback_count"] += 1
                sample_read_callback(
                    "page_miss_u8",
                    memory_address,
                    performance_counts["page_miss_read_u8_callback_count"],
                    exact=False,
                )
            value = memory.read(memory_address, 1)[0]
            if exact_callback:
                refresh_cached_changes(memory_address)
            else:
                cache_page(memory_address)
            return value

        read_u32 = _ReadU32(guard(read32, 0))
        def check_write(address: int) -> None:
            address = cache_address(address)
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
            memory_address = cache_address(address)
            check_write(memory_address)
            # Preserve ordering between cached native writes and a subsequent
            # callback/MMIO write.
            drain_native_write_log()
            if memory_write_observer is not None:
                memory_write_observer(
                    int(context.eip), memory_address, 4, value, int(context.steps)
                )
            if requires_memory_callback(address, 4):
                sync_dirty_pages(
                    callback_dependency_pages(
                        memory_address,
                        4,
                        is_write=True,
                    )
                )
                memory.write_u32(memory_address, value)
                refresh_cached_changes(memory_address)
                if yield_predicate is not None and yield_predicate():
                    context.yield_requested = True
                return
            page = memory_address >> 12
            if cacheable_address(memory_address) and (memory_address & 0xFFF) <= 0xFFC:
                cache_page(memory_address)
            if page in page_buffers and (memory_address & 0xFFF) <= 0xFFC:
                ctypes.c_uint32.from_address(
                    int(page_table[page]) + (memory_address & 0xFFF)
                ).value = value
                mark_dirty_page(page, memory_address & 0xFFF, 4)
                return
            memory.write_u32(memory_address, value)
            if yield_predicate is not None and yield_predicate():
                context.yield_requested = True

        def write8(_user, address: int, value: int) -> None:
            performance_counts["write_u8_callback_count"] += 1
            memory_address = cache_address(address)
            check_write(memory_address)
            drain_native_write_log()
            if memory_write_observer is not None:
                memory_write_observer(
                    int(context.eip), memory_address, 1, value, int(context.steps)
                )
            if requires_memory_callback(address, 1):
                sync_dirty_pages(
                    callback_dependency_pages(
                        memory_address,
                        1,
                        is_write=True,
                    )
                )
                memory.write(memory_address, bytes((value,)))
                refresh_cached_changes(memory_address)
                if yield_predicate is not None and yield_predicate():
                    context.yield_requested = True
                return
            page = memory_address >> 12
            if cacheable_address(memory_address):
                cache_page(memory_address)
            if page in page_buffers:
                ctypes.c_uint8.from_address(
                    int(page_table[page]) + (memory_address & 0xFFF)
                ).value = value
                mark_dirty_page(page, memory_address & 0xFFF, 1)
                return
            memory.write(memory_address, bytes((value,)))
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
        context.callback_address_keys = ctypes.cast(
            self._callback_address_keys,
            ctypes.POINTER(ctypes.c_uint32),
        )
        context.callback_address_mask = self._callback_address_mask
        context.cache_physical_aliases = physical_alias_cache_enabled
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
        context.observed_write_eips = (
            ctypes.cast(observed_write_eips, ctypes.POINTER(ctypes.c_uint32))
            if observed_write_capacity
            else None
        )
        context.observed_write_source_addresses = (
            ctypes.cast(
                observed_write_source_addresses,
                ctypes.POINTER(ctypes.c_uint32),
            )
            if observed_write_capacity
            else None
        )
        context.observed_write_addresses = (
            ctypes.cast(observed_write_addresses, ctypes.POINTER(ctypes.c_uint32))
            if observed_write_capacity
            else None
        )
        context.observed_write_values = (
            ctypes.cast(observed_write_values, ctypes.POINTER(ctypes.c_uint32))
            if observed_write_capacity
            else None
        )
        context.observed_write_steps = (
            ctypes.cast(observed_write_steps, ctypes.POINTER(ctypes.c_uint64))
            if observed_write_capacity
            else None
        )
        context.observed_write_sizes = (
            ctypes.cast(observed_write_sizes, ctypes.POINTER(ctypes.c_uint8))
            if observed_write_capacity
            else None
        )
        context.observed_write_count = 0
        context.observed_write_capacity = observed_write_capacity
        refresh_observed_write_range()
        if cache_context_reused:
            invalidate_changed_pages()
        elif callable(consume_changed_pages):
            # This executor has no cached pages for the current memory object,
            # so pre-run writes cannot make an entry stale. Establish the
            # worklist boundary before the first page fill.
            consume_changed_pages()
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
            dispatch_hot_targets = sorted(
                (
                    {
                        "target": self._dispatch_addresses_by_slot[slot],
                        "target_hex": (
                            f"0x{self._dispatch_addresses_by_slot[slot]:08X}"
                        ),
                        "module_calls": int(
                            self._dispatch_target_call_counts[slot]
                        ),
                        "guest_steps": int(
                            self._dispatch_target_step_counts[slot]
                        ),
                        "average_guest_steps_per_call": round(
                            int(self._dispatch_target_step_counts[slot])
                            / max(
                                1,
                                int(self._dispatch_target_call_counts[slot]),
                            ),
                            3,
                        ),
                    }
                    for slot in (
                        int(self._dispatch_touched_slots[index])
                        for index in range(
                            int(self._dispatch_touched_count.value)
                        )
                    )
                ),
                key=lambda item: (
                    -item["guest_steps"],
                    -item["module_calls"],
                    item["target"],
                ),
            )
            performance_counts["native_dispatch_hot_target_count"] = len(
                dispatch_hot_targets
            )
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
                    "read_callback_sampling": {
                        "interval": NATIVE_READ_CALLBACK_SAMPLE_INTERVAL,
                        "sample_count": sum(read_callback_samples.values()),
                        "high_address_sample_count": sum(
                            count
                            for (_kind, address), count in (
                                read_callback_samples.items()
                            )
                            if address >= 0x80000000
                        ),
                        "hot_addresses": [
                            {
                                "kind": kind,
                                "address": address,
                                "address_hex": f"0x{address:08X}",
                                "sample_count": count,
                            }
                            for (kind, address), count in sorted(
                                read_callback_samples.items(),
                                key=lambda item: (
                                    -item[1],
                                    item[0][0],
                                    item[0][1],
                                ),
                            )[:NATIVE_READ_CALLBACK_HOT_ADDRESS_LIMIT]
                        ],
                    },
                    "native_dispatch_hot_targets": dispatch_hot_targets[:32],
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
            for index in range(int(self._dispatch_touched_count.value)):
                slot = int(self._dispatch_touched_slots[index])
                self._dispatch_target_call_counts[slot] = 0
                self._dispatch_target_step_counts[slot] = 0
            self._dispatch_touched_count.value = 0
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
                    self._dispatch_target_call_counts,
                    self._dispatch_target_step_counts,
                    self._dispatch_touched_slots,
                    ctypes.byref(self._dispatch_touched_count),
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
            if (
                call_handler_yield_predicate is not None
                and call_handler_yield_predicate(target)
            ):
                performance_counts["call_handler_yield_count"] += 1
                return finish("call_handler_yield", int(context.eip), dispatch_eip)


def _context_from_state(state: CpuState) -> _Context:
    context = _Context()
    _update_context(context, state)
    return context


def _update_context(context: _Context, state: CpuState) -> None:
    for name in ("eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi"):
        setattr(context, name, state.get_register(name))
    context.fs_base = state.fs_base
    context.cs_selector = state.cs_selector
    context.gdtr_base = state.gdtr_base
    context.gdtr_limit = state.gdtr_limit
    context.timestamp_counter = state.timestamp_counter
    context.mxcsr = state.mxcsr
    context.fpu_control_word = state.fpu_control_word
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
    state.cs_selector = context.cs_selector & 0xFFFF
    state.gdtr_base = context.gdtr_base
    state.gdtr_limit = context.gdtr_limit & 0xFFFF
    state.timestamp_counter = context.timestamp_counter
    state.mxcsr = context.mxcsr
    state.fpu_control_word = context.fpu_control_word & 0xFFFF
    state.fpu_status_word = context.fpu_status_word
    state.fpu_stack = [float(context.fpu_stack[index]) for index in range(context.fpu_depth)]
    for index in range(8):
        state.set_xmm_register(f"xmm{index}", context.xmm[index].lane)
        state.set_mmx_register(f"mm{index}", context.mmx[index])
    state.flags = CpuFlags(
        **{
            name: bool(getattr(context.flags, name))
            for name in (
                "cf",
                "pf",
                "af",
                "zf",
                "sf",
                "of",
                "df",
                "interrupt_enabled",
            )
        }
    )
    state.eip = context.eip
