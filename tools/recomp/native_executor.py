"""Cached native execution for resumable lifted x86 functions."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from collections import Counter, deque
from pathlib import Path
from typing import Any, Callable, Iterable

from tools.recomp.x86_lifter import (
    CpuFlags,
    CpuState,
    ExecutionTrace,
    LiftedFunction,
    NativeFastPath,
    SparseMemory,
    emit_cpp,
)

NATIVE_MEMORY_CALLBACK_SAMPLE_INTERVAL = 1024
NATIVE_MEMORY_CALLBACK_HOT_ADDRESS_LIMIT = 32
NATIVE_MEMORY_CALLBACK_SAMPLE_KEY_LIMIT = 256
NATIVE_DISPATCH_EDGE_CAPACITY_LIMIT = 1 << 16
NATIVE_DISPATCH_EDGE_REPORT_LIMIT = 1024
NATIVE_EMITTER_SOURCE_FILE = Path(emit_cpp.__code__.co_filename).resolve()


def _is_native_dll_artifact(path: Path) -> bool:
    try:
        if path.stat().st_size < 256:
            return False
        with path.open("rb") as artifact:
            header = artifact.read(64)
            if len(header) != 64 or header[:2] != b"MZ":
                return False
            pe_offset = int.from_bytes(header[60:64], "little")
            if pe_offset < 64:
                return False
            artifact.seek(pe_offset)
            return artifact.read(4) == b"PE\0\0"
    except OSError:
        return False


class NativeExecutorError(RuntimeError):
    pass


class NativeModuleManifest:
    """SQLite manifest mapping native configuration partitions to DLLs."""

    VERSION = 1
    DEFAULT_MAX_ARTIFACTS = 2048
    DEFAULT_MAX_ARTIFACT_BYTES = 4 * 1024 * 1024 * 1024
    PRUNE_INTERVAL_NS = 6 * 60 * 60 * 1_000_000_000
    ORPHAN_GRACE_NS = 60 * 60 * 1_000_000_000

    def __init__(
        self,
        build_dir: Path,
        *,
        max_artifacts: int = DEFAULT_MAX_ARTIFACTS,
        max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
    ) -> None:
        self.build_dir = build_dir.resolve()
        self.path = self.build_dir / "native-module-manifest.sqlite3"
        self.max_artifacts = max(1, int(max_artifacts))
        self.max_artifact_bytes = max(1, int(max_artifact_bytes))
        self.hits = 0
        self.misses = 0
        self.stores = 0
        self.pruned_artifacts = 0
        self.pruned_bytes = 0
        self.pruned_orphan_files = 0
        self.pruned_orphan_bytes = 0
        self.build_dir.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.path), timeout=30.0)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS cache_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS native_modules (
                configuration_key TEXT NOT NULL,
                partition_start INTEGER NOT NULL,
                partition_end INTEGER NOT NULL,
                content_digest TEXT NOT NULL,
                artifact_name TEXT NOT NULL,
                source_name TEXT NOT NULL,
                artifact_bytes INTEGER NOT NULL,
                source_bytes INTEGER NOT NULL,
                last_used_ns INTEGER NOT NULL,
                access_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (
                    configuration_key,
                    partition_start,
                    partition_end
                )
            );
            CREATE INDEX IF NOT EXISTS native_modules_last_used
                ON native_modules(last_used_ns);
            """
        )
        self._connection.execute(
            "INSERT OR REPLACE INTO cache_metadata(key, value) VALUES('version', ?)",
            (str(self.VERSION),),
        )
        self._connection.commit()

    def _owned_path(self, name: str) -> Path | None:
        candidate = (self.build_dir / name).resolve()
        if candidate.parent != self.build_dir:
            return None
        return candidate

    def resolve(
        self,
        *,
        configuration_key: str,
        partition_start: int,
        partition_end: int,
        content_digest: str,
    ) -> tuple[Path, Path] | None:
        row = self._connection.execute(
            """
            SELECT content_digest, artifact_name, source_name
            FROM native_modules
            WHERE configuration_key=?
                AND partition_start=? AND partition_end=?
            """,
            (configuration_key, partition_start, partition_end),
        ).fetchone()
        if row is None or row[0] != content_digest:
            self.misses += 1
            return None
        artifact_path = self._owned_path(row[1])
        source_path = self._owned_path(row[2])
        if (
            artifact_path is None
            or source_path is None
            or not _is_native_dll_artifact(artifact_path)
        ):
            self._connection.execute(
                """
                DELETE FROM native_modules
                WHERE configuration_key=?
                    AND partition_start=? AND partition_end=?
                """,
                (configuration_key, partition_start, partition_end),
            )
            self._connection.commit()
            if source_path is not None:
                try:
                    source_path.unlink(missing_ok=True)
                except OSError:
                    pass
            self.misses += 1
            return None
        self._connection.execute(
            """
            UPDATE native_modules
            SET last_used_ns=?, access_count=access_count+1
            WHERE configuration_key=?
                AND partition_start=? AND partition_end=?
            """,
            (
                time.time_ns(),
                configuration_key,
                partition_start,
                partition_end,
            ),
        )
        self.hits += 1
        return source_path, artifact_path

    def record(
        self,
        *,
        configuration_key: str,
        partition_start: int,
        partition_end: int,
        content_digest: str,
        source_path: Path,
        artifact_path: Path,
    ) -> None:
        artifact_bytes = artifact_path.stat().st_size
        source_bytes = source_path.stat().st_size if source_path.is_file() else 0
        self._connection.execute(
            """
            INSERT OR REPLACE INTO native_modules(
                configuration_key, partition_start, partition_end,
                content_digest, artifact_name, source_name,
                artifact_bytes, source_bytes, last_used_ns, access_count
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                configuration_key,
                partition_start,
                partition_end,
                content_digest,
                artifact_path.name,
                source_path.name,
                artifact_bytes,
                source_bytes,
                time.time_ns(),
            ),
        )
        self._connection.commit()
        self.stores += 1

    def equivalent_source_candidates(
        self,
        *,
        partition_start: int,
        partition_end: int,
        content_digest: str,
    ) -> list[tuple[Path, Path]]:
        """Return valid same-IR artifacts for exact emitted-source comparison."""

        candidates: list[tuple[Path, Path]] = []
        rows = self._connection.execute(
            """
            SELECT source_name, artifact_name
            FROM native_modules
            WHERE partition_start=? AND partition_end=? AND content_digest=?
            ORDER BY last_used_ns DESC
            """,
            (partition_start, partition_end, content_digest),
        ).fetchall()
        for source_name, artifact_name in rows:
            source_path = self._owned_path(source_name)
            artifact_path = self._owned_path(artifact_name)
            if (
                source_path is None
                or artifact_path is None
                or not source_path.is_file()
                or not _is_native_dll_artifact(artifact_path)
            ):
                continue
            candidates.append((source_path, artifact_path))
        return candidates

    def prune(self, *, protected_artifacts: Iterable[Path] = ()) -> None:
        protected = {path.resolve() for path in protected_artifacts}
        rows = self._connection.execute(
            """
            SELECT configuration_key, partition_start, partition_end,
                   artifact_name, source_name, artifact_bytes, source_bytes
            FROM native_modules ORDER BY last_used_ns ASC
            """
        ).fetchall()
        valid_rows = []
        total_bytes = 0
        for row in rows:
            artifact_path = self._owned_path(row[3])
            if artifact_path is None or not _is_native_dll_artifact(artifact_path):
                self._connection.execute(
                    """
                    DELETE FROM native_modules
                    WHERE configuration_key=?
                        AND partition_start=? AND partition_end=?
                    """,
                    row[:3],
                )
                source_path = self._owned_path(row[4])
                if source_path is not None:
                    try:
                        source_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                continue
            valid_rows.append(row)
            total_bytes += int(row[5]) + int(row[6])
        excess_count = max(0, len(valid_rows) - self.max_artifacts)
        excess_bytes = max(0, total_bytes - self.max_artifact_bytes)
        removed_count = 0
        removed_bytes = 0
        for row in valid_rows:
            if removed_count >= excess_count and removed_bytes >= excess_bytes:
                break
            artifact_path = self._owned_path(row[3])
            if artifact_path is None or artifact_path in protected:
                continue
            row_bytes = int(row[5]) + int(row[6])
            deleted = True
            for name in (row[3], row[4]):
                path = self._owned_path(name)
                if path is None:
                    continue
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    deleted = False
            if not deleted:
                continue
            self._connection.execute(
                """
                DELETE FROM native_modules
                WHERE configuration_key=?
                    AND partition_start=? AND partition_end=?
                """,
                row[:3],
            )
            removed_count += 1
            removed_bytes += row_bytes
        referenced_names = {
            name
            for artifact_name, source_name in self._connection.execute(
                "SELECT artifact_name, source_name FROM native_modules"
            ).fetchall()
            for name in (artifact_name, source_name)
        }
        current_time_ns = time.time_ns()
        orphan_count = 0
        orphan_bytes = 0
        for path in self.build_dir.glob("native-loop-*"):
            if (
                not path.is_file()
                or path.name in referenced_names
                or path.resolve() in protected
                or path.suffix.casefold() not in {".cpp", ".dll", ".lib", ".exp"}
            ):
                continue
            try:
                stat = path.stat()
                if current_time_ns - stat.st_mtime_ns < self.ORPHAN_GRACE_NS:
                    continue
                path.unlink()
            except OSError:
                continue
            orphan_count += 1
            orphan_bytes += stat.st_size
        self._connection.execute(
            "INSERT OR REPLACE INTO cache_metadata(key, value) "
            "VALUES('last_prune_ns', ?)",
            (str(current_time_ns),),
        )
        self._connection.commit()
        self.pruned_artifacts += removed_count
        self.pruned_bytes += removed_bytes
        self.pruned_orphan_files += orphan_count
        self.pruned_orphan_bytes += orphan_bytes

    def prune_if_needed(self, *, protected_artifacts: Iterable[Path] = ()) -> None:
        row = self._connection.execute(
            "SELECT value FROM cache_metadata WHERE key='last_prune_ns'"
        ).fetchone()
        last_prune_ns = int(row[0]) if row is not None else 0
        artifact_count, artifact_bytes = self._connection.execute(
            """
            SELECT COUNT(*),
                   COALESCE(SUM(artifact_bytes + source_bytes), 0)
            FROM native_modules
            """
        ).fetchone()
        if (
            time.time_ns() - last_prune_ns >= self.PRUNE_INTERVAL_NS
            or int(artifact_count) > self.max_artifacts
            or int(artifact_bytes) > self.max_artifact_bytes
        ):
            self.prune(protected_artifacts=protected_artifacts)

    def summary(self) -> dict[str, Any]:
        artifact_count, artifact_bytes = self._connection.execute(
            """
            SELECT COUNT(*),
                   COALESCE(SUM(artifact_bytes + source_bytes), 0)
            FROM native_modules
            """
        ).fetchone()
        return {
            "backend": "sqlite-partition-manifest",
            "path": str(self.path),
            "version": self.VERSION,
            "artifact_count": int(artifact_count),
            "artifact_bytes": int(artifact_bytes),
            "hits": self.hits,
            "misses": self.misses,
            "stores": self.stores,
            "pruned_artifacts": self.pruned_artifacts,
            "pruned_bytes": self.pruned_bytes,
            "pruned_orphan_files": self.pruned_orphan_files,
            "pruned_orphan_bytes": self.pruned_orphan_bytes,
        }

    def close(self) -> None:
        connection = getattr(self, "_connection", None)
        if connection is None:
            return
        connection.close()
        self._connection = None

    def __del__(self) -> None:
        connection = getattr(self, "_connection", None)
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
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


def _partition_instructions_for_call_fusion(
    instructions: list[Any],
    *,
    base_maximum_count: int,
    maximum_count: int,
    isolated_addresses: Iterable[int] = (),
) -> list[list[Any]]:
    """Fuse stable chunks, except owners of latency-sensitive native entries."""
    if base_maximum_count <= 0 or maximum_count < base_maximum_count:
        raise ValueError("native call-fusion partition limits are invalid")
    base_modules = _partition_instructions_by_address(
        instructions,
        maximum_count=base_maximum_count,
    )
    if len(base_modules) < 2:
        return base_modules

    owner_by_address = {
        instruction.address: module_index
        for module_index, module in enumerate(base_modules)
        for instruction in module
    }
    isolated_owners = {
        owner_by_address[address]
        for address in isolated_addresses
        if address in owner_by_address
    }
    edge_weights: Counter[tuple[int, int]] = Counter()
    for instruction in instructions:
        if instruction.mnemonic not in {"call", "jmp"}:
            continue
        target = getattr(instruction, "target", None)
        source_owner = owner_by_address.get(instruction.address)
        target_owner = owner_by_address.get(target)
        if (
            source_owner is None
            or target_owner is None
            or source_owner == target_owner
        ):
            continue
        edge_weights[tuple(sorted((source_owner, target_owner)))] += (
            4 if instruction.mnemonic == "call" else 1
        )

    parents = list(range(len(base_modules)))
    member_counts = [len(module) for module in base_modules]

    def find(module_index: int) -> int:
        while parents[module_index] != module_index:
            parents[module_index] = parents[parents[module_index]]
            module_index = parents[module_index]
        return module_index

    for (left, right), _weight in sorted(
        edge_weights.items(),
        key=lambda item: (-item[1], item[0]),
    ):
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            continue
        if left_root in isolated_owners or right_root in isolated_owners:
            continue
        if member_counts[left_root] + member_counts[right_root] > maximum_count:
            continue
        # Retain the lowest address chunk as the stable partition anchor.
        if base_modules[left_root][0].address > base_modules[right_root][0].address:
            left_root, right_root = right_root, left_root
        parents[right_root] = left_root
        member_counts[left_root] += member_counts[right_root]

    fused: dict[int, list[Any]] = {}
    for module_index, module in enumerate(base_modules):
        fused.setdefault(find(module_index), []).extend(module)
    return [
        sorted(module, key=lambda instruction: instruction.address)
        for module in sorted(
            fused.values(),
            key=lambda items: items[0].address,
        )
    ]


_NATIVE_MODULE_EXIT_REASON_NAMES = (
    "fallthrough_or_unknown",
    "branch",
    "call",
    "return",
    "callback",
    "yield",
    "step_budget",
    "fault",
    "software_interrupt",
)
_NATIVE_MODULE_EXIT_REASON_COUNT = len(_NATIVE_MODULE_EXIT_REASON_NAMES)


_NATIVE_DISPATCH_SOURCE = r"""
#include <algorithm>
#include <chrono>
#include <climits>
#include <condition_variable>
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <deque>
#include <filesystem>
#include <mutex>
#include <new>
#include <thread>
#include <vector>
#define NOMINMAX
#include <windows.h>

using B2RNativeEntry = uint32_t (__cdecl *)(void*);
using B2RHostCall = uint32_t (__cdecl *)(void*, uint32_t, void*);
using B2RDispatchClock = std::chrono::steady_clock;
static constexpr uint32_t B2R_MODULE_EXIT_REASON_COUNT = 9u;

struct B2RFlags {
    bool cf;
    bool pf;
    bool af;
    bool zf;
    bool sf;
    bool of;
    bool df;
    bool interrupt_enabled;
};

struct B2RXmm {
    float lane[4];
};

// This is the stable generated-module ABI.  The normal-play dispatcher keeps
// worker contexts in native storage and switches them without reconstructing
// CpuState objects in Python.
struct B2RContext {
    uint32_t eax;
    uint32_t ecx;
    uint32_t edx;
    uint32_t ebx;
    uint32_t esp;
    uint32_t ebp;
    uint32_t esi;
    uint32_t edi;
    uint32_t fs_base;
    uint32_t cs_selector;
    uint32_t gdtr_base;
    uint32_t gdtr_limit;
    uint64_t timestamp_counter;
    uint32_t mxcsr;
    uint32_t fpu_control_word;
    uint32_t fpu_status_word;
    float fpu_stack[8];
    uint32_t fpu_depth;
    B2RXmm xmm[8];
    uint64_t mmx[8];
    B2RFlags flags;
    uint32_t eip;
    uint32_t fault_code;
    uint32_t fault_eip;
    uint32_t module_exit_reason;
    uint32_t callback_bypass_target;
    uint64_t steps;
    uint64_t step_budget;
    bool yield_requested;
    bool direct_observed_write_transport;
    void* user;
    uint32_t (*read_u32)(void*, uint32_t);
    void (*write_u32)(void*, uint32_t, uint32_t);
    uint8_t (*read_u8)(void*, uint32_t);
    void (*write_u8)(void*, uint32_t, uint8_t);
    void* native_memory_user;
    bool (*native_read_u32)(void*, uint32_t, uint32_t*);
    bool (*native_write_u32)(void*, uint32_t, uint32_t);
    bool (*native_read_u8)(void*, uint32_t, uint8_t*);
    bool (*native_write_u8)(void*, uint32_t, uint8_t);
    void (*call)(void*, uint32_t, B2RContext*);
    void (*observe)(void*, B2RContext*);
    uint8_t** read_pages;
    uint8_t* callback_pages;
    uint32_t* callback_address_keys;
    uint32_t callback_address_mask;
    uint8_t* write_callback_pages;
    uint32_t* write_callback_address_keys;
    uint32_t write_callback_address_mask;
    uint8_t* zero_read_callback_pages;
    uint32_t* zero_read_callback_address_keys;
    uint32_t zero_read_callback_address_mask;
    bool cache_physical_aliases;
    uint8_t* dirty_pages;
    uint32_t* dirty_page_generations;
    uint32_t* dirty_page_indices;
    uint16_t* dirty_page_min_offsets;
    uint16_t* dirty_page_max_offsets;
    uint32_t dirty_page_count;
    uint32_t dirty_page_capacity;
    uint32_t observed_write_range_start;
    uint32_t observed_write_range_end;
    uint32_t* observed_write_eips;
    uint32_t* observed_write_source_addresses;
    uint32_t* observed_write_addresses;
    uint64_t* observed_write_values;
    uint64_t* observed_write_steps;
    uint8_t* observed_write_sizes;
    uint32_t observed_write_count;
    uint32_t observed_write_capacity;
    uint32_t observed_write_packet_header;
    uint32_t observed_write_packet_next_address;
    uint64_t observed_write_packet_yield_count;
    uint64_t zero_read_callback_bypass_count;
    uint64_t direct_observed_write_count;
    uint64_t direct_observed_write_byte_count;
    uint8_t* direct_observed_payload;
    uint32_t direct_observed_payload_size;
    uint32_t direct_observed_payload_capacity;
    uint32_t* direct_observed_span_addresses;
    uint32_t* direct_observed_span_payload_offsets;
    uint32_t* direct_observed_span_payload_sizes;
    uint32_t* direct_observed_span_write_counts;
    uint8_t* direct_observed_span_flags;
    uint32_t direct_observed_span_count;
    uint32_t direct_observed_span_capacity;
    bool direct_observed_span_sealed;
    uint32_t* native_fast_path_address_keys;
    uint64_t* native_fast_path_call_counts;
    uint32_t native_fast_path_address_mask;
};

extern "C" __declspec(dllexport) uint32_t b2r_native_context_size() {
    return static_cast<uint32_t>(sizeof(B2RContext));
}

static uint64_t b2r_dispatch_now_ns() {
    return static_cast<uint64_t>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(
            B2RDispatchClock::now().time_since_epoch()
        ).count()
    );
}

static void b2r_record_dispatch_edge(
    uint32_t entry_target,
    uint32_t exit_target,
    uint32_t reason,
    uint64_t* edge_keys,
    uint8_t* edge_reasons,
    uint64_t* edge_counts,
    uint32_t edge_mask,
    uint32_t* touched_edge_slots,
    uint32_t* touched_edge_count,
    uint64_t* edge_overflow_count
) {
    const uint64_t key =
        (static_cast<uint64_t>(entry_target) << 32u) | exit_target;
    uint64_t hash = key ^
        (static_cast<uint64_t>(reason) * 0x9E3779B97F4A7C15ull);
    hash ^= hash >> 30u;
    hash *= 0xBF58476D1CE4E5B9ull;
    hash ^= hash >> 27u;
    hash *= 0x94D049BB133111EBull;
    hash ^= hash >> 31u;
    uint32_t slot = static_cast<uint32_t>(hash) & edge_mask;
    for (uint32_t probe = 0u; probe <= edge_mask; ++probe) {
        if (edge_counts[slot] == 0u) {
            edge_keys[slot] = key;
            edge_reasons[slot] = static_cast<uint8_t>(reason);
            edge_counts[slot] = 1u;
            touched_edge_slots[(*touched_edge_count)++] = slot;
            return;
        }
        if (edge_keys[slot] == key && edge_reasons[slot] == reason) {
            ++edge_counts[slot];
            return;
        }
        slot = (slot + 1u) & edge_mask;
    }
    ++*edge_overflow_count;
}

struct B2RNativeHostServiceEntry {
    uint32_t target;
    uint32_t kind;
    uint32_t stack_cleanup_bytes;
    uint32_t value;
};

constexpr uint32_t B2R_WORKER_LIFECYCLE_CAPACITY = 64u;
constexpr uint32_t B2R_WORKER_READY = 1u;
constexpr uint32_t B2R_WORKER_RUNNING = 2u;
constexpr uint32_t B2R_WORKER_WAITING = 3u;
constexpr uint32_t B2R_WORKER_COMPLETED = 4u;
constexpr uint32_t B2R_WORKER_BLOCKED = 5u;
constexpr uint32_t B2R_WORKER_FAILED = 6u;
constexpr uint32_t B2R_WORKER_SUSPENDED = 7u;
constexpr uint32_t B2R_D3D_VBLANK_DATA_ADDRESS = 0x21b77000u;
constexpr uint32_t B2R_D3D_VBLANK_STACK_TOP = 0x71fff000u;
constexpr uint32_t B2R_D3D_VBLANK_RETURN_SENTINEL = 0xb2d3d000u;

struct B2RNativeWorkerLifecycleEntry {
    uint32_t handle;
    uint32_t start_address;
    uint32_t start_context1;
    uint32_t start_context2;
    uint32_t status;
    uint32_t wait_handle;
    uint32_t generation;
    int32_t priority;
    int32_t base_priority;
    uint32_t disable_boost;
};

struct B2RNativeWorkerLifecycleState {
    B2RNativeWorkerLifecycleEntry entries[B2R_WORKER_LIFECYCLE_CAPACITY];
    uint32_t entry_count;
    uint32_t selection_cursor;
    uint64_t transition_count;
    uint64_t created_count;
    uint64_t resumed_count;
    uint64_t suspended_count;
    uint64_t completed_count;
    uint64_t overflow_count;
};

static B2RNativeWorkerLifecycleEntry* b2r_find_worker(
    B2RNativeWorkerLifecycleState* state,
    uint32_t handle
) {
    if (state == nullptr || handle == 0u) { return nullptr; }
    for (uint32_t index = 0u; index < state->entry_count; ++index) {
        if (state->entries[index].handle == handle) {
            return &state->entries[index];
        }
    }
    return nullptr;
}

static bool b2r_set_worker_status(
    B2RNativeWorkerLifecycleState* state,
    B2RNativeWorkerLifecycleEntry* worker,
    uint32_t status
) {
    if (state == nullptr || worker == nullptr || worker->status == status) {
        return worker != nullptr;
    }
    worker->status = status;
    if (++worker->generation == 0u) { worker->generation = 1u; }
    ++state->transition_count;
    if (status == B2R_WORKER_READY) { ++state->resumed_count; }
    if (status == B2R_WORKER_SUSPENDED) { ++state->suspended_count; }
    if (status == B2R_WORKER_COMPLETED) { ++state->completed_count; }
    if (status != B2R_WORKER_WAITING) { worker->wait_handle = 0u; }
    return true;
}

static B2RNativeWorkerLifecycleEntry* b2r_upsert_worker(
    B2RNativeWorkerLifecycleState* state,
    uint32_t handle,
    uint32_t start_address,
    uint32_t start_context1,
    uint32_t start_context2,
    uint32_t status
) {
    B2RNativeWorkerLifecycleEntry* worker = b2r_find_worker(state, handle);
    if (worker == nullptr) {
        if (state == nullptr ||
            state->entry_count >= B2R_WORKER_LIFECYCLE_CAPACITY) {
            if (state != nullptr) { ++state->overflow_count; }
            return nullptr;
        }
        worker = &state->entries[state->entry_count++];
        worker->handle = handle;
        worker->generation = 1u;
        worker->status = status;
        worker->wait_handle = 0u;
        worker->priority = 8;
        worker->base_priority = 8;
        worker->disable_boost = 0u;
        ++state->created_count;
        ++state->transition_count;
        if (status == B2R_WORKER_SUSPENDED) { ++state->suspended_count; }
    } else {
        b2r_set_worker_status(state, worker, status);
    }
    worker->start_address = start_address;
    worker->start_context1 = start_context1;
    worker->start_context2 = start_context2;
    return worker;
}

struct B2RNativeControllerState {
    uint16_t buttons;
    uint8_t left_trigger;
    uint8_t right_trigger;
    int16_t thumb_lx;
    int16_t thumb_ly;
    int16_t thumb_rx;
    int16_t thumb_ry;
    uint8_t connected;
    uint8_t reserved[3];
    uint32_t generation;
};

constexpr uint32_t B2R_NATIVE_SEMAPHORE_CAPACITY = 64u;
constexpr uint32_t B2R_NATIVE_ALLOCATION_CAPACITY = 1024u;
constexpr uint32_t B2R_NATIVE_FILE_CAPACITY = 128u;
constexpr uint32_t B2R_NATIVE_TITLE_ASSET_STREAM_CAPACITY = 128u;
constexpr uint32_t B2R_NATIVE_SERVICE_TRACE_CAPACITY = 4096u;
constexpr uint32_t B2R_NATIVE_TRAFFIC_MESH_SAMPLE_CAPACITY = 1024u;

struct B2RNativeSemaphoreEntry {
    uint32_t handle;
    uint32_t count;
    uint32_t limit;
};

struct B2RNativeAllocationEntry {
    uint32_t address;
    uint32_t size;
    uint32_t kind;
    uint32_t active;
};

struct B2RNativeFileEntry {
    uint32_t handle;
    uint32_t flags;
    uint64_t size;
    uint64_t position;
    uint64_t host_file;
    uint32_t active;
    uint32_t reserved;
    uint32_t directory_index;
    uint32_t directory_initialized;
    char guest_path[256];
    char host_path[512];
    char directory_pattern[256];
};

struct B2RNativeTitleAssetStreamEntry {
    uint32_t object;
    uint32_t payload;
    uint32_t payload_size;
    uint32_t flags;
    uint32_t image_base;
};

struct B2RNativeTrafficMeshSample {
    uint32_t caller;
    uint32_t owner;
    uint32_t owner_mode;
    uint32_t mesh_entry;
    uint32_t index_data;
    uint32_t index_count;
};

struct B2RNativeWorkerExecutionSlot {
    uint32_t handle;
    uint32_t return_sentinel;
    uint64_t context;
    uint64_t run_count;
    uint64_t step_count;
    uint32_t last_target;
    uint32_t last_reason;
};

struct B2RNativeHostServiceState {
    B2RNativeHostServiceEntry* entries;
    uint32_t entry_count;
    uint32_t eax_offset;
    uint32_t ecx_offset;
    uint32_t esp_offset;
    uint32_t eip_offset;
    uint32_t timestamp_counter_offset;
    uint32_t flags_offset;
    uint64_t performance_counter;
    uint64_t system_time_filetime;
    B2RNativeControllerState controllers[4];
    uint32_t controller_packets[4];
    uint32_t controller_last_generations[4];
    uint64_t service_call_counts[16];
    uint64_t native_call_count;
    uint64_t native_observer_count;
    uint64_t native_frontend_record_table_repair_count;
    uint32_t native_frontend_record_table_last_address;
    uint32_t native_frontend_record_table_last_count;
    uint32_t observed_button_mask;
    uint32_t a_pressed_poll_count;
    uint32_t successful_get_state_count;
    uint32_t current_irql;
    B2RNativeSemaphoreEntry semaphores[B2R_NATIVE_SEMAPHORE_CAPACITY];
    uint32_t semaphore_count;
    uint32_t semaphore_overflow_count;
    B2RNativeAllocationEntry allocations[B2R_NATIVE_ALLOCATION_CAPACITY];
    uint32_t allocation_count;
    uint32_t allocation_overflow_count;
    uint32_t next_pool_address;
    uint32_t next_contiguous_address;
    uint64_t native_allocated_page_count;
    uint64_t native_page_allocation_failure_count;
    uint32_t title_heap_next_address;
    uint64_t title_heap_allocation_count;
    uint64_t title_heap_free_count;
    uint64_t title_heap_bytes_allocated;
    uint64_t native_memory_write_count;
    uint64_t native_gpu_command_kick_count;
    uint64_t native_gpu_completion_count;
    uint64_t native_gpu_get_pointer_sync_count;
    uint64_t native_gpu_observed_range_sync_count;
    int64_t native_d3d_vblank_next_deadline_qpc;
    uint64_t native_d3d_vblank_tick_count;
    uint64_t native_d3d_vblank_callback_schedule_count;
    uint64_t native_d3d_vblank_callback_run_count;
    uint64_t native_d3d_vblank_callback_step_count;
    uint64_t native_d3d_vblank_callback_completion_count;
    uint64_t native_d3d_vblank_callback_failure_count;
    uint64_t native_d3d_vblank_callback_context;
    uint32_t native_d3d_vblank_sequence;
    uint32_t native_d3d_vblank_callback_address;
    bool native_d3d_vblank_callback_pending;
    uint64_t native_memory_read_count;
    uint64_t native_gpu_master_interrupt_count;
    uint32_t native_gpu_master_interrupt_pending;
    uint64_t native_page_miss_count;
    char extracted_root[1024];
    char save_data_root[1024];
    char dashboard_data_root[1024];
    char cache_data_root[1024];
    uint32_t title_id;
    char last_file_guest_path[512];
    char last_file_host_path[1536];
    uint32_t title_asset_stream_count;
    uint32_t title_asset_payload_next;
    uint64_t title_asset_open_count;
    uint64_t title_asset_open_failure_count;
    uint64_t title_asset_payload_bytes;
    B2RNativeTitleAssetStreamEntry title_asset_streams[
        B2R_NATIVE_TITLE_ASSET_STREAM_CAPACITY];
    uint64_t title_track_pss_candidate_count;
    uint64_t title_track_pss_publication_count;
    uint64_t title_track_pss_validation_failure_count;
    uint64_t title_track_pss_published_bytes;
    uint64_t title_track_pss_descriptor_entry_count;
    uint64_t title_track_pss_scene_record_entry_count;
    uint64_t title_traffic_tra_candidate_count;
    uint32_t av_saved_data_address;
    uint32_t av_display_mode_set_count;
    B2RNativeFileEntry files[B2R_NATIVE_FILE_CAPACITY];
    uint32_t file_count;
    uint32_t file_overflow_count;
    uint32_t next_object_handle;
    uint32_t service_trace_count;
    uint32_t service_trace_overflow_count;
    uint32_t service_trace_targets[B2R_NATIVE_SERVICE_TRACE_CAPACITY];
    uint32_t service_trace_kinds[B2R_NATIVE_SERVICE_TRACE_CAPACITY];
    uint32_t service_trace_values[B2R_NATIVE_SERVICE_TRACE_CAPACITY];
    uint32_t service_trace_results[B2R_NATIVE_SERVICE_TRACE_CAPACITY];
    uint32_t service_trace_return_addresses[B2R_NATIVE_SERVICE_TRACE_CAPACITY];
    uint32_t last_service_target;
    uint32_t last_service_kind;
    uint32_t last_service_value;
    uint32_t last_service_result;
    uint32_t last_service_return_address;
    uint32_t last_service_argument0;
    uint32_t last_service_worker_handle;
    const uint32_t* callback_address_keys;
    uint32_t callback_address_mask;
    const uint32_t* write_callback_address_keys;
    uint32_t write_callback_address_mask;
    B2RNativeWorkerLifecycleState* worker_lifecycle;
    uint32_t current_worker_handle;
    uint32_t yield_requested_offset;
    B2RHostCall cold_call;
    void* cold_user;
    uint8_t** read_pages;
    void* memory_user;
    uint32_t (__cdecl *read_u32)(void*, uint32_t);
    void (__cdecl *write_u32)(void*, uint32_t, uint32_t);
    uint8_t (__cdecl *read_u8)(void*, uint32_t);
    void (__cdecl *write_u8)(void*, uint32_t, uint8_t);
    bool cache_physical_aliases;
    uint8_t* dirty_pages;
    uint32_t* dirty_page_generations;
    uint32_t* dirty_page_indices;
    uint16_t* dirty_page_min_offsets;
    uint16_t* dirty_page_max_offsets;
    uint32_t* dirty_page_count;
    uint32_t dirty_page_capacity;
    bool normal_runtime_enabled;
    uint64_t scheduler_quantum;
    uint64_t scheduler_last_main_steps;
    uint64_t scheduler_service_count;
    uint64_t scheduler_worker_run_count;
    uint64_t scheduler_worker_step_count;
    uint64_t scheduler_worker_completion_count;
    uint64_t scheduler_worker_wait_count;
    uint64_t scheduler_worker_failure_count;
    B2RNativeWorkerExecutionSlot worker_execution[
        B2R_WORKER_LIFECYCLE_CAPACITY];
    uint8_t* live_control_mapping;
    uint64_t live_control_size;
    uint8_t* live_command_mapping;
    uint64_t live_command_size;
    uint8_t* live_resource_mapping;
    uint64_t live_resource_size;
    uint64_t command_stream_generation;
    uint64_t live_command_write_cursor;
    uint64_t live_published_record_count;
    uint64_t live_published_byte_count;
    uint64_t live_published_span_count;
    uint64_t live_flip_count;
    uint64_t live_manifest_publish_count;
    uint64_t live_resource_generation;
    uint64_t live_resource_publish_count;
    uint64_t live_controller_refresh_count;
    uint64_t live_presentation_wait_count;
    uint64_t live_presentation_wait_milliseconds;
    uint64_t live_video_frame_timer_handle;
    int64_t live_next_video_frame_deadline_qpc;
    uint64_t live_video_pacing_wait_count;
    uint64_t live_video_pacing_wait_microseconds;
    uint32_t live_resource_slot;
    uint32_t live_resource_payload_size;
    uint32_t live_controller_sequence;
    char live_frontend_text[129];
    uint32_t live_frontend_text_x_bits;
    uint32_t live_frontend_text_y_bits;
    uint32_t live_frontend_text_size_bits;
    uint32_t live_frontend_text_color_argb;
    bool live_frontend_text_pending;
    uint32_t normal_runtime_failure_code;
    uint32_t normal_runtime_failure_target;
    bool normal_runtime_stop_requested;
    uint64_t presentation_event_handle;
    uint64_t publication_event_handle;
    uint64_t normal_runtime_opaque;
    char audio_library_path[1024];
    uint64_t native_audio_decoded_special_clip_count;
    uint64_t native_audio_decoded_music_track_count;
    uint64_t native_audio_decode_failure_count;
    uint64_t native_audio_submitted_buffer_count;
    uint64_t native_audio_submitted_byte_count;
    uint64_t native_audio_mixed_chunk_count;
    uint64_t native_audio_dropped_buffer_count;
    uint64_t native_audio_output_error_count;
    uint64_t native_audio_queued_bytes;
    uint64_t native_audio_buffer_create_count;
    uint64_t native_audio_buffer_data_count;
    uint64_t native_audio_buffer_format_count;
    uint64_t native_audio_buffer_volume_count;
    uint64_t native_audio_buffer_frequency_count;
    uint64_t native_audio_buffer_play_count;
    uint64_t native_audio_buffer_repeated_play_count;
    uint64_t native_audio_buffer_get_position_count;
    uint64_t native_audio_buffer_set_position_count;
    uint64_t native_audio_buffer_refresh_count;
    uint64_t native_audio_buffer_stop_count;
    uint64_t native_audio_stream_create_count;
    uint64_t native_audio_stream_process_count;
    uint64_t native_audio_stream_flush_count;
    uint64_t native_audio_stream_packet_byte_count;
    uint64_t native_audio_decoded_buffer_count;
    uint64_t native_audio_decoded_stream_packet_count;
    uint32_t native_audio_active_buffer_count;
    uint32_t native_audio_active_stream_count;
    uint32_t native_audio_buffer_play_stage;
    uint32_t native_audio_last_buffer;
    uint32_t native_audio_last_data;
    uint32_t native_audio_last_size;
    uint32_t native_audio_last_sample_rate;
    uint32_t native_audio_last_format_tag;
    uint32_t native_audio_last_channels;
    uint32_t native_audio_last_bits_per_sample;
    uint32_t native_audio_last_block_align;
    uint32_t native_audio_last_samples_per_block;
    int32_t native_audio_last_volume;
    uint32_t native_audio_last_frequency;
    uint32_t native_audio_largest_loop_buffer;
    uint32_t native_audio_largest_loop_data;
    uint32_t native_audio_largest_loop_play_length;
    uint32_t native_audio_largest_loop_start;
    uint32_t native_audio_largest_loop_length;
    uint32_t native_audio_largest_loop_sample_rate;
    uint32_t native_audio_largest_loop_frequency;
    uint32_t native_audio_largest_loop_format_tag;
    uint32_t native_service_bypass_target;
    bool native_audio_output_open;
    bool native_audio_looping;
    uint64_t native_traffic_world_draw_count;
    uint64_t native_traffic_world_zero_index_count;
    uint32_t native_traffic_mesh_sample_count;
    uint32_t native_traffic_mesh_sample_cursor;
    B2RNativeTrafficMeshSample native_traffic_mesh_samples[
        B2R_NATIVE_TRAFFIC_MESH_SAMPLE_CAPACITY];
};

static uint32_t b2r_align_up(uint32_t value, uint32_t alignment) {
    if (alignment == 0u) { return value; }
    return (value + alignment - 1u) & ~(alignment - 1u);
}

static B2RNativeAllocationEntry* b2r_find_native_allocation(
    B2RNativeHostServiceState* state,
    uint32_t address
) {
    if (state == nullptr) { return nullptr; }
    for (uint32_t index = 0u; index < state->allocation_count; ++index) {
        B2RNativeAllocationEntry* allocation = &state->allocations[index];
        if (allocation->active != 0u && address >= allocation->address &&
            address - allocation->address < allocation->size) {
            return allocation;
        }
    }
    return nullptr;
}

static bool b2r_native_allocate_pages(
    B2RNativeHostServiceState* state,
    uint32_t address,
    uint32_t size
) {
    if (state == nullptr || state->read_pages == nullptr || size == 0u) {
        return false;
    }
    const uint64_t end = static_cast<uint64_t>(address) + size;
    if (end > 0x100000000ull) { return false; }
    const uint32_t first_page = address >> 12u;
    const uint32_t last_page = static_cast<uint32_t>((end - 1u) >> 12u);
    for (uint32_t page = first_page; page <= last_page; ++page) {
        if (state->read_pages[page] != nullptr) { continue; }
        uint8_t* payload = new (std::nothrow) uint8_t[4096u]();
        if (payload == nullptr) {
            ++state->native_page_allocation_failure_count;
            return false;
        }
        state->read_pages[page] = payload;
        ++state->native_allocated_page_count;
    }
    return true;
}

static B2RNativeAllocationEntry* b2r_register_native_allocation(
    B2RNativeHostServiceState* state,
    uint32_t address,
    uint32_t size,
    uint32_t kind
) {
    if (state == nullptr || size == 0u ||
        state->allocation_count >= B2R_NATIVE_ALLOCATION_CAPACITY) {
        if (state != nullptr) { ++state->allocation_overflow_count; }
        return nullptr;
    }
    B2RNativeAllocationEntry* allocation =
        &state->allocations[state->allocation_count++];
    allocation->address = address;
    allocation->size = size;
    allocation->kind = kind;
    allocation->active = 1u;
    return allocation;
}

static uint32_t b2r_native_allocate_range(
    B2RNativeHostServiceState* state,
    uint32_t size,
    uint32_t alignment,
    bool contiguous
) {
    if (state == nullptr || size == 0u) { return 0u; }
    uint32_t& next = contiguous
        ? state->next_contiguous_address : state->next_pool_address;
    alignment = alignment != 0u ? alignment : 0x1000u;
    const uint32_t address = b2r_align_up(next, alignment);
    const uint64_t end = static_cast<uint64_t>(address) + size;
    if (end > 0x80000000ull ||
        !b2r_native_allocate_pages(state, address, size) ||
        b2r_register_native_allocation(
            state, address, size, contiguous ? 2u : 1u) == nullptr) {
        return 0u;
    }
    next = b2r_align_up(static_cast<uint32_t>(end), alignment);
    return address;
}

static uint32_t b2r_native_allocate_contiguous_range(
    B2RNativeHostServiceState* state,
    uint32_t size,
    uint32_t lowest_physical,
    uint32_t highest_physical,
    uint32_t boundary
) {
    if (state == nullptr || size == 0u || highest_physical < lowest_physical) {
        return 0u;
    }
    if (lowest_physical == 0u && highest_physical == 0xffffffffu) {
        return b2r_native_allocate_range(state, size, 0x1000u, true);
    }
    uint64_t physical =
        (static_cast<uint64_t>(lowest_physical) + 0xfffu) & ~0xfffull;
    for (uint32_t attempt = 0u;
         attempt <= state->allocation_count;
         ++attempt) {
        if (boundary != 0u) {
            const uint64_t boundary_end =
                ((physical / boundary) + 1u) * boundary;
            if (physical + size > boundary_end) {
                physical = (boundary_end + 0xfffu) & ~0xfffull;
            }
        }
        const uint64_t physical_end = physical + size;
        if (physical_end == 0u || physical_end - 1u > highest_physical ||
            physical_end > 0x04000000ull) {
            return 0u;
        }
        bool overlaps = false;
        uint64_t next_physical = physical;
        for (uint32_t index = 0u; index < state->allocation_count; ++index) {
            const B2RNativeAllocationEntry& allocation =
                state->allocations[index];
            if (allocation.active == 0u ||
                allocation.address < 0x80000000u ||
                allocation.address >= 0x84000000u) {
                continue;
            }
            const uint64_t allocation_physical =
                allocation.address & 0x03ffffffu;
            const uint64_t allocation_end =
                allocation_physical + allocation.size;
            if (physical < allocation_end &&
                physical_end > allocation_physical) {
                overlaps = true;
                next_physical = std::max(
                    next_physical,
                    (allocation_end + 0xfffu) & ~0xfffull);
            }
        }
        if (overlaps) {
            physical = next_physical;
            continue;
        }
        const uint32_t address =
            static_cast<uint32_t>(physical) | 0x80000000u;
        if (!b2r_native_allocate_pages(state, address, size) ||
            b2r_register_native_allocation(
                state, address, size, 2u) == nullptr) {
            return 0u;
        }
        return address;
    }
    return 0u;
}

static B2RNativeSemaphoreEntry* b2r_find_native_semaphore(
    B2RNativeHostServiceState* state,
    uint32_t handle
) {
    if (state == nullptr || handle == 0u) { return nullptr; }
    for (uint32_t index = 0u; index < state->semaphore_count; ++index) {
        if (state->semaphores[index].handle == handle) {
            return &state->semaphores[index];
        }
    }
    return nullptr;
}

static B2RNativeSemaphoreEntry* b2r_register_native_semaphore(
    B2RNativeHostServiceState* state,
    uint32_t handle,
    uint32_t count,
    uint32_t limit
) {
    if (state == nullptr || handle == 0u || limit == 0u) { return nullptr; }
    B2RNativeSemaphoreEntry* semaphore = b2r_find_native_semaphore(state, handle);
    if (semaphore == nullptr) {
        for (uint32_t index = 0u; index < state->semaphore_count; ++index) {
            if (state->semaphores[index].handle == 0u) {
                semaphore = &state->semaphores[index];
                break;
            }
        }
        if (semaphore == nullptr &&
            state->semaphore_count >= B2R_NATIVE_SEMAPHORE_CAPACITY) {
            ++state->semaphore_overflow_count;
            return nullptr;
        }
        if (semaphore == nullptr) {
            semaphore = &state->semaphores[state->semaphore_count++];
        }
        semaphore->handle = handle;
    }
    semaphore->limit = limit;
    semaphore->count = count < limit ? count : limit;
    return semaphore;
}

static uint32_t b2r_allocate_native_object_handle(
    B2RNativeHostServiceState* state
) {
    if (state == nullptr) { return 0u; }
    if (state->next_object_handle < 0x108u) {
        state->next_object_handle = 0x108u;
    }
    const uint32_t handle = state->next_object_handle;
    state->next_object_handle += 4u;
    return handle;
}

static uint32_t b2r_native_service_cache_address(
    B2RNativeHostServiceState* state,
    uint32_t address
) {
    if (state->cache_physical_aliases &&
        address >= 0xa0000000u && address <= 0xbfffffffu) {
        return address & 0x7fffffffu;
    }
    return address;
}

static bool b2r_native_service_has_exact_callback(
    B2RNativeHostServiceState* state,
    const uint32_t* keys,
    uint32_t mask,
    uint32_t address,
    uint32_t size
) {
    if (state == nullptr || keys == nullptr || size == 0u) { return false; }
    for (uint32_t offset = 0u; offset < size; ++offset) {
        const uint32_t current = b2r_native_service_cache_address(
            state, address + offset);
        uint32_t slot = (current * 2654435761u) & mask;
        for (;;) {
            const uint32_t key = keys[slot];
            if (key == current) { return true; }
            if (key == 0xffffffffu) { break; }
            slot = (slot + 1u) & mask;
        }
    }
    return false;
}

static uint32_t b2r_native_service_read_u32(
    B2RNativeHostServiceState* state,
    uint32_t address
) {
    const uint32_t cached = b2r_native_service_cache_address(state, address);
    const uint32_t page = cached >> 12u;
    if ((cached & 0xfffu) <= 0xffcu && state->read_pages != nullptr &&
        state->read_pages[page] != nullptr) {
        uint32_t value;
        std::memcpy(&value, state->read_pages[page] + (cached & 0xfffu), 4u);
        return value;
    }
    return state->read_u32(state->memory_user, address);
}

static bool b2r_native_service_try_read_u32(
    B2RNativeHostServiceState* state,
    uint32_t address,
    uint32_t* value
) {
    if (state == nullptr || value == nullptr) { return false; }
    const uint32_t cached = b2r_native_service_cache_address(state, address);
    const uint32_t page = cached >> 12u;
    if ((cached & 0xfffu) > 0xffcu || state->read_pages == nullptr ||
        state->read_pages[page] == nullptr) {
        return false;
    }
    std::memcpy(value, state->read_pages[page] + (cached & 0xfffu), 4u);
    return true;
}

static uint8_t b2r_native_service_read_u8(
    B2RNativeHostServiceState* state,
    uint32_t address
) {
    const uint32_t cached = b2r_native_service_cache_address(state, address);
    const uint32_t page = cached >> 12u;
    if (state->read_pages != nullptr && state->read_pages[page] != nullptr) {
        return state->read_pages[page][cached & 0xfffu];
    }
    return state->read_u8(state->memory_user, address);
}

static void b2r_native_service_mark_dirty(
    B2RNativeHostServiceState* state,
    uint32_t page,
    uint16_t offset,
    uint16_t size
) {
    if (state->dirty_pages == nullptr) { return; }
    if (state->dirty_page_generations != nullptr) {
        if (++state->dirty_page_generations[page] == 0u) {
            state->dirty_page_generations[page] = 1u;
        }
    }
    const uint16_t end = static_cast<uint16_t>(offset + size);
    if (state->dirty_pages[page] == 0u) {
        state->dirty_pages[page] = 1u;
        state->dirty_page_min_offsets[page] = offset;
        state->dirty_page_max_offsets[page] = end;
        if (state->dirty_page_indices != nullptr &&
            *state->dirty_page_count < state->dirty_page_capacity) {
            state->dirty_page_indices[(*state->dirty_page_count)++] = page;
        }
    } else {
        if (offset < state->dirty_page_min_offsets[page]) {
            state->dirty_page_min_offsets[page] = offset;
        }
        if (end > state->dirty_page_max_offsets[page]) {
            state->dirty_page_max_offsets[page] = end;
        }
    }
}

static void b2r_native_service_write_u8(
    B2RNativeHostServiceState* state,
    uint32_t address,
    uint8_t value
) {
    if (state == nullptr) { return; }
    const uint32_t cached = b2r_native_service_cache_address(state, address);
    const uint32_t page = cached >> 12u;
    if (state->read_pages != nullptr &&
        (state->read_pages[page] != nullptr ||
         b2r_native_allocate_pages(state, cached, 1u))) {
        state->read_pages[page][cached & 0xfffu] = value;
        b2r_native_service_mark_dirty(
            state, page, static_cast<uint16_t>(cached & 0xfffu), 1u);
        return;
    }
    if (state->write_u8 != nullptr) {
        state->write_u8(state->memory_user, address, value);
    }
}

static void b2r_native_service_write_u32(
    B2RNativeHostServiceState* state,
    uint32_t address,
    uint32_t value
) {
    if (state == nullptr) { return; }
    const uint32_t cached = b2r_native_service_cache_address(state, address);
    const uint32_t page = cached >> 12u;
    if ((cached & 0xfffu) <= 0xffcu && state->read_pages != nullptr &&
        (state->read_pages[page] != nullptr ||
         b2r_native_allocate_pages(state, cached, 4u))) {
        std::memcpy(state->read_pages[page] + (cached & 0xfffu), &value, 4u);
        b2r_native_service_mark_dirty(
            state, page, static_cast<uint16_t>(cached & 0xfffu), 4u);
        return;
    }
    if (b2r_native_allocate_pages(state, cached, 4u)) {
        for (uint32_t offset = 0u; offset < 4u; ++offset) {
            b2r_native_service_write_u8(
                state, address + offset,
                static_cast<uint8_t>(value >> (offset * 8u)));
        }
        return;
    }
    if (state->write_u32 != nullptr) {
        state->write_u32(state->memory_user, address, value);
    }
}

static void b2r_native_service_write_u16(
    B2RNativeHostServiceState* state,
    uint32_t address,
    uint16_t value
) {
    b2r_native_service_write_u8(
        state, address, static_cast<uint8_t>(value & 0xffu));
    b2r_native_service_write_u8(
        state, address + 1u, static_cast<uint8_t>(value >> 8u));
}

static uint16_t b2r_native_service_read_u16(
    B2RNativeHostServiceState* state,
    uint32_t address
) {
    return static_cast<uint16_t>(
        static_cast<uint16_t>(b2r_native_service_read_u8(state, address)) |
        (static_cast<uint16_t>(
            b2r_native_service_read_u8(state, address + 1u)) << 8u));
}

static void b2r_native_service_write_u64(
    B2RNativeHostServiceState* state,
    uint32_t address,
    uint64_t value
) {
    b2r_native_service_write_u32(
        state, address, static_cast<uint32_t>(value));
    b2r_native_service_write_u32(
        state, address + 4u, static_cast<uint32_t>(value >> 32u));
}

static uint64_t b2r_native_service_read_u64(
    B2RNativeHostServiceState* state,
    uint32_t address
) {
    const uint64_t low = b2r_native_service_read_u32(state, address);
    const uint64_t high = b2r_native_service_read_u32(state, address + 4u);
    return low | (high << 32u);
}

struct B2RSha1Context {
    uint32_t state[5];
    uint32_t count[2];
    uint8_t buffer[64];
};

static uint32_t b2r_sha1_rotate_left(uint32_t value, uint32_t bits) {
    return (value << bits) | (value >> (32u - bits));
}

static void b2r_sha1_transform(B2RSha1Context* context, const uint8_t* block) {
    uint32_t words[80] = {};
    for (uint32_t index = 0u; index < 16u; ++index) {
        const uint32_t offset = index * 4u;
        words[index] =
            (static_cast<uint32_t>(block[offset]) << 24u) |
            (static_cast<uint32_t>(block[offset + 1u]) << 16u) |
            (static_cast<uint32_t>(block[offset + 2u]) << 8u) |
            static_cast<uint32_t>(block[offset + 3u]);
    }
    for (uint32_t index = 16u; index < 80u; ++index) {
        words[index] = b2r_sha1_rotate_left(
            words[index - 3u] ^ words[index - 8u] ^
                words[index - 14u] ^ words[index - 16u],
            1u);
    }

    uint32_t a = context->state[0];
    uint32_t b = context->state[1];
    uint32_t c = context->state[2];
    uint32_t d = context->state[3];
    uint32_t e = context->state[4];
    for (uint32_t index = 0u; index < 80u; ++index) {
        uint32_t function = 0u;
        uint32_t constant = 0u;
        if (index < 20u) {
            function = (b & c) | ((~b) & d);
            constant = 0x5a827999u;
        } else if (index < 40u) {
            function = b ^ c ^ d;
            constant = 0x6ed9eba1u;
        } else if (index < 60u) {
            function = (b & c) | (b & d) | (c & d);
            constant = 0x8f1bbcdcu;
        } else {
            function = b ^ c ^ d;
            constant = 0xca62c1d6u;
        }
        const uint32_t next = b2r_sha1_rotate_left(a, 5u) + function + e +
            constant + words[index];
        e = d;
        d = c;
        c = b2r_sha1_rotate_left(b, 30u);
        b = a;
        a = next;
    }
    context->state[0] += a;
    context->state[1] += b;
    context->state[2] += c;
    context->state[3] += d;
    context->state[4] += e;
}

static void b2r_sha1_init(B2RSha1Context* context) {
    std::memset(context, 0, sizeof(*context));
    context->state[0] = 0x67452301u;
    context->state[1] = 0xefcdab89u;
    context->state[2] = 0x98badcfeu;
    context->state[3] = 0x10325476u;
    context->state[4] = 0xc3d2e1f0u;
}

static void b2r_sha1_update(
    B2RSha1Context* context,
    const uint8_t* payload,
    uint32_t payload_size
) {
    const uint32_t buffer_offset = (context->count[0] >> 3u) & 63u;
    const uint32_t added_bits = payload_size << 3u;
    context->count[0] += added_bits;
    if (context->count[0] < added_bits) { ++context->count[1]; }
    context->count[1] += payload_size >> 29u;

    const uint32_t first_block_size = 64u - buffer_offset;
    uint32_t consumed = 0u;
    if (payload_size >= first_block_size) {
        std::memcpy(
            context->buffer + buffer_offset, payload, first_block_size);
        b2r_sha1_transform(context, context->buffer);
        consumed = first_block_size;
        while (consumed + 63u < payload_size) {
            b2r_sha1_transform(context, payload + consumed);
            consumed += 64u;
        }
    }
    if (consumed < payload_size) {
        const uint32_t destination_offset =
            payload_size >= first_block_size ? 0u : buffer_offset;
        std::memcpy(
            context->buffer + destination_offset,
            payload + consumed,
            payload_size - consumed);
    }
}

static void b2r_sha1_update_guest(
    B2RNativeHostServiceState* state,
    B2RSha1Context* context,
    uint32_t payload_address,
    uint32_t payload_size
) {
    uint8_t chunk[4096] = {};
    uint32_t consumed = 0u;
    while (consumed < payload_size) {
        uint32_t chunk_size = payload_size - consumed;
        if (chunk_size > sizeof(chunk)) {
            chunk_size = static_cast<uint32_t>(sizeof(chunk));
        }
        for (uint32_t index = 0u; index < chunk_size; ++index) {
            chunk[index] = b2r_native_service_read_u8(
                state, payload_address + consumed + index);
        }
        b2r_sha1_update(context, chunk, chunk_size);
        consumed += chunk_size;
    }
}

static void b2r_sha1_final(B2RSha1Context* context, uint8_t digest[20]) {
    uint8_t length[8] = {};
    for (uint32_t index = 0u; index < 8u; ++index) {
        const uint32_t shift = (7u - index) * 8u;
        length[index] = static_cast<uint8_t>(
            shift >= 32u
                ? context->count[1] >> (shift - 32u)
                : context->count[0] >> shift);
    }
    const uint8_t marker = 0x80u;
    const uint8_t zero = 0u;
    b2r_sha1_update(context, &marker, 1u);
    while ((context->count[0] & 504u) != 448u) {
        b2r_sha1_update(context, &zero, 1u);
    }
    b2r_sha1_update(context, length, 8u);
    for (uint32_t index = 0u; index < 20u; ++index) {
        digest[index] = static_cast<uint8_t>(
            context->state[index / 4u] >> ((3u - (index & 3u)) * 8u));
    }
}

constexpr uint32_t B2R_XC_SHA_CONTEXT_OFFSET = 24u;

static void b2r_load_xc_sha_context(
    B2RNativeHostServiceState* state,
    uint32_t address,
    B2RSha1Context* context
) {
    const uint32_t payload = address + B2R_XC_SHA_CONTEXT_OFFSET;
    for (uint32_t index = 0u; index < 5u; ++index) {
        context->state[index] = b2r_native_service_read_u32(
            state, payload + index * 4u);
    }
    context->count[0] = b2r_native_service_read_u32(state, payload + 20u);
    context->count[1] = b2r_native_service_read_u32(state, payload + 24u);
    for (uint32_t index = 0u; index < 64u; ++index) {
        context->buffer[index] = b2r_native_service_read_u8(
            state, payload + 28u + index);
    }
}

static void b2r_store_xc_sha_context(
    B2RNativeHostServiceState* state,
    uint32_t address,
    const B2RSha1Context* context
) {
    const uint32_t payload = address + B2R_XC_SHA_CONTEXT_OFFSET;
    for (uint32_t index = 0u; index < 5u; ++index) {
        b2r_native_service_write_u32(
            state, payload + index * 4u, context->state[index]);
    }
    b2r_native_service_write_u32(state, payload + 20u, context->count[0]);
    b2r_native_service_write_u32(state, payload + 24u, context->count[1]);
    for (uint32_t index = 0u; index < 64u; ++index) {
        b2r_native_service_write_u8(
            state, payload + 28u + index, context->buffer[index]);
    }
}

static void b2r_xc_hmac(
    B2RNativeHostServiceState* state,
    uint32_t key_address,
    uint32_t key_size,
    uint32_t payload_address,
    uint32_t payload_size,
    uint32_t payload2_address,
    uint32_t payload2_size,
    uint32_t digest_address
) {
    uint8_t pad[64] = {};
    const uint32_t bounded_key_size = key_size < 64u ? key_size : 64u;
    for (uint32_t index = 0u; index < bounded_key_size; ++index) {
        pad[index] = b2r_native_service_read_u8(state, key_address + index);
    }
    for (uint32_t index = 0u; index < 64u; ++index) { pad[index] ^= 0x36u; }
    B2RSha1Context context = {};
    b2r_sha1_init(&context);
    b2r_sha1_update(&context, pad, 64u);
    b2r_sha1_update_guest(
        state, &context, payload_address, payload_size);
    b2r_sha1_update_guest(
        state, &context, payload2_address, payload2_size);
    uint8_t inner_digest[20] = {};
    b2r_sha1_final(&context, inner_digest);

    std::memset(pad, 0, sizeof(pad));
    for (uint32_t index = 0u; index < bounded_key_size; ++index) {
        pad[index] = b2r_native_service_read_u8(state, key_address + index);
    }
    for (uint32_t index = 0u; index < 64u; ++index) { pad[index] ^= 0x5cu; }
    b2r_sha1_init(&context);
    b2r_sha1_update(&context, pad, 64u);
    b2r_sha1_update(&context, inner_digest, 20u);
    uint8_t digest[20] = {};
    b2r_sha1_final(&context, digest);
    for (uint32_t index = 0u; index < 20u; ++index) {
        b2r_native_service_write_u8(
            state, digest_address + index, digest[index]);
    }
}

static uint8_t b2r_ascii_lower(uint8_t value) {
    return value >= 'A' && value <= 'Z'
        ? static_cast<uint8_t>(value + ('a' - 'A')) : value;
}

static bool b2r_native_path_ends_with(
    const char* path,
    uint32_t path_length,
    const char* suffix
) {
    uint32_t suffix_length = 0u;
    while (suffix[suffix_length] != '\0') { ++suffix_length; }
    if (suffix_length > path_length) { return false; }
    const uint32_t start = path_length - suffix_length;
    for (uint32_t index = 0u; index < suffix_length; ++index) {
        if (b2r_ascii_lower(static_cast<uint8_t>(path[start + index])) !=
            b2r_ascii_lower(static_cast<uint8_t>(suffix[index]))) {
            return false;
        }
    }
    return true;
}

static bool b2r_native_path_contains(
    const char* path,
    uint32_t path_length,
    const char* needle
) {
    uint32_t needle_length = 0u;
    while (needle[needle_length] != '\0') { ++needle_length; }
    if (needle_length == 0u || needle_length > path_length) { return false; }
    for (uint32_t start = 0u; start + needle_length <= path_length; ++start) {
        bool match = true;
        for (uint32_t index = 0u; index < needle_length; ++index) {
            if (b2r_ascii_lower(static_cast<uint8_t>(path[start + index])) !=
                b2r_ascii_lower(static_cast<uint8_t>(needle[index]))) {
                match = false;
                break;
            }
        }
        if (match) { return true; }
    }
    return false;
}

static bool b2r_native_path_starts_with(
    const char* path,
    uint32_t path_length,
    const char* prefix
) {
    uint32_t prefix_length = 0u;
    while (prefix[prefix_length] != '\0') { ++prefix_length; }
    if (prefix_length > path_length) { return false; }
    for (uint32_t index = 0u; index < prefix_length; ++index) {
        if (b2r_ascii_lower(static_cast<uint8_t>(path[index])) !=
            b2r_ascii_lower(static_cast<uint8_t>(prefix[index]))) {
            return false;
        }
    }
    return true;
}

static bool b2r_native_path_equals(
    const char* path,
    uint32_t path_length,
    const char* expected
) {
    uint32_t expected_length = 0u;
    while (expected[expected_length] != '\0') { ++expected_length; }
    return path_length == expected_length &&
        b2r_native_path_starts_with(path, path_length, expected);
}

static bool b2r_native_path_is_raw_partition_zero(
    const char* path,
    uint32_t path_length
) {
    return b2r_native_path_equals(
        path, path_length, "\\Device\\Harddisk0\\Partition0");
}

static bool b2r_native_path_raw_cache_partition(
    const char* path,
    uint32_t path_length,
    uint32_t* partition
) {
    for (uint32_t candidate = 3u; candidate <= 5u; ++candidate) {
        char expected[40] = {};
        const int written = std::snprintf(
            expected,
            sizeof(expected),
            "\\Device\\Harddisk0\\Partition%u",
            candidate);
        if (written > 0 &&
            static_cast<uint32_t>(written) < sizeof(expected) &&
            b2r_native_path_equals(path, path_length, expected)) {
            if (partition != nullptr) { *partition = candidate; }
            return true;
        }
    }
    return false;
}

static bool b2r_native_path_is_writable(
    const char* path,
    uint32_t path_length
) {
    return
        b2r_native_path_is_raw_partition_zero(path, path_length) ||
        b2r_native_path_raw_cache_partition(path, path_length, nullptr) ||
        b2r_native_path_starts_with(
            path, path_length, "\\Device\\Harddisk0\\Partition0\\") ||
        b2r_native_path_starts_with(
            path, path_length, "\\Device\\Harddisk0\\Partition1\\") ||
        b2r_native_path_starts_with(path, path_length, "\\??\\E:\\") ||
        b2r_native_path_starts_with(path, path_length, "E:\\") ||
        b2r_native_path_starts_with(path, path_length, "\\??\\U:\\") ||
        b2r_native_path_starts_with(path, path_length, "U:\\") ||
        b2r_native_path_starts_with(path, path_length, "\\??\\T:\\") ||
        b2r_native_path_starts_with(path, path_length, "T:\\") ||
        b2r_native_path_starts_with(
            path, path_length, "\\Device\\Harddisk0\\Partition3\\") ||
        b2r_native_path_starts_with(
            path, path_length, "\\Device\\Harddisk0\\Partition4\\") ||
        b2r_native_path_starts_with(
            path, path_length, "\\Device\\Harddisk0\\Partition5\\") ||
        b2r_native_path_starts_with(path, path_length, "\\??\\X:\\") ||
        b2r_native_path_starts_with(path, path_length, "X:\\") ||
        b2r_native_path_starts_with(path, path_length, "\\??\\Y:\\") ||
        b2r_native_path_starts_with(path, path_length, "Y:\\") ||
        b2r_native_path_starts_with(path, path_length, "\\??\\Z:\\") ||
        b2r_native_path_starts_with(path, path_length, "Z:\\");
}

static bool b2r_native_decode_object_path(
    B2RNativeHostServiceState* state,
    uint32_t object_attributes,
    char* path,
    uint32_t capacity,
    uint32_t* path_length
) {
    if (state == nullptr || object_attributes == 0u || path == nullptr ||
        capacity < 2u || path_length == nullptr) {
        return false;
    }
    uint32_t candidates[6] = {object_attributes, 0u, 0u, 0u, 0u, 0u};
    for (uint32_t index = 0u; index < 5u; ++index) {
        candidates[index + 1u] = b2r_native_service_read_u32(
            state, object_attributes + index * 4u);
    }
    for (uint32_t candidate_index = 0u; candidate_index < 6u;
         ++candidate_index) {
        const uint32_t descriptor = candidates[candidate_index];
        if (descriptor == 0u) { continue; }
        const uint32_t length =
            static_cast<uint32_t>(b2r_native_service_read_u8(state, descriptor)) |
            (static_cast<uint32_t>(
                b2r_native_service_read_u8(state, descriptor + 1u)) << 8u);
        const uint32_t maximum_length =
            static_cast<uint32_t>(
                b2r_native_service_read_u8(state, descriptor + 2u)) |
            (static_cast<uint32_t>(
                b2r_native_service_read_u8(state, descriptor + 3u)) << 8u);
        const uint32_t buffer = b2r_native_service_read_u32(
            state, descriptor + 4u);
        if (length == 0u || length > maximum_length || length >= capacity ||
            buffer == 0u) {
            continue;
        }
        bool path_like = false;
        bool valid = true;
        for (uint32_t index = 0u; index < length; ++index) {
            const uint8_t value = b2r_native_service_read_u8(
                state, buffer + index);
            if (value < 0x20u || value > 0x7eu) {
                valid = false;
                break;
            }
            path[index] = static_cast<char>(value);
            path_like = path_like || value == '\\' || value == '/' || value == ':';
        }
        if (!valid || !path_like) { continue; }
        path[length] = '\0';
        *path_length = length;
        return true;
    }
    return false;
}

static bool b2r_native_build_extracted_path(
    B2RNativeHostServiceState* state,
    const char* guest_path,
    uint32_t guest_length,
    char* host_path,
    uint32_t host_capacity
) {
    if (state == nullptr || guest_path == nullptr || guest_length == 0u ||
        host_path == nullptr || host_capacity < 2u) {
        return false;
    }
    if (b2r_native_path_is_raw_partition_zero(guest_path, guest_length)) {
        const char* root = state->cache_data_root;
        if (root[0] == '\0') { return false; }
        const size_t root_length = std::strlen(root);
        const char separator = root_length != 0u &&
            (root[root_length - 1u] == '\\' || root[root_length - 1u] == '/')
            ? '\0' : '\\';
        const int written = separator == '\0'
            ? std::snprintf(
                host_path, host_capacity, "%spartition0.bin", root)
            : std::snprintf(
                host_path, host_capacity, "%s\\partition0.bin", root);
        return written > 0 && static_cast<uint32_t>(written) < host_capacity;
    }
    uint32_t raw_cache_partition = 0u;
    if (b2r_native_path_raw_cache_partition(
            guest_path, guest_length, &raw_cache_partition)) {
        const char* root = state->cache_data_root;
        if (root[0] == '\0') { return false; }
        const size_t root_length = std::strlen(root);
        const char separator = root_length != 0u &&
            (root[root_length - 1u] == '\\' || root[root_length - 1u] == '/')
            ? '\0' : '\\';
        const int written = separator == '\0'
            ? std::snprintf(
                host_path,
                host_capacity,
                "%spartition%u.bin",
                root,
                raw_cache_partition)
            : std::snprintf(
                host_path,
                host_capacity,
                "%s\\partition%u.bin",
                root,
                raw_cache_partition);
        return written > 0 && static_cast<uint32_t>(written) < host_capacity;
    }
    auto strip_prefix = [guest_path, guest_length](
        const char* prefix,
        uint32_t* relative_start
    ) -> bool {
        uint32_t prefix_length = 0u;
        while (prefix[prefix_length] != '\0') { ++prefix_length; }
        if (prefix_length > guest_length) { return false; }
        for (uint32_t index = 0u; index < prefix_length; ++index) {
            if (b2r_ascii_lower(static_cast<uint8_t>(guest_path[index])) !=
                b2r_ascii_lower(static_cast<uint8_t>(prefix[index]))) {
                return false;
            }
        }
        *relative_start = prefix_length;
        return true;
    };
    const char* root = state->extracted_root;
    const char* alias = nullptr;
    uint32_t relative_start = 0u;
    if (strip_prefix("\\Device\\Cdrom0\\", &relative_start) ||
        strip_prefix("\\??\\D:\\", &relative_start) ||
        strip_prefix("D:\\", &relative_start) ||
        strip_prefix("Cdrom0:\\", &relative_start)) {
        root = state->extracted_root;
    } else if (strip_prefix(
                   "\\Device\\Harddisk0\\Partition0\\", &relative_start) ||
               strip_prefix(
                   "\\Device\\Harddisk0\\Partition1\\", &relative_start) ||
               strip_prefix("\\??\\E:\\", &relative_start) ||
               strip_prefix("E:\\", &relative_start)) {
        root = state->save_data_root;
    } else if (strip_prefix("\\??\\U:\\", &relative_start) ||
               strip_prefix("U:\\", &relative_start)) {
        root = state->save_data_root;
        alias = "UDATA";
    } else if (strip_prefix("\\??\\T:\\", &relative_start) ||
               strip_prefix("T:\\", &relative_start)) {
        root = state->save_data_root;
        alias = "TDATA";
    } else if (strip_prefix(
                   "\\Device\\Harddisk0\\Partition2\\", &relative_start) ||
               strip_prefix("\\??\\C:\\", &relative_start) ||
               strip_prefix("C:\\", &relative_start)) {
        root = state->dashboard_data_root;
    } else if (strip_prefix(
                   "\\Device\\Harddisk0\\Partition3\\", &relative_start) ||
               strip_prefix(
                   "\\Device\\Harddisk0\\Partition4\\", &relative_start) ||
               strip_prefix(
                   "\\Device\\Harddisk0\\Partition5\\", &relative_start) ||
               strip_prefix("\\??\\X:\\", &relative_start) ||
               strip_prefix("\\??\\Y:\\", &relative_start) ||
               strip_prefix("\\??\\Z:\\", &relative_start) ||
               strip_prefix("X:\\", &relative_start) ||
               strip_prefix("Y:\\", &relative_start) ||
               strip_prefix("Z:\\", &relative_start)) {
        root = state->cache_data_root;
    } else if (guest_length >= 2u && guest_path[1] == ':') {
        return false;
    }
    while (relative_start < guest_length &&
           (guest_path[relative_start] == '\\' ||
            guest_path[relative_start] == '/')) {
        ++relative_start;
    }
    if (root[0] == '\0') { return false; }
    for (uint32_t index = relative_start; index < guest_length; ++index) {
        if (guest_path[index] == ':') { return false; }
        const bool segment_start = index == relative_start ||
            guest_path[index - 1u] == '\\' || guest_path[index - 1u] == '/';
        if (segment_start && guest_path[index] == '.' &&
            index + 1u < guest_length && guest_path[index + 1u] == '.' &&
            (index + 2u == guest_length ||
             guest_path[index + 2u] == '\\' ||
             guest_path[index + 2u] == '/')) {
            return false;
        }
    }
    uint32_t host_length = 0u;
    while (root[host_length] != '\0' &&
           host_length + 2u < host_capacity) {
        host_path[host_length] = root[host_length];
        ++host_length;
    }
    if (root[host_length] != '\0') { return false; }
    if (host_length != 0u && host_path[host_length - 1u] != '\\' &&
        host_path[host_length - 1u] != '/') {
        host_path[host_length++] = '\\';
    }
    if (alias != nullptr) {
        for (uint32_t index = 0u; alias[index] != '\0'; ++index) {
            if (host_length + 1u >= host_capacity) { return false; }
            host_path[host_length++] = alias[index];
        }
        if (host_length + 1u >= host_capacity) { return false; }
        host_path[host_length++] = '\\';
        if (state->title_id != 0u) {
            const int written = std::snprintf(
                host_path + host_length,
                host_capacity - host_length,
                "%08X\\",
                state->title_id);
            if (written <= 0 ||
                static_cast<uint32_t>(written) >= host_capacity - host_length) {
                return false;
            }
            host_length += static_cast<uint32_t>(written);
        }
    }
    for (uint32_t index = relative_start; index < guest_length; ++index) {
        if (host_length + 1u >= host_capacity) { return false; }
        host_path[host_length++] =
            guest_path[index] == '/' ? '\\' : guest_path[index];
    }
    host_path[host_length] = '\0';
    return true;
}

static B2RNativeFileEntry* b2r_find_native_file(
    B2RNativeHostServiceState* state,
    uint32_t handle
) {
    if (state == nullptr || handle == 0u) { return nullptr; }
    for (uint32_t index = 0u; index < state->file_count; ++index) {
        B2RNativeFileEntry* file = &state->files[index];
        if (file->active != 0u && file->handle == handle) { return file; }
    }
    return nullptr;
}

static B2RNativeFileEntry* b2r_register_native_file(
    B2RNativeHostServiceState* state,
    uint32_t flags,
    uint64_t size,
    std::FILE* host_file = nullptr,
    const char* guest_path = nullptr,
    const char* host_path = nullptr
) {
    if (state == nullptr) { return nullptr; }
    B2RNativeFileEntry* file = nullptr;
    for (uint32_t index = 0u; index < state->file_count; ++index) {
        if (state->files[index].active == 0u) {
            file = &state->files[index];
            break;
        }
    }
    if (file == nullptr && state->file_count >= B2R_NATIVE_FILE_CAPACITY) {
        ++state->file_overflow_count;
        return nullptr;
    }
    if (file == nullptr) { file = &state->files[state->file_count++]; }
    file->handle = b2r_allocate_native_object_handle(state);
    file->flags = flags;
    file->size = size;
    file->position = 0u;
    file->host_file = static_cast<uint64_t>(
        reinterpret_cast<uintptr_t>(host_file));
    file->active = 1u;
    file->reserved = 0u;
    file->directory_index = 0u;
    file->directory_initialized = 0u;
    std::snprintf(
        file->guest_path,
        sizeof(file->guest_path),
        "%s",
        guest_path != nullptr ? guest_path : "");
    std::snprintf(
        file->host_path,
        sizeof(file->host_path),
        "%s",
        host_path != nullptr ? host_path : "");
    file->directory_pattern[0] = '\0';
    return file;
}

static char b2r_native_ascii_lower(char value) {
    return value >= 'A' && value <= 'Z'
        ? static_cast<char>(value + ('a' - 'A')) : value;
}

static int b2r_native_compare_names(const char* left, const char* right) {
    uint32_t index = 0u;
    while (left[index] != '\0' && right[index] != '\0') {
        const char left_lower = b2r_native_ascii_lower(left[index]);
        const char right_lower = b2r_native_ascii_lower(right[index]);
        if (left_lower != right_lower) {
            return left_lower < right_lower ? -1 : 1;
        }
        ++index;
    }
    if (left[index] == right[index]) { return 0; }
    return left[index] == '\0' ? -1 : 1;
}

static bool b2r_native_wildcard_match(
    const char* pattern,
    const char* value
) {
    const char* star = nullptr;
    const char* retry = nullptr;
    while (*value != '\0') {
        if (*pattern == '?' ||
            b2r_native_ascii_lower(*pattern) ==
                b2r_native_ascii_lower(*value)) {
            ++pattern;
            ++value;
        } else if (*pattern == '*') {
            star = pattern++;
            retry = value;
        } else if (star != nullptr) {
            pattern = star + 1;
            value = ++retry;
        } else {
            return false;
        }
    }
    while (*pattern == '*') { ++pattern; }
    return *pattern == '\0';
}

static bool b2r_is_native_raw_u32_register(uint32_t address) {
    if (address >= 0xfec00000u && address <= 0xfec00ffcu) {
        return true;
    }
    switch (address) {
    case 0x00100410u:  // GPU software completion flag
    case 0x002256b8u:  // synthetic D3D context global
    case 0x002256c0u:  // GPU submission base
    case 0x002256c4u:  // GPU submission limit
    case 0x005a6e0cu:  // cleanup-list sentinel
    case 0x005a7058u:  // frontend resource-cache sentinel
    case 0x005a70a4u:  // frontend registry-list sentinel
    case 0x005a727cu:  // frontend initializer-list sentinel
    case 0x21b73000u:  // synthetic D3D get-pointer slot
    case 0x21b8c000u:  // GPU progress counter
    case 0x31ff0000u:  // synthetic DirectSound workspace
    case 0xfd002400u:  // GPU PFIFO runout status
    case 0xfd003214u:  // GPU PFIFO cache status
    case 0xfe820010u:  // MCPX frame counter
    case 0xfec0012cu:  // audio DSP control
    case 0xfec00130u:  // audio DSP status
        return true;
    default:
        return false;
    }
}

static uint64_t b2r_native_known_file_size(
    const char* path,
    uint32_t path_length
) {
    if (b2r_native_path_ends_with(path, path_length, "sample.xsb")) {
        return 152u;
    }
    if (b2r_native_path_ends_with(path, path_length, "b2distfx.bin")) {
        return 21756u;
    }
    return 0u;
}

static uint32_t b2r_native_write_file_information(
    B2RNativeHostServiceState* state,
    const B2RNativeFileEntry* file,
    uint32_t destination,
    uint32_t requested_length,
    uint32_t information_class
) {
    if (state == nullptr || file == nullptr || destination == 0u ||
        requested_length == 0u) {
        return 0u;
    }
    const bool is_directory = (file->flags & 1u) != 0u;
    const uint64_t allocation_size = file->size != 0u
        ? (file->size + 0x7ffu) & ~0x7ffull : 0u;
    uint32_t payload_size = 24u;
    if (information_class == 4u) { payload_size = 40u; }
    else if (information_class == 34u) { payload_size = 56u; }
    else if (information_class == 14u || information_class == 19u ||
             information_class == 20u) { payload_size = 8u; }
    else if (information_class == 18u) { payload_size = 76u; }
    const uint32_t write_size = requested_length < payload_size
        ? requested_length : payload_size;
    for (uint32_t index = 0u; index < write_size; ++index) {
        b2r_native_service_write_u8(state, destination + index, 0u);
    }
    auto write_if_present = [&](uint32_t offset, uint32_t value) {
        if (offset + 4u <= write_size) {
            b2r_native_service_write_u32(state, destination + offset, value);
        }
    };
    auto write_u64_if_present = [&](uint32_t offset, uint64_t value) {
        if (offset + 8u <= write_size) {
            b2r_native_service_write_u64(state, destination + offset, value);
        }
    };
    if (information_class == 4u) {
        write_if_present(32u, is_directory ? 0x10u : 0x80u);
    } else if (information_class == 34u) {
        write_u64_if_present(32u, allocation_size);
        write_u64_if_present(40u, file->size);
        write_if_present(48u, is_directory ? 0x10u : 0x80u);
    } else if (information_class == 14u) {
        write_u64_if_present(0u, file->position);
    } else if (information_class == 19u) {
        write_u64_if_present(0u, allocation_size);
    } else if (information_class == 20u) {
        write_u64_if_present(0u, file->size);
    } else {
        const uint32_t standard_offset = information_class == 18u ? 40u : 0u;
        write_u64_if_present(standard_offset, allocation_size);
        write_u64_if_present(standard_offset + 8u, file->size);
        write_if_present(standard_offset + 16u, 1u);
        if (standard_offset + 22u <= write_size) {
            b2r_native_service_write_u8(
                state, destination + standard_offset + 21u,
                is_directory ? 1u : 0u);
        }
        if (information_class == 18u) {
            write_if_present(72u, 0u);
        }
    }
    return write_size;
}

extern "C" __declspec(dllexport) bool b2r_native_memory_write_u32(
    void* opaque,
    uint32_t address,
    uint32_t value
) {
    B2RNativeHostServiceState* state =
        static_cast<B2RNativeHostServiceState*>(opaque);
    constexpr uint32_t kGpuCommandKick = 0x80000000u;
    constexpr uint32_t kNv2aStatusPoll = 0xfd100410u;
    constexpr uint32_t kNv2aStatusBusyBit = 0x00010000u;
    constexpr uint32_t kGpuInterruptStatus = 0xfd400100u;
    constexpr uint32_t kGpuPfifoInterruptStatus = 0xfd002100u;
    constexpr uint32_t kAudioDspControl = 0xfec0012cu;
    constexpr uint32_t kAudioDspStatus = 0xfec00130u;
    constexpr uint32_t kAudioDspResetRequest = 0x2u;
    constexpr uint32_t kAudioDspResetReady = 0x100u;
    constexpr uint32_t kGpuSubmissionBase = 0x002256c0u;
    constexpr uint32_t kDmaPointerOffset = 0x17f4u;
    constexpr uint32_t kDmaStatusOffset = 0x44u;
    constexpr uint32_t kCompletionMask = 0x0fffffffu;
    if (state == nullptr) { return false; }
    if (address == kNv2aStatusPoll) {
        if (!b2r_native_allocate_pages(state, address, 4u)) { return false; }
        b2r_native_service_write_u32(
            state, address, value & ~kNv2aStatusBusyBit);
        ++state->native_memory_write_count;
        return true;
    }
    if (address == kGpuInterruptStatus ||
        address == kGpuPfifoInterruptStatus) {
        uint32_t current = 0u;
        b2r_native_service_try_read_u32(state, address, &current);
        if (!b2r_native_allocate_pages(state, address, 4u)) { return false; }
        b2r_native_service_write_u32(state, address, current & ~value);
        ++state->native_memory_write_count;
        return true;
    }
    if (address == kAudioDspControl) {
        if (!b2r_native_allocate_pages(state, address, 4u)) { return false; }
        b2r_native_service_write_u32(state, address, value);
        if (value & kAudioDspResetRequest) {
            uint32_t status = 0u;
            b2r_native_service_try_read_u32(state, kAudioDspStatus, &status);
            if (b2r_native_allocate_pages(state, kAudioDspStatus, 4u)) {
                b2r_native_service_write_u32(
                    state, kAudioDspStatus, status | kAudioDspResetReady);
            }
        }
        ++state->native_memory_write_count;
        return true;
    }
    if (b2r_is_native_raw_u32_register(address)) {
        if (!b2r_native_allocate_pages(state, address, 4u)) { return false; }
        b2r_native_service_write_u32(state, address, value);
        ++state->native_memory_write_count;
        return true;
    }
    if (address != kGpuCommandKick) {
        if (b2r_native_service_has_exact_callback(
                state, state->write_callback_address_keys,
                state->write_callback_address_mask, address, 4u) ||
            !b2r_native_allocate_pages(state, address, 4u)) {
            return false;
        }
        for (uint32_t offset = 0u; offset < 4u; ++offset) {
            b2r_native_service_write_u8(
                state, address + offset,
                static_cast<uint8_t>(value >> (offset * 8u)));
        }
        ++state->native_page_miss_count;
        return true;
    }

    if (!b2r_native_allocate_pages(state, address, 4u)) { return false; }
    b2r_native_service_write_u32(state, address, value);
    ++state->native_memory_write_count;
    ++state->native_gpu_command_kick_count;
    state->native_gpu_master_interrupt_pending = 1u;

    uint32_t dma_state = 0u;
    uint32_t submitted = 0u;
    if (b2r_native_service_try_read_u32(
            state, kGpuSubmissionBase + kDmaPointerOffset, &dma_state) &&
        b2r_native_service_try_read_u32(
            state, kGpuSubmissionBase, &submitted) &&
        dma_state != 0u && submitted != 0u) {
        const uint32_t completion = dma_state + kDmaStatusOffset;
        if (completion >= dma_state &&
            b2r_native_allocate_pages(state, completion, 4u)) {
            b2r_native_service_write_u32(
                state, completion, submitted & kCompletionMask);
            ++state->native_gpu_completion_count;
        }
    }
    return true;
}

extern "C" __declspec(dllexport) bool b2r_native_memory_read_u32(
    void* opaque,
    uint32_t address,
    uint32_t* value
) {
    B2RNativeHostServiceState* state =
        static_cast<B2RNativeHostServiceState*>(opaque);
    constexpr uint32_t kGpuMasterInterruptStatus = 0xfd000100u;
    constexpr uint32_t kGpuMasterPfifoPending = 0x01000000u;
    constexpr uint32_t kGpuPfifoRunoutStatus = 0xfd002400u;
    constexpr uint32_t kGpuPfifoCache1Status = 0xfd003214u;
    constexpr uint32_t kGpuPfifoIdle = 0x10u;
    constexpr uint32_t kGpuProgressCounter = 0x21b8c000u;
    constexpr uint32_t kGpuSoftwareCompletion = 0x00100410u;
    constexpr uint32_t kGpuSoftwareCompletionPending = 0x00010000u;
    constexpr uint32_t kMcpxFrameCounter = 0xfe820010u;
    constexpr uint32_t kAudioDspControl = 0xfec0012cu;
    constexpr uint32_t kAudioDspStatus = 0xfec00130u;
    constexpr uint32_t kAudioDspResetRequest = 0x2u;
    constexpr uint32_t kAudioDspResetReady = 0x100u;
    if (state == nullptr || value == nullptr) { return false; }

    uint32_t current = 0u;
    b2r_native_service_try_read_u32(state, address, &current);
    if (address == kGpuMasterInterruptStatus) {
        *value = state->native_gpu_master_interrupt_pending != 0u
            ? kGpuMasterPfifoPending : 0u;
        if (state->native_gpu_master_interrupt_pending != 0u) {
            state->native_gpu_master_interrupt_pending = 0u;
            ++state->native_gpu_master_interrupt_count;
        }
    } else if (address == kGpuPfifoRunoutStatus ||
               address == kGpuPfifoCache1Status) {
        *value = kGpuPfifoIdle;
    } else if (address == kGpuProgressCounter) {
        *value = current != 0u ? current + 1u : 0u;
    } else if (address == kGpuSoftwareCompletion) {
        *value = current & ~kGpuSoftwareCompletionPending;
    } else if (address == kMcpxFrameCounter) {
        *value = current + 4u;
    } else if (address == kAudioDspStatus) {
        uint32_t control = 0u;
        b2r_native_service_try_read_u32(state, kAudioDspControl, &control);
        *value = control & kAudioDspResetRequest
            ? current | kAudioDspResetReady : current;
    } else if (b2r_is_native_raw_u32_register(address)) {
        *value = current;
    } else {
        if (b2r_native_service_has_exact_callback(
                state, state->callback_address_keys,
                state->callback_address_mask, address, 4u) ||
            !b2r_native_allocate_pages(state, address, 4u)) {
            return false;
        }
        *value = 0u;
        for (uint32_t offset = 0u; offset < 4u; ++offset) {
            *value |= static_cast<uint32_t>(
                b2r_native_service_read_u8(state, address + offset))
                << (offset * 8u);
        }
        ++state->native_page_miss_count;
        return true;
    }
    if (address != kGpuMasterInterruptStatus &&
        b2r_native_allocate_pages(state, address, 4u)) {
        b2r_native_service_write_u32(state, address, *value);
    }
    ++state->native_memory_read_count;
    return true;
}

extern "C" __declspec(dllexport) void b2r_native_observe(
    void* opaque,
    void* context_pointer
) {
    B2RNativeHostServiceState* state =
        static_cast<B2RNativeHostServiceState*>(opaque);
    B2RContext* context = static_cast<B2RContext*>(context_pointer);
    if (state == nullptr) { return; }
    ++state->native_observer_count;
    constexpr uint32_t kFrontendRecordTableScan = 0x00112873u;
    constexpr uint32_t kFrontendRecordTableMaxCount = 0x00010000u;
    constexpr uint32_t kWorldDrawCallback = 0x000c4a70u;
    if (context != nullptr && context->eip == kWorldDrawCallback) {
        const uint32_t caller = b2r_native_service_read_u32(
            state, context->esp);
        const uint32_t mesh_entry = b2r_native_service_read_u32(
            state, context->esp + 4u);
        const uint32_t index_data = mesh_entry != 0u
            ? b2r_native_service_read_u32(state, mesh_entry + 0x0cu)
            : 0u;
        const uint32_t index_count = mesh_entry != 0u
            ? b2r_native_service_read_u32(state, mesh_entry + 0x10u)
            : 0u;
        const uint32_t owner_mode = context->ecx != 0u
            ? b2r_native_service_read_u32(state, context->ecx + 0x14u)
            : 0u;
        const uint32_t sample_index =
            state->native_traffic_mesh_sample_cursor
                % B2R_NATIVE_TRAFFIC_MESH_SAMPLE_CAPACITY;
        B2RNativeTrafficMeshSample* sample =
            &state->native_traffic_mesh_samples[sample_index];
        sample->caller = caller;
        sample->owner = context->ecx;
        sample->owner_mode = owner_mode;
        sample->mesh_entry = mesh_entry;
        sample->index_data = index_data;
        sample->index_count = index_count;
        ++state->native_traffic_mesh_sample_cursor;
        if (state->native_traffic_mesh_sample_count <
                B2R_NATIVE_TRAFFIC_MESH_SAMPLE_CAPACITY) {
            ++state->native_traffic_mesh_sample_count;
        }
        ++state->native_traffic_world_draw_count;
        state->native_traffic_world_zero_index_count +=
            index_count == 0u ? 1u : 0u;
        return;
    }
    if (!state->normal_runtime_enabled || context == nullptr ||
        context->eip != kFrontendRecordTableScan || context->edi == 0u) {
        return;
    }
    const uint32_t count_address = context->edi + 4u;
    const uint32_t count = b2r_native_service_read_u32(state, count_address);
    state->native_frontend_record_table_last_address = count_address;
    state->native_frontend_record_table_last_count = count;
    if (count <= kFrontendRecordTableMaxCount) { return; }
    b2r_native_service_write_u32(state, count_address, 0u);
    ++state->native_frontend_record_table_repair_count;
}

extern "C" __declspec(dllexport) bool b2r_native_memory_write_u8(
    void* opaque,
    uint32_t address,
    uint8_t value
) {
    B2RNativeHostServiceState* state =
        static_cast<B2RNativeHostServiceState*>(opaque);
    constexpr uint32_t kAudioVoiceCommand0 = 0xfec0011bu;
    constexpr uint32_t kAudioVoiceCommand1 = 0xfec0017bu;
    constexpr uint8_t kAudioVoiceCommandPending = 0x2u;
    if (state == nullptr) { return false; }
    if (state->normal_runtime_enabled &&
        address != kAudioVoiceCommand0 && address != kAudioVoiceCommand1 &&
        b2r_native_service_has_exact_callback(
            state, state->write_callback_address_keys,
            state->write_callback_address_mask, address, 1u)) {
        if (!b2r_native_allocate_pages(state, address, 1u)) { return false; }
        b2r_native_service_write_u8(state, address, value);
        ++state->native_memory_write_count;
        return true;
    }
    if (address != kAudioVoiceCommand0 && address != kAudioVoiceCommand1) {
        if (b2r_native_service_has_exact_callback(
                state, state->write_callback_address_keys,
                state->write_callback_address_mask, address, 1u) ||
            !b2r_native_allocate_pages(state, address, 1u)) {
            return false;
        }
        b2r_native_service_write_u8(state, address, value);
        ++state->native_page_miss_count;
        return true;
    }
    if (!b2r_native_allocate_pages(state, address, 1u)) { return false; }
    b2r_native_service_write_u8(
        state,
        address,
        static_cast<uint8_t>(value & ~kAudioVoiceCommandPending));
    ++state->native_memory_write_count;
    return true;
}

extern "C" __declspec(dllexport) bool b2r_native_memory_read_u8(
    void* opaque,
    uint32_t address,
    uint8_t* value
) {
    B2RNativeHostServiceState* state =
        static_cast<B2RNativeHostServiceState*>(opaque);
    constexpr uint32_t kAudioVoiceCommand0 = 0xfec0011bu;
    constexpr uint32_t kAudioVoiceCommand1 = 0xfec0017bu;
    constexpr uint32_t kGpuPfifoRunoutStatus = 0xfd002400u;
    constexpr uint32_t kGpuPfifoCache1Status = 0xfd003214u;
    constexpr uint32_t kMcpxFrameCounter = 0xfe820010u;
    constexpr uint8_t kGpuPfifoIdle = 0x10u;
    if (state == nullptr || value == nullptr) { return false; }
    if (address == kGpuPfifoRunoutStatus ||
        address == kGpuPfifoCache1Status) {
        if (!b2r_native_allocate_pages(state, address, 1u)) { return false; }
        *value = kGpuPfifoIdle;
        b2r_native_service_write_u8(state, address, *value);
        ++state->native_memory_read_count;
        return true;
    }
    if (address == kMcpxFrameCounter) {
        uint32_t current = 0u;
        b2r_native_service_try_read_u32(state, address, &current);
        current += 4u;
        if (!b2r_native_allocate_pages(state, address, 4u)) { return false; }
        b2r_native_service_write_u32(state, address, current);
        *value = static_cast<uint8_t>(current);
        ++state->native_memory_read_count;
        return true;
    }
    if (state->normal_runtime_enabled &&
        address != kAudioVoiceCommand0 && address != kAudioVoiceCommand1 &&
        b2r_native_service_has_exact_callback(
            state, state->callback_address_keys,
            state->callback_address_mask, address, 1u)) {
        if (!b2r_native_allocate_pages(state, address, 1u)) { return false; }
        *value = b2r_native_service_read_u8(state, address);
        ++state->native_memory_read_count;
        return true;
    }
    if (address != kAudioVoiceCommand0 && address != kAudioVoiceCommand1) {
        if (b2r_native_service_has_exact_callback(
                state, state->callback_address_keys,
                state->callback_address_mask, address, 1u) ||
            !b2r_native_allocate_pages(state, address, 1u)) {
            return false;
        }
        *value = b2r_native_service_read_u8(state, address);
        ++state->native_page_miss_count;
        return true;
    }
    const uint32_t cached = b2r_native_service_cache_address(state, address);
    const uint32_t page = cached >> 12u;
    *value = state->read_pages != nullptr && state->read_pages[page] != nullptr
        ? state->read_pages[page][cached & 0xfffu] : 0u;
    ++state->native_memory_read_count;
    return true;
}

static void b2r_native_service_copy_bytes(
    B2RNativeHostServiceState* state,
    uint32_t destination,
    uint32_t source,
    uint32_t size
) {
    while (size != 0u) {
        const uint32_t cached_destination =
            b2r_native_service_cache_address(state, destination);
        const uint32_t cached_source =
            b2r_native_service_cache_address(state, source);
        const uint32_t destination_offset = cached_destination & 0xfffu;
        const uint32_t source_offset = cached_source & 0xfffu;
        uint32_t chunk = 0x1000u - destination_offset;
        if (chunk > 0x1000u - source_offset) {
            chunk = 0x1000u - source_offset;
        }
        if (chunk > size) { chunk = size; }
        const uint32_t destination_page = cached_destination >> 12u;
        const uint32_t source_page = cached_source >> 12u;
        uint8_t* destination_bytes = state->read_pages != nullptr
            ? state->read_pages[destination_page] : nullptr;
        uint8_t* source_bytes = state->read_pages != nullptr
            ? state->read_pages[source_page] : nullptr;
        if (destination_bytes != nullptr && source_bytes != nullptr) {
            std::memmove(
                destination_bytes + destination_offset,
                source_bytes + source_offset,
                chunk);
            b2r_native_service_mark_dirty(
                state, destination_page,
                static_cast<uint16_t>(destination_offset),
                static_cast<uint16_t>(chunk));
        } else {
            for (uint32_t index = 0u; index < chunk; ++index) {
                b2r_native_service_write_u8(
                    state, destination + index,
                    b2r_native_service_read_u8(state, source + index));
            }
        }
        destination += chunk;
        source += chunk;
        size -= chunk;
    }
}

static char b2r_ascii_lower(char value) {
    return value >= 'A' && value <= 'Z'
        ? static_cast<char>(value + ('a' - 'A')) : value;
}

static bool b2r_path_is_track_pss(const char* path, uint32_t length) {
    constexpr char kMarker[] = "trackpss";
    for (uint32_t start = 0u; start + sizeof(kMarker) < length; ++start) {
        if (start != 0u && path[start - 1u] != '\\' &&
            path[start - 1u] != '/') {
            continue;
        }
        bool marker_matches = true;
        for (uint32_t index = 0u; index + 1u < sizeof(kMarker); ++index) {
            if (b2r_ascii_lower(path[start + index]) != kMarker[index]) {
                marker_matches = false;
                break;
            }
        }
        const uint32_t direction_start = start + sizeof(kMarker);
        if (!marker_matches || direction_start >= length) { continue; }
        constexpr char kForward[] = "forward";
        constexpr char kReverse[] = "reverse";
        const auto direction_matches = [&](const char* direction,
                                           uint32_t direction_length) {
            if (direction_start + direction_length >= length) { return false; }
            for (uint32_t index = 0u; index < direction_length; ++index) {
                if (b2r_ascii_lower(path[direction_start + index]) !=
                    direction[index]) {
                    return false;
                }
            }
            return path[direction_start + direction_length] == '\\' ||
                path[direction_start + direction_length] == '/';
        };
        uint32_t filename_start = 0u;
        if (direction_matches(kForward, sizeof(kForward) - 1u)) {
            filename_start = direction_start + sizeof(kForward);
        } else if (direction_matches(kReverse, sizeof(kReverse) - 1u)) {
            filename_start = direction_start + sizeof(kReverse);
        } else {
            continue;
        }
        if (filename_start >= length || length - filename_start < 5u) {
            continue;
        }
        bool nested = false;
        for (uint32_t index = filename_start; index < length; ++index) {
            if (path[index] == '\\' || path[index] == '/') {
                nested = true;
                break;
            }
        }
        if (nested || path[length - 4u] != '.' ||
            b2r_ascii_lower(path[length - 3u]) != 'p' ||
            b2r_ascii_lower(path[length - 2u]) != 's' ||
            b2r_ascii_lower(path[length - 1u]) != 's') {
            continue;
        }
        return true;
    }
    return false;
}

static bool b2r_path_is_traffic_tra(const char* path, uint32_t length) {
    constexpr char kSuffix[] = "traffic/traffic.tra";
    constexpr uint32_t kSuffixLength = sizeof(kSuffix) - 1u;
    if (path == nullptr || length < kSuffixLength) { return false; }
    const uint32_t start = length - kSuffixLength;
    if (start != 0u && path[start - 1u] != '\\' &&
        path[start - 1u] != '/' && path[start - 1u] != ':') {
        return false;
    }
    for (uint32_t index = 0u; index < kSuffixLength; ++index) {
        const char expected = kSuffix[index];
        const char actual = path[start + index];
        if (expected == '/') {
            if (actual != '/' && actual != '\\') { return false; }
        } else if (b2r_ascii_lower(actual) != expected) {
            return false;
        }
    }
    return true;
}

static B2RNativeTitleAssetStreamEntry* b2r_find_title_asset_stream(
    B2RNativeHostServiceState* state,
    uint32_t object
) {
    if (state == nullptr || object < 0x31f10000u) { return nullptr; }
    const uint32_t offset = object - 0x31f10000u;
    if ((offset & 0xfffu) != 0u) { return nullptr; }
    const uint32_t index = offset >> 12u;
    if (index >= state->title_asset_stream_count ||
        index >= B2R_NATIVE_TITLE_ASSET_STREAM_CAPACITY) {
        return nullptr;
    }
    B2RNativeTitleAssetStreamEntry* stream =
        &state->title_asset_streams[index];
    return stream->object == object ? stream : nullptr;
}

static bool b2r_publish_track_pss_image(
    B2RNativeHostServiceState* state,
    B2RNativeTitleAssetStreamEntry* stream
) {
    constexpr uint32_t kTrackPss = 1u;
    constexpr uint32_t kPublicationAttempted = 2u;
    constexpr uint32_t kPublished = 4u;
    constexpr uint32_t kHeaderSize = 0x68u;
    constexpr uint32_t kImageAnchorPointerOffset = 0x14u;
    constexpr uint32_t kImageAnchorOffset = 0x1c0u;
    constexpr uint32_t kDescriptorCountOffset = 0x5cu;
    constexpr uint32_t kDescriptorPointerOffsets[2] = {0x60u, 0x64u};
    constexpr uint32_t kDescriptorSize = 8u;
    constexpr uint32_t kMaxDescriptorCount = 4096u;
    constexpr uint32_t kMaxStreamChunkSize = 0x3c000u;
    constexpr uint32_t kSceneRecordSize = 0x78u;
    constexpr uint32_t kMaxSceneRecordCount = 4096u;
    constexpr uint32_t kSceneTableOffsets[3][2] = {
        {0x18u, 0x1cu}, {0x20u, 0x24u}, {0x28u, 0x2cu}};
    if (state == nullptr || stream == nullptr ||
        (stream->flags & kTrackPss) == 0u) {
        return true;
    }
    if ((stream->flags & kPublicationAttempted) != 0u) {
        return (stream->flags & kPublished) != 0u;
    }
    stream->flags |= kPublicationAttempted;
    const auto reject = [&]() {
        ++state->title_track_pss_validation_failure_count;
        return false;
    };
    if (stream->payload == 0u || stream->payload_size < kHeaderSize) {
        return reject();
    }
    const uint32_t image_anchor = b2r_native_service_read_u32(
        state, stream->payload + kImageAnchorPointerOffset);
    if (image_anchor < kImageAnchorOffset) { return reject(); }
    const uint32_t image_base = image_anchor - kImageAnchorOffset;
    const uint64_t image_end =
        static_cast<uint64_t>(image_base) + stream->payload_size;
    if (image_end > 0x100000000ull) { return reject(); }

    const uint32_t descriptor_count = b2r_native_service_read_u32(
        state, stream->payload + kDescriptorCountOffset);
    if (descriptor_count == 0u || descriptor_count > kMaxDescriptorCount) {
        return reject();
    }
    const uint64_t descriptor_table_size =
        static_cast<uint64_t>(descriptor_count) * kDescriptorSize;
    for (uint32_t table_index = 0u; table_index < 2u; ++table_index) {
        const uint32_t table_address = b2r_native_service_read_u32(
            state, stream->payload + kDescriptorPointerOffsets[table_index]);
        if (table_address < image_base) { return reject(); }
        const uint32_t table_offset = table_address - image_base;
        if (static_cast<uint64_t>(table_address) + descriptor_table_size >
                0x100000000ull ||
            static_cast<uint64_t>(table_offset) + descriptor_table_size >
                stream->payload_size) {
            return reject();
        }
        uint64_t previous_end = 0u;
        for (uint32_t index = 0u; index < descriptor_count; ++index) {
            const uint32_t descriptor = stream->payload + table_offset +
                index * kDescriptorSize;
            const uint32_t stream_offset =
                b2r_native_service_read_u32(state, descriptor);
            const uint32_t stream_length =
                b2r_native_service_read_u32(state, descriptor + 4u);
            const uint64_t stream_end =
                static_cast<uint64_t>(stream_offset) + stream_length;
            if (stream_length == 0u || stream_length > kMaxStreamChunkSize ||
                stream_end > 0x100000000ull ||
                (index != 0u && stream_offset < previous_end)) {
                return reject();
            }
            previous_end = stream_end;
        }
    }

    uint64_t scene_record_count = 0u;
    for (uint32_t table_index = 0u; table_index < 3u; ++table_index) {
        const uint32_t count = b2r_native_service_read_u32(
            state, stream->payload + kSceneTableOffsets[table_index][0]);
        const uint32_t address = b2r_native_service_read_u32(
            state, stream->payload + kSceneTableOffsets[table_index][1]);
        if (count > kMaxSceneRecordCount) { return reject(); }
        if (count == 0u) { continue; }
        if (address < image_base) { return reject(); }
        const uint32_t offset = address - image_base;
        const uint64_t table_size =
            static_cast<uint64_t>(count) * kSceneRecordSize;
        if (static_cast<uint64_t>(offset) + table_size >
            stream->payload_size) {
            return reject();
        }
        scene_record_count += count;
    }

    if (!b2r_native_allocate_pages(
            state, image_base, stream->payload_size)) {
        return reject();
    }
    b2r_native_service_copy_bytes(
        state, image_base, stream->payload, stream->payload_size);
    stream->image_base = image_base;
    stream->flags |= kPublished;
    ++state->title_track_pss_publication_count;
    state->title_track_pss_published_bytes += stream->payload_size;
    state->title_track_pss_descriptor_entry_count +=
        static_cast<uint64_t>(descriptor_count) * 2u;
    state->title_track_pss_scene_record_entry_count += scene_record_count;
    return true;
}

static void b2r_refresh_live_controller(B2RNativeHostServiceState* state) {
    if (state == nullptr || state->live_control_mapping == nullptr ||
        state->live_control_size < 84u) {
        return;
    }
    const uint8_t* mapping = state->live_control_mapping;
    uint32_t sequence = 0u;
    std::memcpy(&sequence, mapping + 64u, sizeof(sequence));
    if (sequence == 0u || sequence == state->live_controller_sequence ||
        (sequence & 1u) != 0u) {
        return;
    }
    B2RNativeControllerState controller{};
    std::memcpy(&controller.buttons, mapping + 68u, sizeof(controller.buttons));
    controller.left_trigger = mapping[70u];
    controller.right_trigger = mapping[71u];
    std::memcpy(&controller.thumb_lx, mapping + 72u, sizeof(controller.thumb_lx));
    std::memcpy(&controller.thumb_ly, mapping + 74u, sizeof(controller.thumb_ly));
    std::memcpy(&controller.thumb_rx, mapping + 76u, sizeof(controller.thumb_rx));
    std::memcpy(&controller.thumb_ry, mapping + 78u, sizeof(controller.thumb_ry));
    controller.connected = mapping[80u] != 0u ? 1u : 0u;
    uint32_t confirmed = 0u;
    std::memcpy(&confirmed, mapping + 64u, sizeof(confirmed));
    if (confirmed != sequence || (confirmed & 1u) != 0u) {
        return;
    }
    controller.generation = sequence;
    state->controllers[0] = controller;
    state->live_controller_sequence = sequence;
    ++state->live_controller_refresh_count;
}

static void b2r_native_audio_buffer_set_data(
    B2RNativeHostServiceState* state,
    uint32_t buffer,
    uint32_t data,
    uint32_t size);
static void b2r_native_audio_buffer_create(
    B2RNativeHostServiceState* state,
    uint32_t descriptor,
    uint32_t output);
static void b2r_native_audio_buffer_set_format(
    B2RNativeHostServiceState* state,
    uint32_t buffer,
    uint32_t format);
static void b2r_native_audio_buffer_set_volume(
    B2RNativeHostServiceState* state,
    uint32_t buffer,
    int32_t volume);
static void b2r_native_audio_buffer_set_frequency(
    B2RNativeHostServiceState* state,
    uint32_t buffer,
    uint32_t frequency);
static uint32_t b2r_native_audio_buffer_get_position(
    B2RNativeHostServiceState* state,
    uint32_t buffer,
    uint32_t play_cursor_output,
    uint32_t write_cursor_output);
static uint32_t b2r_native_audio_buffer_set_position(
    B2RNativeHostServiceState* state,
    uint32_t buffer,
    uint32_t position);
static uint32_t b2r_native_audio_buffer_play(
    B2RNativeHostServiceState* state,
    uint32_t buffer,
    uint32_t flags);
static void b2r_native_audio_buffer_stop(
    B2RNativeHostServiceState* state,
    uint32_t buffer);
static uint32_t b2r_native_audio_buffer_stop_ex(
    B2RNativeHostServiceState* state,
    uint32_t buffer);
static void b2r_native_audio_stream_create(
    B2RNativeHostServiceState* state,
    uint32_t descriptor,
    uint32_t output);
static void b2r_native_audio_stream_process(
    B2RNativeHostServiceState* state,
    uint32_t stream,
    uint32_t packet);
static void b2r_native_audio_stream_flush(
    B2RNativeHostServiceState* state,
    uint32_t stream);
static void b2r_native_audio_stream_set_format(
    B2RNativeHostServiceState* state,
    uint32_t stream,
    uint32_t format);

static bool b2r_try_native_host_service(
    B2RNativeHostServiceState* state,
    uint32_t target,
    void* context,
    uint32_t* next_target
) {
    if (state == nullptr || state->entries == nullptr) { return false; }
    if (state->native_service_bypass_target == target) {
        state->native_service_bypass_target = 0u;
        return false;
    }
    b2r_refresh_live_controller(state);
    B2RNativeHostServiceEntry* service = nullptr;
    for (uint32_t index = 0u; index < state->entry_count; ++index) {
        if (state->entries[index].target == target) {
            service = &state->entries[index];
            break;
        }
    }
    if (service == nullptr || service->kind == 0u || service->kind >= 16u) {
        return false;
    }
    uint8_t* bytes = static_cast<uint8_t*>(context);
    uint32_t& eax = *reinterpret_cast<uint32_t*>(bytes + state->eax_offset);
    uint32_t& esp = *reinterpret_cast<uint32_t*>(bytes + state->esp_offset);
    uint32_t& eip = *reinterpret_cast<uint32_t*>(bytes + state->eip_offset);
    const uint32_t return_address = b2r_native_service_read_u32(state, esp);
    bool forward_original = false;
    auto argument = [&](uint32_t index) {
        return b2r_native_service_read_u32(state, esp + 4u + index * 4u);
    };
    state->last_service_target = target;
    state->last_service_kind = service->kind;
    state->last_service_value = service->value;
    state->last_service_result = eax;
    state->last_service_return_address = return_address;
    state->last_service_argument0 = service->stack_cleanup_bytes >= 4u
        ? argument(0u) : 0u;
    state->last_service_worker_handle = state->current_worker_handle;
    constexpr uint32_t kDisconnected = 0x48fu;
    switch (service->kind) {
    case 1u:
        eax = service->value;
        break;
    case 2u:
        eax = static_cast<uint32_t>(state->performance_counter);
        break;
    case 3u: {
        const uint32_t output = argument(0u);
        if (output != 0u) {
            b2r_native_service_write_u32(
                state, output, static_cast<uint32_t>(state->system_time_filetime));
            b2r_native_service_write_u32(
                state, output + 4u,
                static_cast<uint32_t>(state->system_time_filetime >> 32u));
        }
        break;
    }
    case 4u: {
        uint32_t mask = 0u;
        for (uint32_t port = 0u; port < 4u; ++port) {
            if (state->controllers[port].connected != 0u) { mask |= 1u << port; }
        }
        eax = mask;
        break;
    }
    case 5u: {
        const uint32_t port = argument(1u);
        eax = port < 4u && state->controllers[port].connected != 0u
            ? 0xb2401000u + port : 0u;
        break;
    }
    case 6u: {
        const uint32_t handle = argument(0u);
        const uint32_t output = argument(1u);
        const uint32_t port = handle - 0xb2401000u;
        const bool connected =
            port < 4u && state->controllers[port].connected != 0u;
        if (connected && output != 0u) {
            b2r_native_service_write_u8(state, output, 1u);
            b2r_native_service_write_u8(state, output + 1u, 1u);
            for (uint32_t offset = 2u; offset < 20u; ++offset) {
                b2r_native_service_write_u8(state, output + offset, 0u);
            }
        }
        eax = connected ? 0u : kDisconnected;
        break;
    }
    case 7u: {
        const uint32_t handle = argument(0u);
        const uint32_t output = argument(1u);
        const uint32_t port = handle - 0xb2401000u;
        const bool connected =
            port < 4u && state->controllers[port].connected != 0u;
        if (connected && output != 0u) {
            const B2RNativeControllerState& controller = state->controllers[port];
            state->observed_button_mask |= controller.buttons;
            if ((controller.buttons & 0x1000u) != 0u) {
                ++state->a_pressed_poll_count;
            }
            ++state->successful_get_state_count;
            if (state->controller_last_generations[port] != controller.generation) {
                ++state->controller_packets[port];
                state->controller_last_generations[port] = controller.generation;
            }
            b2r_native_service_write_u32(state, output, state->controller_packets[port]);
            const uint16_t digital = controller.buttons & 0x00ffu;
            b2r_native_service_write_u8(state, output + 4u, digital & 0xffu);
            b2r_native_service_write_u8(state, output + 5u, digital >> 8u);
            const uint16_t analog_masks[6] = {
                0x1000u, 0x2000u, 0x4000u, 0x8000u, 0x0100u, 0x0200u};
            for (uint32_t index = 0u; index < 6u; ++index) {
                b2r_native_service_write_u8(
                    state, output + 6u + index,
                    (controller.buttons & analog_masks[index]) != 0u ? 0xffu : 0u);
            }
            b2r_native_service_write_u8(state, output + 12u, controller.left_trigger);
            b2r_native_service_write_u8(state, output + 13u, controller.right_trigger);
            const int16_t sticks[4] = {
                controller.thumb_lx, controller.thumb_ly,
                controller.thumb_rx, controller.thumb_ry};
            for (uint32_t index = 0u; index < 4u; ++index) {
                const uint16_t value = static_cast<uint16_t>(sticks[index]);
                b2r_native_service_write_u8(state, output + 14u + index * 2u, value & 0xffu);
                b2r_native_service_write_u8(state, output + 15u + index * 2u, value >> 8u);
            }
        }
        eax = connected ? 0u : kDisconnected;
        break;
    }
    case 8u: {
        constexpr uint32_t kCreateWorker = 1u;
        constexpr uint32_t kTerminateWorker = 2u;
        constexpr uint32_t kSuspendWorker = 3u;
        constexpr uint32_t kResumeWorker = 4u;
        constexpr uint32_t kCreateSemaphore = 5u;
        constexpr uint32_t kReferenceWorker = 6u;
        constexpr uint32_t kSetBasePriority = 7u;
        constexpr uint32_t kSetPriority = 8u;
        constexpr uint32_t kSetDisableBoost = 9u;
        if (service->value == kCreateWorker) {
            const uint32_t handle = b2r_allocate_native_object_handle(state);
            if (handle == 0u) { return false; }
            if (argument(0u) != 0u) {
                b2r_native_service_write_u32(state, argument(0u), handle);
            }
            if (argument(4u) != 0u) {
                b2r_native_service_write_u32(state, argument(4u), handle);
            }
            const uint32_t status = argument(7u) != 0u
                ? B2R_WORKER_SUSPENDED : B2R_WORKER_READY;
            if (argument(9u) != 0u) {
                b2r_upsert_worker(
                    state->worker_lifecycle,
                    handle,
                    argument(9u),
                    argument(5u),
                    argument(6u),
                    status
                );
            }
            eax = 0u;
        } else if (service->value == kTerminateWorker) {
            if (state->current_worker_handle != 0u) {
                B2RNativeWorkerLifecycleEntry* worker = b2r_find_worker(
                    state->worker_lifecycle, state->current_worker_handle);
                if (b2r_set_worker_status(
                        state->worker_lifecycle, worker, B2R_WORKER_COMPLETED)) {
                    *reinterpret_cast<bool*>(
                        bytes + state->yield_requested_offset) = true;
                }
            }
            eax = 0u;
        } else if (service->value == kSuspendWorker) {
            b2r_set_worker_status(
                state->worker_lifecycle,
                b2r_find_worker(state->worker_lifecycle, argument(0u)),
                B2R_WORKER_SUSPENDED
            );
            eax = 0u;
        } else if (service->value == kResumeWorker) {
            b2r_set_worker_status(
                state->worker_lifecycle,
                b2r_find_worker(state->worker_lifecycle, argument(0u)),
                B2R_WORKER_READY
            );
            eax = 0u;
        } else if (service->value == kCreateSemaphore) {
            const uint32_t handle = b2r_allocate_native_object_handle(state);
            if (handle == 0u) { return false; }
            B2RNativeSemaphoreEntry* semaphore = b2r_register_native_semaphore(
                state, handle, argument(2u), argument(3u));
            if (semaphore == nullptr) {
                if (argument(0u) != 0u) {
                    b2r_native_service_write_u32(state, argument(0u), 0u);
                }
                eax = 0xc000009au;
            } else {
                if (argument(0u) != 0u) {
                    b2r_native_service_write_u32(state, argument(0u), handle);
                }
                eax = 0u;
            }
        } else if (service->value == kReferenceWorker) {
            eax = b2r_find_worker(state->worker_lifecycle, argument(0u)) != nullptr
                ? 0u : 0xc0000008u;
        } else if (service->value == kSetBasePriority ||
                   service->value == kSetPriority ||
                   service->value == kSetDisableBoost) {
            B2RNativeWorkerLifecycleEntry* worker = b2r_find_worker(
                state->worker_lifecycle, argument(0u));
            if (worker == nullptr) {
                eax = 0u;
            } else if (service->value == kSetBasePriority) {
                eax = static_cast<uint32_t>(worker->base_priority);
                worker->base_priority = static_cast<int32_t>(argument(1u));
            } else if (service->value == kSetPriority) {
                eax = static_cast<uint32_t>(worker->priority);
                worker->priority = static_cast<int32_t>(argument(1u));
            } else {
                eax = worker->disable_boost;
                worker->disable_boost = argument(1u) != 0u ? 1u : 0u;
            }
        } else {
            if (state->cold_call == nullptr) { return false; }
            state->cold_call(state->cold_user, target, context);
        }
        break;
    }
    case 9u: {
        constexpr uint32_t kRaise = 1u;
        constexpr uint32_t kLower = 2u;
        constexpr uint32_t kRaiseDpc = 3u;
        constexpr uint32_t kRaiseSynch = 4u;
        constexpr uint32_t kGetCurrent = 5u;
        const uint32_t previous = state->current_irql;
        if (service->value == kRaise) {
            const uint32_t requested = argument(0u);
            if (requested > state->current_irql) {
                state->current_irql = requested;
            }
            eax = previous;
        } else if (service->value == kLower) {
            state->current_irql = service->stack_cleanup_bytes != 0u
                ? argument(0u) : 0u;
            eax = 0u;
        } else if (service->value == kRaiseDpc) {
            if (state->current_irql < 2u) { state->current_irql = 2u; }
            eax = previous;
        } else if (service->value == kRaiseSynch) {
            if (state->current_irql < 3u) { state->current_irql = 3u; }
            eax = previous;
        } else if (service->value == kGetCurrent) {
            eax = state->current_irql;
        } else {
            return false;
        }
        break;
    }
    case 10u: {
        constexpr uint32_t kReleaseSemaphore = 1u;
        constexpr uint32_t kWaitSemaphore = 2u;
        constexpr uint32_t kWaitKernelObject = 3u;
        if (service->value == kWaitKernelObject) {
            // KeWaitForSingleObject receives an in-place dispatcher object,
            // not a runtime handle. Hardware completion is synchronous in the
            // native guest runner, so this boundary is already satisfied.
            eax = 0u;
            break;
        }
        if (state->live_control_mapping != nullptr &&
            state->live_control_size >= 204u) {
            const uint32_t lifecycle_count = state->worker_lifecycle != nullptr
                ? state->worker_lifecycle->entry_count : 0u;
            const uint32_t handle = argument(0u);
            std::memcpy(state->live_control_mapping + 188u,
                        &state->semaphore_count, sizeof(state->semaphore_count));
            std::memcpy(state->live_control_mapping + 192u,
                        &lifecycle_count, sizeof(lifecycle_count));
            std::memcpy(state->live_control_mapping + 196u,
                        &handle, sizeof(handle));
            std::memcpy(state->live_control_mapping + 200u,
                        &state->current_worker_handle,
                        sizeof(state->current_worker_handle));
            MemoryBarrier();
        }
        auto publish_wait_phase = [&](uint32_t phase) {
            if (state->live_control_mapping == nullptr ||
                state->live_control_size < 188u) {
                return;
            }
            std::memcpy(state->live_control_mapping + 184u,
                        &phase, sizeof(phase));
            MemoryBarrier();
        };
        publish_wait_phase(10u);
        B2RNativeSemaphoreEntry* semaphore = b2r_find_native_semaphore(
            state, argument(0u));
        publish_wait_phase(11u);
        if (semaphore == nullptr) {
            // An invalid or stale object handle is an NT service result, not
            // an unknown native target. Keep the call inside the compiled ABI
            // dispatcher and let the title's normal NTSTATUS path handle it.
            eax = 0xc0000008u;
            break;
        }
        if (service->value == kReleaseSemaphore) {
            const uint32_t release_count = argument(1u);
            const uint32_t previous = semaphore->count;
            const uint64_t released =
                static_cast<uint64_t>(semaphore->count) + release_count;
            semaphore->count = static_cast<uint32_t>(
                released < semaphore->limit ? released : semaphore->limit);
            const uint32_t previous_address = argument(2u);
            if (previous_address != 0u) {
                b2r_native_service_write_u32(state, previous_address, previous);
            }
            if (state->worker_lifecycle != nullptr) {
                for (uint32_t index = 0u;
                     index < state->worker_lifecycle->entry_count &&
                     semaphore->count != 0u;
                     ++index) {
                    B2RNativeWorkerLifecycleEntry* worker =
                        &state->worker_lifecycle->entries[index];
                    if (worker->status != B2R_WORKER_WAITING ||
                        worker->wait_handle != semaphore->handle) {
                        continue;
                    }
                    --semaphore->count;
                    b2r_set_worker_status(
                        state->worker_lifecycle, worker, B2R_WORKER_READY);
                }
            }
            eax = 0u;
        } else if (service->value == kWaitSemaphore) {
            publish_wait_phase(12u);
            if (semaphore->count != 0u) {
                --semaphore->count;
                eax = 0u;
            } else {
                publish_wait_phase(13u);
                eax = 0x00000102u;
                *reinterpret_cast<bool*>(
                    bytes + state->yield_requested_offset) = true;
                const bool primary_worker =
                    state->worker_lifecycle != nullptr &&
                    state->worker_lifecycle->entry_count != 0u &&
                    state->worker_lifecycle->entries[0].handle ==
                        state->current_worker_handle;
                if (!primary_worker) {
                    B2RNativeWorkerLifecycleEntry* worker = b2r_find_worker(
                        state->worker_lifecycle, state->current_worker_handle);
                    publish_wait_phase(14u);
                    if (worker == nullptr) { return false; }
                    worker->wait_handle = semaphore->handle;
                    b2r_set_worker_status(
                        state->worker_lifecycle, worker, B2R_WORKER_WAITING);
                    worker->wait_handle = semaphore->handle;
                }
                publish_wait_phase(16u);
            }
        } else {
            return false;
        }
        break;
    }
    case 11u: {
        constexpr uint32_t kInitAnsiString = 1u;
        constexpr uint32_t kEqualString = 2u;
        constexpr uint32_t kMissingNonVolatileSetting = 3u;
        constexpr uint32_t kNtStatusToDosError = 4u;
        constexpr uint32_t kPreserveReturn = 5u;
        constexpr uint32_t kDelayThread = 6u;
        constexpr uint32_t kAnsiStringToUnicodeString = 7u;
        constexpr uint32_t kUnicodeStringToAnsiString = 8u;
        if (service->value == kInitAnsiString) {
            const uint32_t destination = argument(0u);
            const uint32_t source = argument(1u);
            uint32_t length = 0u;
            if (source != 0u) {
                while (length < 0xfffeu &&
                       b2r_native_service_read_u8(state, source + length) != 0u) {
                    ++length;
                }
            }
            const uint32_t maximum = source != 0u ? length + 1u : 0u;
            if (destination != 0u) {
                b2r_native_service_write_u32(
                    state, destination,
                    (length & 0xffffu) | ((maximum & 0xffffu) << 16u));
                b2r_native_service_write_u32(state, destination + 4u, source);
            }
        } else if (service->value == kEqualString) {
            const uint32_t left = argument(0u);
            const uint32_t right = argument(1u);
            const bool case_insensitive = argument(2u) != 0u;
            const uint32_t left_descriptor =
                b2r_native_service_read_u32(state, left);
            const uint32_t right_descriptor =
                b2r_native_service_read_u32(state, right);
            const uint32_t left_length = left_descriptor & 0xffffu;
            const uint32_t right_length = right_descriptor & 0xffffu;
            bool equal = left_length == right_length;
            const uint32_t left_buffer =
                b2r_native_service_read_u32(state, left + 4u);
            const uint32_t right_buffer =
                b2r_native_service_read_u32(state, right + 4u);
            for (uint32_t index = 0u; equal && index < left_length; ++index) {
                uint8_t left_value = b2r_native_service_read_u8(
                    state, left_buffer + index);
                uint8_t right_value = b2r_native_service_read_u8(
                    state, right_buffer + index);
                if (case_insensitive) {
                    if (left_value >= 'A' && left_value <= 'Z') {
                        left_value = static_cast<uint8_t>(left_value + ('a' - 'A'));
                    }
                    if (right_value >= 'A' && right_value <= 'Z') {
                        right_value = static_cast<uint8_t>(right_value + ('a' - 'A'));
                    }
                }
                equal = left_value == right_value;
            }
            eax = equal ? 1u : 0u;
        } else if (service->value == kMissingNonVolatileSetting) {
            const uint32_t result_length = argument(4u);
            if (result_length != 0u) {
                b2r_native_service_write_u32(state, result_length, 0u);
            }
            eax = 0xc0000034u;
        } else if (service->value == kNtStatusToDosError) {
            const uint32_t status = argument(0u);
            switch (status) {
            case 0x00000000u: eax = 0u; break;
            case 0xc000000fu: eax = 2u; break;
            case 0xc0000034u: eax = 2u; break;
            case 0xc0000035u: eax = 183u; break;
            case 0xc0000022u: eax = 5u; break;
            case 0xc0000008u: eax = 6u; break;
            case 0xc000000du: eax = 87u; break;
            case 0xc0000002u: eax = 120u; break;
            default: eax = 1u; break;
            }
        } else if (service->value == kDelayThread) {
            B2RNativeWorkerLifecycleEntry* worker = b2r_find_worker(
                state->worker_lifecycle, state->current_worker_handle);
            if (worker != nullptr) {
                b2r_set_worker_status(
                    state->worker_lifecycle, worker, B2R_WORKER_WAITING);
                worker->wait_handle = 0u;
            }
            eax = 0u;
            *reinterpret_cast<bool*>(
                bytes + state->yield_requested_offset) = true;
        } else if (service->value == kAnsiStringToUnicodeString) {
            constexpr uint32_t kStatusBufferOverflow = 0x80000005u;
            constexpr uint32_t kStatusNoMemory = 0xc0000017u;
            constexpr uint32_t kStatusInvalidParameter = 0xc000000du;
            constexpr uint32_t kStatusInvalidParameter2 = 0xc00000f0u;
            constexpr uint32_t kXboxAnsiCodePage = 1252u;
            const uint32_t destination_address = argument(0u);
            const uint32_t source_address = argument(1u);
            const bool allocate_destination = argument(2u) != 0u;
            if (destination_address == 0u || source_address == 0u) {
                eax = kStatusInvalidParameter;
                break;
            }
            const uint32_t source_descriptor =
                b2r_native_service_read_u32(state, source_address);
            const uint32_t source_length = source_descriptor & 0xffffu;
            const uint32_t source_buffer =
                b2r_native_service_read_u32(state, source_address + 4u);
            if (source_length != 0u && source_buffer == 0u) {
                eax = kStatusInvalidParameter;
                break;
            }
            std::vector<char> source_bytes(source_length);
            for (uint32_t index = 0u; index < source_length; ++index) {
                source_bytes[index] = static_cast<char>(
                    b2r_native_service_read_u8(
                        state, source_buffer + index));
            }
            std::vector<wchar_t> converted(source_length);
            const int converted_count = source_length == 0u ? 0 :
                MultiByteToWideChar(
                    kXboxAnsiCodePage,
                    0u,
                    source_bytes.data(),
                    static_cast<int>(source_length),
                    converted.data(),
                    static_cast<int>(converted.size()));
            if (source_length != 0u && converted_count <= 0) {
                eax = kStatusInvalidParameter;
                break;
            }
            const uint32_t output_length =
                static_cast<uint32_t>(converted_count) * 2u;
            const uint32_t required_capacity = output_length + 2u;
            if (required_capacity > 0xffffu) {
                eax = kStatusInvalidParameter2;
                break;
            }
            uint32_t destination_descriptor =
                b2r_native_service_read_u32(state, destination_address);
            uint32_t destination_maximum = destination_descriptor >> 16u;
            uint32_t destination_buffer =
                b2r_native_service_read_u32(
                    state, destination_address + 4u);
            b2r_native_service_write_u32(
                state,
                destination_address,
                (destination_descriptor & 0xffff0000u) | output_length);
            if (allocate_destination) {
                destination_buffer = b2r_native_allocate_range(
                    state, required_capacity, 2u, false);
                destination_maximum = required_capacity;
                b2r_native_service_write_u32(
                    state,
                    destination_address,
                    output_length | (destination_maximum << 16u));
                b2r_native_service_write_u32(
                    state, destination_address + 4u, destination_buffer);
                if (destination_buffer == 0u) {
                    eax = kStatusNoMemory;
                    break;
                }
            } else if (
                destination_buffer == 0u ||
                output_length >= destination_maximum
            ) {
                eax = destination_buffer != 0u
                    ? kStatusBufferOverflow : kStatusInvalidParameter;
                break;
            }
            for (uint32_t index = 0u;
                 index < static_cast<uint32_t>(converted_count);
                 ++index) {
                b2r_native_service_write_u16(
                    state,
                    destination_buffer + index * 2u,
                    static_cast<uint16_t>(converted[index]));
            }
            b2r_native_service_write_u16(
                state, destination_buffer + output_length, 0u);
            eax = 0u;
        } else if (service->value == kUnicodeStringToAnsiString) {
            constexpr uint32_t kStatusBufferOverflow = 0x80000005u;
            constexpr uint32_t kStatusNoMemory = 0xc0000017u;
            constexpr uint32_t kStatusInvalidParameter = 0xc000000du;
            constexpr uint32_t kXboxAnsiCodePage = 1252u;
            const uint32_t destination_address = argument(0u);
            const uint32_t source_address = argument(1u);
            const bool allocate_destination = argument(2u) != 0u;
            if (destination_address == 0u || source_address == 0u) {
                eax = kStatusInvalidParameter;
                break;
            }
            const uint32_t source_descriptor =
                b2r_native_service_read_u32(state, source_address);
            const uint32_t source_length = source_descriptor & 0xffffu;
            const uint32_t source_buffer =
                b2r_native_service_read_u32(state, source_address + 4u);
            if ((source_length & 1u) != 0u ||
                (source_length != 0u && source_buffer == 0u)) {
                eax = kStatusInvalidParameter;
                break;
            }
            const uint32_t source_character_count = source_length / 2u;
            std::vector<wchar_t> source_characters(source_character_count);
            for (uint32_t index = 0u;
                 index < source_character_count;
                 ++index) {
                source_characters[index] = static_cast<wchar_t>(
                    b2r_native_service_read_u16(
                        state, source_buffer + index * 2u));
            }
            std::vector<char> converted(source_character_count * 2u + 1u);
            const char replacement = '?';
            const int converted_count = source_character_count == 0u ? 0 :
                WideCharToMultiByte(
                    kXboxAnsiCodePage,
                    0u,
                    source_characters.data(),
                    static_cast<int>(source_character_count),
                    converted.data(),
                    static_cast<int>(converted.size()),
                    &replacement,
                    nullptr);
            if (source_character_count != 0u && converted_count <= 0) {
                eax = kStatusInvalidParameter;
                break;
            }
            const uint32_t required_length =
                static_cast<uint32_t>(converted_count);
            const uint32_t required_capacity = required_length + 1u;
            uint32_t destination_descriptor =
                b2r_native_service_read_u32(state, destination_address);
            uint32_t destination_maximum = destination_descriptor >> 16u;
            uint32_t destination_buffer =
                b2r_native_service_read_u32(
                    state, destination_address + 4u);
            uint32_t output_length = required_length;
            uint32_t status = 0u;
            if (allocate_destination) {
                destination_buffer = b2r_native_allocate_range(
                    state, required_capacity, 1u, false);
                destination_maximum = required_capacity;
                b2r_native_service_write_u32(
                    state, destination_address + 4u, destination_buffer);
                if (destination_buffer == 0u) {
                    b2r_native_service_write_u32(
                        state,
                        destination_address,
                        required_length | (destination_maximum << 16u));
                    eax = kStatusNoMemory;
                    break;
                }
            } else if (destination_buffer == 0u) {
                eax = kStatusInvalidParameter;
                break;
            } else if (output_length >= destination_maximum) {
                if (destination_maximum == 0u) {
                    b2r_native_service_write_u32(
                        state,
                        destination_address,
                        destination_descriptor & 0xffff0000u);
                    eax = kStatusBufferOverflow;
                    break;
                }
                output_length = destination_maximum - 1u;
                status = kStatusBufferOverflow;
            }
            b2r_native_service_write_u32(
                state,
                destination_address,
                output_length | (destination_maximum << 16u));
            for (uint32_t index = 0u; index < output_length; ++index) {
                b2r_native_service_write_u8(
                    state,
                    destination_buffer + index,
                    static_cast<uint8_t>(converted[index]));
            }
            b2r_native_service_write_u8(
                state, destination_buffer + output_length, 0u);
            eax = status;
        } else if (service->value != kPreserveReturn) {
            return false;
        }
        break;
    }
    case 12u: {
        constexpr uint32_t kAllocateContiguousEx = 1u;
        constexpr uint32_t kAllocateVirtual = 2u;
        constexpr uint32_t kAllocateContiguous = 3u;
        constexpr uint32_t kGetPhysicalAddress = 4u;
        constexpr uint32_t kQueryAllocationSize = 5u;
        constexpr uint32_t kFreeAllocation = 6u;
        constexpr uint32_t kValidateAllocationRange = 7u;
        constexpr uint32_t kAllocatePool = 8u;
        if (service->value == kAllocateContiguousEx) {
            eax = b2r_native_allocate_contiguous_range(
                state,
                argument(0u),
                argument(1u),
                argument(2u),
                argument(3u));
        } else if (service->value == kAllocateVirtual) {
            const uint32_t base_address_address = argument(0u);
            const uint32_t region_size_address = argument(2u);
            const uint32_t allocation_type = argument(3u);
            const uint32_t requested_base = base_address_address != 0u
                ? b2r_native_service_read_u32(state, base_address_address) : 0u;
            const uint32_t requested_size = region_size_address != 0u
                ? b2r_native_service_read_u32(state, region_size_address) : 0u;
            if (requested_size == 0u) {
                eax = 0xc000000du;
                break;
            }
            uint32_t address = 0u;
            if (requested_base != 0u && (allocation_type & 0x1000u) != 0u &&
                b2r_find_native_allocation(state, requested_base) != nullptr) {
                address = requested_base;
            } else {
                address = b2r_native_allocate_range(
                    state, requested_size, 0x1000u, false);
            }
            if (address == 0u) {
                eax = 0xc000000du;
                break;
            }
            if (base_address_address != 0u) {
                b2r_native_service_write_u32(
                    state, base_address_address, address);
            }
            if (region_size_address != 0u) {
                b2r_native_service_write_u32(
                    state, region_size_address, requested_size);
            }
            eax = 0u;
        } else if (service->value == kAllocateContiguous) {
            eax = b2r_native_allocate_range(
                state, argument(0u), 0x1000u, true);
        } else if (service->value == kGetPhysicalAddress) {
            const uint32_t address = argument(0u);
            if (b2r_find_native_allocation(state, address) == nullptr) {
                return false;
            }
            eax = address >= 0x80000000u && address < 0x84000000u
                ? address & 0x7fffffffu : address;
        } else if (service->value == kQueryAllocationSize) {
            B2RNativeAllocationEntry* allocation =
                b2r_find_native_allocation(state, argument(0u));
            if (allocation == nullptr) { return false; }
            eax = allocation->size;
        } else if (service->value == kFreeAllocation) {
            B2RNativeAllocationEntry* allocation =
                b2r_find_native_allocation(state, argument(0u));
            if (allocation == nullptr) {
                eax = 0xc000000du;
            } else {
                allocation->active = 0u;
                eax = 0u;
            }
        } else if (service->value == kValidateAllocationRange) {
            B2RNativeAllocationEntry* allocation =
                b2r_find_native_allocation(state, argument(0u));
            const uint32_t size = argument(1u);
            if (allocation == nullptr) {
                return false;
            }
            const uint32_t allocation_page_start = allocation->address & ~0xfffu;
            const uint64_t allocation_end =
                static_cast<uint64_t>(allocation->address) + allocation->size;
            const uint64_t allocation_page_end =
                (allocation_end + 0xfffull) & ~0xfffull;
            const uint32_t range_page_start = argument(0u) & ~0xfffu;
            const uint64_t range_end =
                static_cast<uint64_t>(argument(0u)) + (size != 0u ? size : 1u);
            const uint64_t range_page_end = (range_end + 0xfffull) & ~0xfffull;
            if (range_page_start < allocation_page_start ||
                range_page_end > allocation_page_end) {
                return false;
            }
            eax = 0u;
        } else if (service->value == kAllocatePool) {
            eax = b2r_native_allocate_range(
                state, argument(0u), 0x10u, false);
        } else {
            return false;
        }
        break;
    }
    case 13u: {
        constexpr uint32_t kDirectSoundEffectImage = 1u;
        constexpr uint32_t kBufferSetData = 2u;
        constexpr uint32_t kBufferSetFormat = 3u;
        constexpr uint32_t kBufferPlay = 4u;
        constexpr uint32_t kBufferStop = 5u;
        constexpr uint32_t kBufferStopEx = 6u;
        constexpr uint32_t kStreamCreate = 7u;
        constexpr uint32_t kStreamProcess = 8u;
        constexpr uint32_t kStreamFlush = 9u;
        constexpr uint32_t kStreamSetFormat = 10u;
        constexpr uint32_t kBufferCreate = 11u;
        constexpr uint32_t kPassthrough = 12u;
        constexpr uint32_t kBufferSetVolume = 13u;
        constexpr uint32_t kBufferSetFrequency = 14u;
        constexpr uint32_t kBufferGetPosition = 15u;
        constexpr uint32_t kBufferSetPosition = 16u;
        if (service->value == kDirectSoundEffectImage) {
            constexpr uint32_t kWorkspaceAddress = 0x31ff0000u;
            const uint32_t image_size = argument(1u);
            const uint32_t workspace_output_address = argument(2u);
            if (workspace_output_address != 0u) {
                b2r_native_service_write_u32(
                    state, workspace_output_address, kWorkspaceAddress);
            }
            b2r_native_service_write_u32(state, kWorkspaceAddress, image_size);
            eax = 0u;
        } else if (service->value == kBufferSetData) {
            b2r_native_audio_buffer_set_data(
                state, argument(0u), argument(1u), argument(2u));
            forward_original = true;
        } else if (service->value == kBufferSetFormat) {
            b2r_native_audio_buffer_set_format(
                state, argument(0u), argument(1u));
            forward_original = true;
        } else if (service->value == kBufferPlay) {
            eax = b2r_native_audio_buffer_play(
                state, argument(0u), argument(3u));
        } else if (service->value == kBufferStop) {
            b2r_native_audio_buffer_stop(state, argument(0u));
            forward_original = true;
        } else if (service->value == kBufferStopEx) {
            eax = b2r_native_audio_buffer_stop_ex(state, argument(0u));
        } else if (service->value == kStreamCreate) {
            b2r_native_audio_stream_create(
                state, argument(1u), argument(2u));
            forward_original = true;
        } else if (service->value == kStreamProcess) {
            b2r_native_audio_stream_process(
                state, argument(0u), argument(1u));
            forward_original = true;
        } else if (service->value == kStreamFlush) {
            b2r_native_audio_stream_flush(state, argument(0u));
            forward_original = true;
        } else if (service->value == kStreamSetFormat) {
            b2r_native_audio_stream_set_format(
                state, argument(0u), argument(1u));
            forward_original = true;
        } else if (service->value == kBufferCreate) {
            b2r_native_audio_buffer_create(
                state, argument(1u), argument(2u));
            forward_original = true;
        } else if (service->value == kPassthrough) {
            forward_original = true;
        } else if (service->value == kBufferSetVolume) {
            b2r_native_audio_buffer_set_volume(
                state, argument(0u), static_cast<int32_t>(argument(1u)));
            eax = 0u;
        } else if (service->value == kBufferSetFrequency) {
            b2r_native_audio_buffer_set_frequency(
                state, argument(0u), argument(1u));
            eax = 0u;
        } else if (service->value == kBufferGetPosition) {
            eax = b2r_native_audio_buffer_get_position(
                state, argument(0u), argument(1u), argument(2u));
        } else if (service->value == kBufferSetPosition) {
            eax = b2r_native_audio_buffer_set_position(
                state, argument(0u), argument(1u));
        } else {
            return false;
        }
        break;
    }
    case 14u: {
        constexpr uint32_t kHeapAllocate = 1u;
        constexpr uint32_t kHeapFree = 2u;
        constexpr uint32_t kAllocationListCount = 3u;
        constexpr uint32_t kGlobalListRegister = 4u;
        constexpr uint32_t kTextDraw = 5u;
        constexpr uint32_t kAssetActivate = 6u;
        constexpr uint32_t kAssetStatus = 7u;
        constexpr uint32_t kAssetRead = 8u;
        constexpr uint32_t kAssetSeek = 9u;
        constexpr uint32_t kStaticDriveSetup = 10u;
        constexpr uint32_t kD3dFlush = 11u;
        constexpr uint32_t kSpinDelay = 12u;
        constexpr uint32_t kAssetOpen = 13u;
        constexpr uint32_t kFrontendSpecialAudioCreate = 14u;
        constexpr uint32_t kMusicModeSet = 15u;
        if (service->value == kHeapAllocate) {
            const uint32_t descriptor = argument(0u);
            const uint32_t raw_size = descriptor != 0u
                ? b2r_native_service_read_u32(state, descriptor) : 0u;
            const uint32_t requested_size =
                raw_size != 0u && raw_size <= 0x01000000u ? raw_size : 0u;
            const uint32_t normalized_size = requested_size != 0u
                ? requested_size : 0x60u;
            const uint32_t allocated_size = b2r_align_up(
                normalized_size > 0x10u ? normalized_size : 0x10u, 0x10u);
            const uint32_t address = b2r_align_up(
                state->title_heap_next_address, 0x10u);
            const uint64_t end = static_cast<uint64_t>(address) + allocated_size;
            if (end > 0x80000000ull ||
                !b2r_native_allocate_pages(state, address, allocated_size)) {
                eax = 0u;
                break;
            }
            state->title_heap_next_address = b2r_align_up(
                static_cast<uint32_t>(end), 0x10u);
            ++state->title_heap_allocation_count;
            state->title_heap_bytes_allocated += allocated_size;
            eax = address;
        } else if (service->value == kHeapFree) {
            eax = argument(1u);
            ++state->title_heap_free_count;
        } else if (service->value == kAllocationListCount) {
            constexpr uint32_t kSentinel = 0x005a6ebcu;
            constexpr uint32_t kOwnerBackOffset = 4u;
            constexpr uint32_t kMaxNodes = 4096u;
            const uint32_t owner = argument(0u);
            uint32_t node = b2r_native_service_read_u32(state, kSentinel);
            uint32_t visited[kMaxNodes] = {};
            uint32_t visited_count = 0u;
            uint32_t match_count = 0u;
            while (node != 0u && node != kSentinel &&
                   visited_count < kMaxNodes) {
                bool repeated = false;
                for (uint32_t index = 0u; index < visited_count; ++index) {
                    if (visited[index] == node) {
                        repeated = true;
                        break;
                    }
                }
                if (repeated) { break; }
                visited[visited_count++] = node;
                if (b2r_native_service_read_u32(
                        state, node - kOwnerBackOffset) == owner) {
                    ++match_count;
                }
                node = b2r_native_service_read_u32(state, node);
            }
            eax = match_count;
        } else if (service->value == kGlobalListRegister) {
            constexpr uint32_t kHead = 0x005add5cu;
            constexpr uint32_t kTail = kHead + 4u;
            const uint32_t object = argument(0u);
            const uint32_t owner = object != 0u
                ? b2r_native_service_read_u32(state, object + 0xa0u) : 0u;
            if (b2r_native_service_read_u32(state, kHead) == 0u) {
                b2r_native_service_write_u32(state, kHead, kHead);
                b2r_native_service_write_u32(state, kTail, kHead);
            }
            if (owner != 0u) {
                const uint8_t flags = b2r_native_service_read_u8(
                    state, owner + 3u);
                if ((flags & 0x03u) == 0u) {
                    const uint32_t node = owner + 8u;
                    const uint32_t previous_head =
                        b2r_native_service_read_u32(state, kHead);
                    b2r_native_service_write_u32(state, node, previous_head);
                    b2r_native_service_write_u32(state, node + 4u, kHead);
                    b2r_native_service_write_u32(
                        state, previous_head + 4u, node);
                    b2r_native_service_write_u32(state, kHead, node);
                    b2r_native_service_write_u8(
                        state, owner + 3u,
                        static_cast<uint8_t>(flags | 0x03u));
                }
            }
            if (object != 0u) {
                const uint8_t object_flags = b2r_native_service_read_u8(
                    state, object + 3u);
                b2r_native_service_write_u8(
                    state, object + 3u,
                    static_cast<uint8_t>(object_flags | 0x0cu));
            }
            eax = object;
        } else if (service->value == kAssetOpen) {
            constexpr uint32_t kObjectBase = 0x31f10000u;
            constexpr uint32_t kObjectStride = 0x1000u;
            constexpr uint32_t kVtable = 0x31f10100u;
            constexpr uint32_t kActivateTarget = 0x31f10200u;
            constexpr uint32_t kStatusTarget = 0x31f10300u;
            constexpr uint32_t kReadTarget = 0x31f10700u;
            constexpr uint32_t kSeekTarget = 0x31f10800u;
            if (state->title_asset_stream_count >=
                B2R_NATIVE_TITLE_ASSET_STREAM_CAPACITY) {
                ++state->title_asset_open_failure_count;
                eax = 0u;
                break;
            }
            const uint32_t guest_path_address = argument(0u);
            char guest_path[512] = {};
            uint32_t guest_length = 0u;
            while (guest_length + 1u < sizeof(guest_path)) {
                const uint8_t value = b2r_native_service_read_u8(
                    state, guest_path_address + guest_length);
                if (value == 0u) { break; }
                guest_path[guest_length++] = static_cast<char>(value);
            }
            guest_path[guest_length] = '\0';
            uint32_t relative_start = 0u;
            if (guest_length >= 3u &&
                ((guest_path[0] >= 'A' && guest_path[0] <= 'Z') ||
                 (guest_path[0] >= 'a' && guest_path[0] <= 'z')) &&
                guest_path[1] == ':' &&
                (guest_path[2] == '\\' || guest_path[2] == '/')) {
                relative_start = 3u;
            }
            while (relative_start < guest_length &&
                   (guest_path[relative_start] == '\\' ||
                    guest_path[relative_start] == '/')) {
                ++relative_start;
            }
            bool valid_path = state->extracted_root[0] != '\0' &&
                relative_start < guest_length;
            for (uint32_t index = relative_start;
                 valid_path && index < guest_length; ++index) {
                if (guest_path[index] == ':') { valid_path = false; }
                const bool segment_start = index == relative_start ||
                    guest_path[index - 1u] == '\\' ||
                    guest_path[index - 1u] == '/';
                if (segment_start && guest_path[index] == '.' &&
                    index + 1u < guest_length && guest_path[index + 1u] == '.' &&
                    (index + 2u == guest_length ||
                     guest_path[index + 2u] == '\\' ||
                     guest_path[index + 2u] == '/')) {
                    valid_path = false;
                }
            }
            char host_path[1536] = {};
            uint32_t host_length = 0u;
            while (valid_path && state->extracted_root[host_length] != '\0' &&
                   host_length + 2u < sizeof(host_path)) {
                host_path[host_length] = state->extracted_root[host_length];
                ++host_length;
            }
            if (valid_path && host_length != 0u &&
                host_path[host_length - 1u] != '\\' &&
                host_path[host_length - 1u] != '/') {
                host_path[host_length++] = '\\';
            }
            for (uint32_t index = relative_start;
                 valid_path && index < guest_length; ++index) {
                if (host_length + 1u >= sizeof(host_path)) {
                    valid_path = false;
                    break;
                }
                const char value = guest_path[index];
                host_path[host_length++] = value == '/' ? '\\' : value;
            }
            host_path[host_length] = '\0';
            std::FILE* file = valid_path ? std::fopen(host_path, "rb") : nullptr;
            uint32_t payload_size = 0u;
            bool payload_size_valid = false;
            if (file != nullptr && std::fseek(file, 0, SEEK_END) == 0) {
                const long size = std::ftell(file);
                if (size >= 0 && static_cast<uint64_t>(size) <= 0x20000000ull &&
                    std::fseek(file, 0, SEEK_SET) == 0) {
                    payload_size = static_cast<uint32_t>(size);
                    payload_size_valid = true;
                }
            }
            if (!payload_size_valid) {
                if (file != nullptr) { std::fclose(file); }
                ++state->title_asset_open_failure_count;
                eax = 0u;
                break;
            }
            if (state->title_asset_payload_next < 0x32000000u) {
                state->title_asset_payload_next = 0x32000000u;
            }
            const uint32_t payload_address = b2r_align_up(
                state->title_asset_payload_next, 0x1000u);
            const uint64_t payload_end =
                static_cast<uint64_t>(payload_address) + payload_size;
            const bool allocated = payload_end <= 0x80000000ull &&
                b2r_native_allocate_pages(
                    state, payload_address, payload_size != 0u ? payload_size : 1u);
            uint32_t copied = 0u;
            while (allocated && copied < payload_size) {
                const uint32_t address = payload_address + copied;
                const uint32_t page =
                    b2r_native_service_cache_address(state, address) >> 12u;
                const uint32_t page_offset = address & 0xfffu;
                uint32_t chunk = 0x1000u - page_offset;
                if (chunk > payload_size - copied) {
                    chunk = payload_size - copied;
                }
                if (state->read_pages == nullptr ||
                    state->read_pages[page] == nullptr ||
                    std::fread(
                        state->read_pages[page] + page_offset,
                        1u, chunk, file) != chunk) {
                    break;
                }
                b2r_native_service_mark_dirty(
                    state, page, static_cast<uint16_t>(page_offset),
                    static_cast<uint16_t>(chunk));
                copied += chunk;
            }
            std::fclose(file);
            if (!allocated || copied != payload_size) {
                ++state->title_asset_open_failure_count;
                eax = 0u;
                break;
            }
            const uint32_t object = kObjectBase +
                state->title_asset_stream_count * kObjectStride;
            B2RNativeTitleAssetStreamEntry* stream =
                &state->title_asset_streams[state->title_asset_stream_count];
            ++state->title_asset_stream_count;
            stream->object = object;
            stream->payload = payload_address;
            stream->payload_size = payload_size;
            stream->flags =
                (b2r_path_is_track_pss(guest_path, guest_length) ? 1u : 0u) |
                (b2r_path_is_traffic_tra(guest_path, guest_length) ? 8u : 0u);
            stream->image_base = 0u;
            if ((stream->flags & 1u) != 0u) {
                ++state->title_track_pss_candidate_count;
            }
            if ((stream->flags & 8u) != 0u) {
                ++state->title_traffic_tra_candidate_count;
            }
            state->title_asset_payload_next = b2r_align_up(
                payload_address + payload_size, 0x1000u);
            b2r_native_allocate_pages(state, object, kObjectStride);
            b2r_native_service_write_u32(state, object, kVtable);
            b2r_native_service_write_u32(state, object + 0x10u, payload_size);
            b2r_native_service_write_u32(state, object + 0x14u, 0u);
            b2r_native_service_write_u32(state, object + 0x18u, 0u);
            b2r_native_service_write_u32(state, object + 0x1cu, 0u);
            b2r_native_service_write_u32(state, object + 0x2cu, 2u);
            b2r_native_service_write_u32(
                state, object + 0x30u, payload_address);
            b2r_native_service_write_u32(state, object + 0x34u, payload_size);
            b2r_native_service_write_u32(state, object + 0x38u, 0u);
            b2r_native_service_write_u32(state, kVtable + 4u, kActivateTarget);
            b2r_native_service_write_u32(state, kVtable + 8u, kReadTarget);
            b2r_native_service_write_u32(state, kVtable + 0x10u, kSeekTarget);
            b2r_native_service_write_u32(state, kVtable + 0x1cu, kStatusTarget);
            ++state->title_asset_open_count;
            state->title_asset_payload_bytes += payload_size;
            eax = object;
        } else if (service->value == kAssetActivate) {
            eax = 1u;
        } else if (service->value == kAssetStatus) {
            const uint32_t object = *reinterpret_cast<uint32_t*>(
                bytes + state->ecx_offset);
            const uint32_t payload = object != 0u
                ? b2r_native_service_read_u32(state, object + 0x30u) : 0u;
            eax = payload != 0u ? 1u : 3u;
            if (payload != 0u) {
                b2r_native_service_write_u32(state, object + 0x2cu, 1u);
            }
        } else if (service->value == kAssetRead) {
            const uint32_t object = *reinterpret_cast<uint32_t*>(
                bytes + state->ecx_offset);
            const uint32_t destination = argument(0u);
            const uint32_t requested = argument(1u);
            const uint32_t payload = object != 0u
                ? b2r_native_service_read_u32(state, object + 0x30u) : 0u;
            const uint32_t payload_size = object != 0u
                ? b2r_native_service_read_u32(state, object + 0x34u) : 0u;
            const uint32_t position = object != 0u
                ? b2r_native_service_read_u32(state, object + 0x38u) : 0u;
            const uint32_t available = position < payload_size
                ? payload_size - position : 0u;
            const uint32_t copied = requested < available ? requested : available;
            if (destination != 0u && payload != 0u && copied != 0u) {
                b2r_native_service_copy_bytes(
                    state, destination, payload + position, copied);
            }
            B2RNativeTitleAssetStreamEntry* stream =
                b2r_find_title_asset_stream(state, object);
            if (stream != nullptr) {
                b2r_publish_track_pss_image(state, stream);
            }
            const uint32_t next_position = position + copied;
            if (object != 0u) {
                b2r_native_service_write_u32(
                    state, object + 0x18u, next_position);
                b2r_native_service_write_u32(state, object + 0x1cu, 0u);
                b2r_native_service_write_u32(
                    state, object + 0x38u, next_position);
                b2r_native_service_write_u32(state, object + 0x2cu, 1u);
            }
            eax = copied;
        } else if (service->value == kAssetSeek) {
            const uint32_t object = *reinterpret_cast<uint32_t*>(
                bytes + state->ecx_offset);
            const uint64_t offset_bits =
                (static_cast<uint64_t>(argument(1u)) << 32u) | argument(0u);
            const int64_t offset = static_cast<int64_t>(offset_bits);
            const uint32_t origin = argument(2u);
            const uint32_t payload_size = object != 0u
                ? b2r_native_service_read_u32(state, object + 0x34u) : 0u;
            const uint32_t current = object != 0u
                ? b2r_native_service_read_u32(state, object + 0x38u) : 0u;
            const int64_t base = origin == 0u
                ? 0 : origin == 1u ? current : payload_size;
            int64_t requested_position = base + offset;
            if (requested_position < 0) { requested_position = 0; }
            if (requested_position > payload_size) {
                requested_position = payload_size;
            }
            const uint32_t position = static_cast<uint32_t>(requested_position);
            if (object != 0u) {
                b2r_native_service_write_u32(state, object + 0x18u, position);
                b2r_native_service_write_u32(state, object + 0x1cu, 0u);
                b2r_native_service_write_u32(state, object + 0x38u, position);
                b2r_native_service_write_u32(state, object + 0x2cu, 1u);
            }
            eax = position;
        } else if (service->value == kStaticDriveSetup) {
            const uint32_t object = *reinterpret_cast<uint32_t*>(
                bytes + state->ecx_offset);
            const uint32_t count = argument(0u);
            const uint32_t element_base = argument(1u);
            const uint32_t descriptor = argument(3u);
            const uint32_t bounded_count = count < 16u ? count : 16u;
            if (object != 0u) {
                b2r_native_service_write_u32(state, object + 4u, 1u);
                b2r_native_service_write_u32(state, object + 8u, 0u);
                b2r_native_service_write_u32(state, object + 0xcu, count);
                b2r_native_service_write_u32(
                    state, object + 0x10u, element_base);
            }
            const uint32_t registry_count =
                b2r_native_service_read_u32(state, 0x00598248u);
            if (registry_count < 2u) {
                const uint32_t descriptor_tag = descriptor != 0u
                    ? b2r_native_service_read_u32(state, descriptor) : 0u;
                b2r_native_service_write_u32(
                    state, 0x00598234u + registry_count * 4u, object);
                b2r_native_service_write_u32(
                    state, 0x0059823cu + registry_count * 5u,
                    descriptor_tag);
                b2r_native_service_write_u32(
                    state, 0x00598248u, registry_count + 1u);
            }
            for (uint32_t index = 0u; index < bounded_count; ++index) {
                const uint32_t element = element_base + index * 0x50u;
                b2r_native_service_write_u32(state, element, descriptor);
                b2r_native_service_write_u32(state, element + 0x20u, 0u);
            }
            eax = 1u;
        } else if (service->value == kD3dFlush) {
            const uint32_t context = *reinterpret_cast<uint32_t*>(
                bytes + state->ecx_offset);
            const uint32_t current_push = context != 0u
                ? b2r_native_service_read_u32(state, context) : 0u;
            const uint32_t dma_state = context != 0u
                ? b2r_native_service_read_u32(state, context + 0x17f4u) : 0u;
            if (dma_state != 0u) {
                b2r_native_service_write_u32(
                    state, dma_state + 0x40u,
                    current_push & 0x0fffffffu);
            }
            eax = 0u;
        } else if (service->value == kSpinDelay) {
            uint64_t& timestamp_counter = *reinterpret_cast<uint64_t*>(
                bytes + state->timestamp_counter_offset);
            uint8_t* flags = bytes + state->flags_offset;
            timestamp_counter += 400u;
            flags[1] = 1u;
            flags[2] = 0u;
            flags[3] = 1u;
            flags[4] = 0u;
            flags[5] = 0u;
            eax = 0u;
        } else if (service->value == kFrontendSpecialAudioCreate) {
            constexpr uint32_t kHandle = 0x31f10400u;
            constexpr uint32_t kListSentinel = kHandle + 0x0cu;
            b2r_native_service_write_u32(state, kHandle, 1u);
            b2r_native_service_write_u32(
                state, kHandle + 0x10u, kListSentinel);
            eax = kHandle;
        } else if (service->value == kMusicModeSet) {
            constexpr uint32_t kSyntheticManager = 0x31fe0000u;
            const uint32_t holder = *reinterpret_cast<uint32_t*>(
                bytes + state->ecx_offset);
            uint32_t object = holder != 0u
                ? b2r_native_service_read_u32(state, holder) : 0u;
            if (object == 0u && holder != 0u) {
                object = kSyntheticManager;
                b2r_native_service_write_u32(state, holder, object);
            }
            if (object != 0u) {
                b2r_native_service_write_u32(
                    state, object + 0x38u, argument(0u));
            }
            eax = 1u;
        } else if (service->value == kTextDraw) {
            const uint32_t string_address = argument(0u);
            uint32_t length = 0u;
            while (length + 1u < sizeof(state->live_frontend_text)) {
                const uint8_t value = b2r_native_service_read_u8(
                    state, string_address + length);
                if (value == 0u) { break; }
                state->live_frontend_text[length++] =
                    static_cast<char>(value);
            }
            state->live_frontend_text[length] = '\0';
            state->live_frontend_text_x_bits = argument(1u);
            state->live_frontend_text_y_bits = argument(2u);
            state->live_frontend_text_size_bits = argument(3u);
            const uint32_t color_address = argument(4u);
            auto color_component = [&](uint32_t offset) {
                const uint32_t bits = b2r_native_service_read_u32(
                    state, color_address + offset);
                float value = 0.0f;
                std::memcpy(&value, &bits, sizeof(value));
                return value != value
                    ? 0u
                    : static_cast<uint32_t>(
                        static_cast<int32_t>(value)) & 0xffu;
            };
            state->live_frontend_text_color_argb =
                (color_component(0x0cu) << 24u) |
                (color_component(0x00u) << 16u) |
                (color_component(0x04u) << 8u) |
                color_component(0x08u);
            state->live_frontend_text_pending = length != 0u;
        } else {
            return false;
        }
        break;
    }
    case 15u: {
        constexpr uint32_t kAvSendTvEncoderOption = 1u;
        constexpr uint32_t kHalGetInterruptVector = 2u;
        constexpr uint32_t kHalReadWritePciSpace = 3u;
        constexpr uint32_t kIoCreateDevice = 4u;
        constexpr uint32_t kKeConnectInterrupt = 5u;
        constexpr uint32_t kKeInitializeInterrupt = 6u;
        constexpr uint32_t kKeStallExecutionProcessor = 7u;
        constexpr uint32_t kMmClaimGpuInstanceMemory = 8u;
        constexpr uint32_t kNtClose = 9u;
        constexpr uint32_t kKeSetEvent = 10u;
        constexpr uint32_t kNtCreateFile = 11u;
        constexpr uint32_t kNtOpenFile = 12u;
        constexpr uint32_t kNtOpenSymbolicLinkObject = 13u;
        constexpr uint32_t kNtQueryInformationFile = 14u;
        constexpr uint32_t kNtQuerySymbolicLinkObject = 15u;
        constexpr uint32_t kNtQueryVolumeInformationFile = 16u;
        constexpr uint32_t kAvGetSavedDataAddress = 17u;
        constexpr uint32_t kAvSetDisplayMode = 18u;
        constexpr uint32_t kAvSetSavedDataAddress = 19u;
        constexpr uint32_t kNtReadFile = 20u;
        constexpr uint32_t kNtSetInformationFile = 21u;
        constexpr uint32_t kNtWriteFile = 22u;
        constexpr uint32_t kNtQueryDirectoryFile = 23u;
        constexpr uint32_t kXcShaInit = 24u;
        constexpr uint32_t kXcShaUpdate = 25u;
        constexpr uint32_t kXcShaFinal = 26u;
        constexpr uint32_t kXcRc4Key = 27u;
        constexpr uint32_t kXcRc4Crypt = 28u;
        constexpr uint32_t kXcHmac = 29u;
        constexpr uint32_t kRtlTimeFieldsToTime = 30u;
        constexpr uint32_t kRtlTimeToTimeFields = 31u;
        constexpr uint32_t kNtDeviceIoControlFile = 32u;
        constexpr uint32_t kNtFsControlFile = 33u;
        if (service->value == kAvSendTvEncoderOption) {
            const uint32_t output = argument(3u);
            if (output != 0u) {
                b2r_native_service_write_u32(state, output, 0x00400101u);
            }
            eax = 0u;
        } else if (service->value == kHalGetInterruptVector) {
            const uint32_t level = argument(0u);
            const uint32_t vector = argument(1u);
            eax = 0x20u + ((level != 0u ? level : vector) & 0x1fu);
        } else if (service->value == kHalReadWritePciSpace) {
            const uint32_t buffer = argument(3u);
            const uint32_t size = argument(4u);
            const bool write = argument(5u) != 0u;
            if (!write && buffer != 0u) {
                for (uint32_t offset = 0u; offset < size; ++offset) {
                    b2r_native_service_write_u8(state, buffer + offset, 0u);
                }
            }
            eax = 0u;
        } else if (service->value == kIoCreateDevice) {
            if (state->next_object_handle < 0x108u) {
                state->next_object_handle = 0x108u;
            }
            state->next_object_handle += 4u;
            eax = 0u;
        } else if (service->value == kKeConnectInterrupt) {
            eax = 1u;
        } else if (service->value == kKeInitializeInterrupt) {
            // The kernel export is void; preserve EAX.
        } else if (service->value == kKeStallExecutionProcessor) {
            state->performance_counter += argument(0u);
            eax = 0u;
        } else if (service->value == kMmClaimGpuInstanceMemory) {
            eax = b2r_native_allocate_range(
                state, argument(0u), 0x1000u, true);
        } else if (service->value == kNtClose) {
            B2RNativeSemaphoreEntry* semaphore = b2r_find_native_semaphore(
                state, argument(0u));
            if (semaphore != nullptr) {
                semaphore->handle = 0u;
                semaphore->count = 0u;
                semaphore->limit = 0u;
            }
            B2RNativeFileEntry* file = b2r_find_native_file(
                state, argument(0u));
            if (file != nullptr) {
                std::FILE* host_file = reinterpret_cast<std::FILE*>(
                    static_cast<uintptr_t>(file->host_file));
                if (host_file != nullptr) { std::fclose(host_file); }
                file->host_file = 0u;
                file->active = 0u;
            }
            eax = 0u;
        } else if (service->value == kKeSetEvent) {
            const uint32_t event = argument(0u);
            const uint32_t previous = event != 0u
                ? b2r_native_service_read_u32(state, event + 4u) : 0u;
            if (event != 0u) {
                b2r_native_service_write_u32(state, event + 4u, 1u);
            }
            eax = previous;
        } else if (service->value == kNtCreateFile ||
                   service->value == kNtOpenFile) {
            constexpr uint32_t kStatusInvalidParameter = 0xc000000du;
            constexpr uint32_t kStatusAccessDenied = 0xc0000022u;
            constexpr uint32_t kStatusObjectNameNotFound = 0xc0000034u;
            constexpr uint32_t kStatusObjectNameCollision = 0xc0000035u;
            const uint32_t output_handle = argument(0u);
            const uint32_t desired_access = argument(1u);
            const uint32_t object_attributes = argument(2u);
            const uint32_t io_status = argument(3u);
            const uint32_t create_options = service->value == kNtCreateFile
                ? argument(8u) : argument(5u);
            const uint32_t create_disposition = service->value == kNtCreateFile
                ? argument(7u) : 1u;
            uint32_t io_information = 0u;
            char path[512] = {};
            char host_path[1536] = {};
            uint32_t path_length = 0u;
            if (!b2r_native_decode_object_path(
                    state, object_attributes, path,
                    static_cast<uint32_t>(sizeof(path)), &path_length)) {
                eax = kStatusInvalidParameter;
            } else {
                const bool disc = b2r_native_path_contains(
                    path, path_length, "cdrom");
                const uint32_t recorded_guest_length = path_length <
                    sizeof(state->last_file_guest_path) - 1u
                    ? path_length
                    : static_cast<uint32_t>(
                          sizeof(state->last_file_guest_path) - 1u);
                std::memcpy(
                    state->last_file_guest_path, path, recorded_guest_length);
                state->last_file_guest_path[recorded_guest_length] = '\0';
                state->last_file_host_path[0] = '\0';
                const bool directory = (create_options & 1u) != 0u ||
                    path[path_length - 1u] == '\\' ||
                    b2r_native_path_ends_with(path, path_length, "cdrom0");
                const bool raw_partition_zero =
                    b2r_native_path_is_raw_partition_zero(path, path_length);
                const bool raw_cache_partition =
                    b2r_native_path_raw_cache_partition(
                        path,
                        path_length,
                        nullptr);
                const bool raw_partition =
                    raw_partition_zero || raw_cache_partition;
                std::FILE* host_file = nullptr;
                uint64_t file_size = b2r_native_known_file_size(
                    path, path_length);
                const bool mapped = b2r_native_build_extracted_path(
                    state, path, path_length, host_path,
                    static_cast<uint32_t>(sizeof(host_path)));
                if (mapped) {
                    std::memcpy(
                        state->last_file_host_path,
                        host_path,
                        sizeof(state->last_file_host_path));
                    std::error_code error;
                    const std::filesystem::path resolved(host_path);
                    const bool existed = std::filesystem::exists(
                        resolved, error);
                    const bool writable_path = b2r_native_path_is_writable(
                        path, path_length);
                    const bool wants_write =
                        (desired_access & 0x40000006u) != 0u ||
                        create_disposition == 0u ||
                        create_disposition == 2u ||
                        create_disposition == 3u ||
                        create_disposition == 4u ||
                        create_disposition == 5u;
                    if (error) {
                        eax = kStatusInvalidParameter;
                    } else if (directory) {
                        if (existed && !std::filesystem::is_directory(
                                resolved, error)) {
                            eax = kStatusInvalidParameter;
                        } else if (existed && create_disposition == 2u) {
                            eax = kStatusObjectNameCollision;
                        } else if (!existed &&
                                   create_disposition != 2u &&
                                   create_disposition != 3u) {
                            eax = kStatusObjectNameNotFound;
                        } else if (!existed && !writable_path) {
                            eax = kStatusAccessDenied;
                        } else if (!existed &&
                                   !std::filesystem::create_directories(
                                       resolved, error) && error) {
                            eax = kStatusInvalidParameter;
                        } else {
                            io_information = existed ? 1u : 2u;
                            eax = 0u;
                        }
                    } else if (existed && std::filesystem::is_directory(
                                   resolved, error)) {
                        eax = kStatusInvalidParameter;
                    } else if (existed && create_disposition == 2u) {
                        eax = kStatusObjectNameCollision;
                    } else if (!existed &&
                               (service->value == kNtOpenFile ||
                                create_disposition == 1u ||
                                create_disposition == 4u) &&
                               !raw_partition) {
                        eax = kStatusObjectNameNotFound;
                    } else if (wants_write && !writable_path) {
                        eax = kStatusAccessDenied;
                    } else {
                        const bool truncate =
                            create_disposition == 0u ||
                            create_disposition == 4u ||
                            create_disposition == 5u;
                        if (!existed) {
                            std::filesystem::create_directories(
                                resolved.parent_path(), error);
                        }
                        if (!error) {
                            host_file = std::fopen(
                                host_path,
                                wants_write
                                    ? (truncate || !existed ? "w+b" : "r+b")
                                    : "rb");
                        }
                        if (host_file == nullptr) {
                            eax = kStatusInvalidParameter;
                        } else {
                            io_information = existed
                                ? (truncate ? 3u : 1u) : 2u;
                            eax = 0u;
                        }
                    }
                    if (eax == 0u && host_file != nullptr &&
                        raw_partition) {
                        constexpr long kPartitionTableEnd = 0xa00;
                        constexpr long kCachePartitionSize = 32 * 1024 * 1024;
                        const long required_size = raw_partition_zero
                            ? kPartitionTableEnd : kCachePartitionSize;
                        if (std::fseek(host_file, 0, SEEK_END) != 0) {
                            eax = kStatusInvalidParameter;
                        } else {
                            const long host_size = std::ftell(host_file);
                            if (host_size < 0) {
                                eax = kStatusInvalidParameter;
                            } else if (host_size < required_size &&
                                       (std::fseek(
                                            host_file,
                                            required_size - 1,
                                            SEEK_SET) != 0 ||
                                        std::fputc(0, host_file) == EOF ||
                                        std::fflush(host_file) != 0)) {
                                eax = kStatusInvalidParameter;
                            }
                        }
                    }
                    if (eax != 0u && host_file != nullptr) {
                        std::fclose(host_file);
                        host_file = nullptr;
                    }
                    if (eax == 0u && host_file != nullptr &&
                        std::fseek(host_file, 0, SEEK_END) == 0) {
                        const long host_size = std::ftell(host_file);
                        if (host_size >= 0 &&
                            std::fseek(host_file, 0, SEEK_SET) == 0) {
                            file_size = static_cast<uint64_t>(host_size);
                        } else {
                            std::fclose(host_file);
                            host_file = nullptr;
                            eax = kStatusInvalidParameter;
                        }
                    }
                } else {
                    eax = kStatusInvalidParameter;
                }
                if (eax == 0u) {
                    B2RNativeFileEntry* file = b2r_register_native_file(
                        state,
                        (directory ? 1u : 0u) |
                            (disc ? 2u : 0u) |
                            (host_file != nullptr &&
                             (desired_access & 0x40000006u) != 0u ? 8u : 0u) |
                            (raw_partition ? 16u : 0u),
                        file_size, host_file, path, host_path);
                    if (file == nullptr) {
                        if (host_file != nullptr) { std::fclose(host_file); }
                        eax = kStatusInvalidParameter;
                        io_information = 0u;
                    } else if (output_handle != 0u) {
                        b2r_native_service_write_u32(
                            state, output_handle, file->handle);
                    }
                }
            }
            if (io_status != 0u) {
                b2r_native_service_write_u32(state, io_status, eax);
                b2r_native_service_write_u32(
                    state, io_status + 4u,
                    eax == 0u ? io_information : 0u);
            }
        } else if (service->value == kNtOpenSymbolicLinkObject) {
            constexpr uint32_t kStatusObjectNameNotFound = 0xc0000034u;
            const uint32_t output_handle = argument(0u);
            char path[512] = {};
            uint32_t path_length = 0u;
            if (!b2r_native_decode_object_path(
                    state, argument(1u), path,
                    static_cast<uint32_t>(sizeof(path)), &path_length) ||
                !b2r_native_path_ends_with(path, path_length, "\\??\\d:")) {
                eax = kStatusObjectNameNotFound;
            } else {
                B2RNativeFileEntry* link = b2r_register_native_file(
                    state, 4u, 0u);
                if (link == nullptr) {
                    eax = kStatusObjectNameNotFound;
                } else {
                    if (output_handle != 0u) {
                        b2r_native_service_write_u32(
                            state, output_handle, link->handle);
                    }
                    eax = 0u;
                }
            }
        } else if (service->value == kNtQueryInformationFile) {
            constexpr uint32_t kStatusInvalidHandle = 0xc0000008u;
            B2RNativeFileEntry* file = b2r_find_native_file(
                state, argument(0u));
            const uint32_t io_status = argument(1u);
            uint32_t bytes_written = 0u;
            if (file == nullptr || (file->flags & 4u) != 0u) {
                eax = kStatusInvalidHandle;
            } else {
                bytes_written = b2r_native_write_file_information(
                    state, file, argument(2u), argument(3u), argument(4u));
                eax = 0u;
            }
            if (io_status != 0u) {
                b2r_native_service_write_u32(state, io_status, eax);
                b2r_native_service_write_u32(
                    state, io_status + 4u, eax == 0u ? bytes_written : 0u);
            }
        } else if (service->value == kNtQueryDirectoryFile) {
            constexpr uint32_t kStatusNoMoreFiles = 0x80000006u;
            constexpr uint32_t kStatusBufferTooSmall = 0xc0000023u;
            constexpr uint32_t kStatusInvalidInfoClass = 0xc0000003u;
            constexpr uint32_t kStatusInvalidHandle = 0xc0000008u;
            constexpr uint32_t kStatusInvalidParameter = 0xc000000du;
            B2RNativeFileEntry* file = b2r_find_native_file(
                state, argument(0u));
            const uint32_t io_status = argument(4u);
            const uint32_t destination = argument(5u);
            const uint32_t requested_length = argument(6u);
            const uint32_t information_class = argument(7u);
            const uint32_t name_descriptor = argument(8u);
            uint32_t bytes_written = 0u;
            eax = 0u;
            char requested_pattern[256] = {};
            uint32_t requested_pattern_length = 0u;
            if (name_descriptor != 0u) {
                requested_pattern_length =
                    static_cast<uint32_t>(b2r_native_service_read_u8(
                        state, name_descriptor)) |
                    (static_cast<uint32_t>(b2r_native_service_read_u8(
                        state, name_descriptor + 1u)) << 8u);
                const uint32_t pattern_buffer = b2r_native_service_read_u32(
                    state, name_descriptor + 4u);
                if (requested_pattern_length >= sizeof(requested_pattern) ||
                    (requested_pattern_length != 0u && pattern_buffer == 0u)) {
                    eax = kStatusInvalidParameter;
                } else {
                    for (uint32_t index = 0u;
                         index < requested_pattern_length; ++index) {
                        requested_pattern[index] = static_cast<char>(
                            b2r_native_service_read_u8(
                                state, pattern_buffer + index));
                    }
                    requested_pattern[requested_pattern_length] = '\0';
                }
            }
            if (eax == 0u && information_class != 1u) {
                eax = kStatusInvalidInfoClass;
            } else if (eax == 0u && file == nullptr) {
                eax = kStatusInvalidHandle;
            } else if (eax == 0u && (file->flags & 1u) == 0u) {
                eax = kStatusInvalidParameter;
            } else if (eax == 0u &&
                       (destination == 0u || requested_length == 0u)) {
                eax = kStatusInvalidParameter;
            }
            if (eax == 0u) {
                const char* pattern = requested_pattern_length != 0u
                    ? requested_pattern
                    : (file->directory_initialized != 0u
                        ? file->directory_pattern : "*");
                if (std::strcmp(pattern, "*.*") == 0) { pattern = "*"; }
                const bool restart = argument(9u) != 0u ||
                    file->directory_initialized == 0u ||
                    b2r_native_compare_names(
                        pattern, file->directory_pattern) != 0;
                if (restart) {
                    std::snprintf(
                        file->directory_pattern,
                        sizeof(file->directory_pattern),
                        "%s",
                        pattern);
                    file->directory_index = 0u;
                    file->directory_initialized = 1u;
                }

                char search_path[800] = {};
                const size_t host_path_length = std::strlen(file->host_path);
                const char separator = host_path_length != 0u &&
                    (file->host_path[host_path_length - 1u] == '\\' ||
                     file->host_path[host_path_length - 1u] == '/')
                    ? '\0' : '\\';
                const int search_length = separator == '\0'
                    ? std::snprintf(
                        search_path, sizeof(search_path), "%s*", file->host_path)
                    : std::snprintf(
                        search_path, sizeof(search_path), "%s\\*", file->host_path);
                std::vector<WIN32_FIND_DATAA> entries;
                if (search_length <= 0 ||
                    static_cast<size_t>(search_length) >= sizeof(search_path)) {
                    eax = kStatusInvalidParameter;
                } else {
                    WIN32_FIND_DATAA data = {};
                    HANDLE search = FindFirstFileA(search_path, &data);
                    if (search == INVALID_HANDLE_VALUE) {
                        eax = GetLastError() == ERROR_FILE_NOT_FOUND
                            ? 0u : kStatusInvalidParameter;
                    } else {
                        do {
                            if (std::strcmp(data.cFileName, ".") != 0 &&
                                std::strcmp(data.cFileName, "..") != 0 &&
                                b2r_native_wildcard_match(
                                    file->directory_pattern, data.cFileName)) {
                                entries.push_back(data);
                            }
                        } while (FindNextFileA(search, &data) != 0);
                        FindClose(search);
                        std::sort(
                            entries.begin(), entries.end(),
                            [](const WIN32_FIND_DATAA& left,
                               const WIN32_FIND_DATAA& right) {
                                const int folded = b2r_native_compare_names(
                                    left.cFileName, right.cFileName);
                                return folded != 0
                                    ? folded < 0
                                    : std::strcmp(
                                        left.cFileName, right.cFileName) < 0;
                            });
                    }
                }
                if (eax == 0u && file->directory_index >= entries.size()) {
                    eax = kStatusNoMoreFiles;
                } else if (eax == 0u) {
                    const WIN32_FIND_DATAA& entry =
                        entries[file->directory_index];
                    const uint32_t name_length = static_cast<uint32_t>(
                        std::strlen(entry.cFileName));
                    const uint32_t required_length = 0x40u + name_length;
                    if (requested_length < required_length) {
                        eax = kStatusBufferTooSmall;
                    } else {
                        for (uint32_t index = 0u; index < required_length; ++index) {
                            b2r_native_service_write_u8(
                                state, destination + index, 0u);
                        }
                        const uint64_t creation_time =
                            (static_cast<uint64_t>(
                                entry.ftCreationTime.dwHighDateTime) << 32u) |
                            entry.ftCreationTime.dwLowDateTime;
                        const uint64_t access_time =
                            (static_cast<uint64_t>(
                                entry.ftLastAccessTime.dwHighDateTime) << 32u) |
                            entry.ftLastAccessTime.dwLowDateTime;
                        const uint64_t write_time =
                            (static_cast<uint64_t>(
                                entry.ftLastWriteTime.dwHighDateTime) << 32u) |
                            entry.ftLastWriteTime.dwLowDateTime;
                        const bool directory =
                            (entry.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) != 0u;
                        const uint64_t size = directory ? 0u :
                            (static_cast<uint64_t>(entry.nFileSizeHigh) << 32u) |
                            entry.nFileSizeLow;
                        const uint64_t allocation_size = size != 0u
                            ? (size + 0xfffu) & ~0xfffull : 0u;
                        b2r_native_service_write_u32(
                            state, destination + 4u, file->directory_index);
                        b2r_native_service_write_u64(
                            state, destination + 8u, creation_time);
                        b2r_native_service_write_u64(
                            state, destination + 16u, access_time);
                        b2r_native_service_write_u64(
                            state, destination + 24u, write_time);
                        b2r_native_service_write_u64(
                            state, destination + 32u, write_time);
                        b2r_native_service_write_u64(
                            state, destination + 40u, size);
                        b2r_native_service_write_u64(
                            state, destination + 48u, allocation_size);
                        b2r_native_service_write_u32(
                            state, destination + 56u,
                            directory ? 0x10u : 0x80u);
                        b2r_native_service_write_u32(
                            state, destination + 60u, name_length);
                        for (uint32_t index = 0u; index < name_length; ++index) {
                            b2r_native_service_write_u8(
                                state, destination + 64u + index,
                                static_cast<uint8_t>(entry.cFileName[index]));
                        }
                        ++file->directory_index;
                        bytes_written = required_length;
                    }
                }
            }
            if (io_status != 0u) {
                b2r_native_service_write_u32(state, io_status, eax);
                b2r_native_service_write_u32(
                    state, io_status + 4u, eax == 0u ? bytes_written : 0u);
            }
        } else if (service->value == kNtQuerySymbolicLinkObject) {
            constexpr uint32_t kStatusInvalidHandle = 0xc0000008u;
            static constexpr char kCdromTarget[] = "\\Device\\Cdrom0";
            B2RNativeFileEntry* link = b2r_find_native_file(
                state, argument(0u));
            const uint32_t descriptor = argument(1u);
            uint32_t written_length = 0u;
            if (link == nullptr || (link->flags & 4u) == 0u ||
                descriptor == 0u) {
                eax = kStatusInvalidHandle;
            } else {
                const uint32_t maximum_length =
                    static_cast<uint32_t>(
                        b2r_native_service_read_u8(state, descriptor + 2u)) |
                    (static_cast<uint32_t>(
                        b2r_native_service_read_u8(state, descriptor + 3u)) << 8u);
                const uint32_t buffer = b2r_native_service_read_u32(
                    state, descriptor + 4u);
                constexpr uint32_t kTargetLength =
                    static_cast<uint32_t>(sizeof(kCdromTarget) - 1u);
                written_length = maximum_length < kTargetLength
                    ? maximum_length : kTargetLength;
                if (buffer != 0u) {
                    for (uint32_t index = 0u; index < written_length; ++index) {
                        b2r_native_service_write_u8(
                            state, buffer + index,
                            static_cast<uint8_t>(kCdromTarget[index]));
                    }
                    b2r_native_service_write_u16(
                        state, descriptor,
                        static_cast<uint16_t>(written_length));
                } else {
                    written_length = 0u;
                }
                eax = 0u;
            }
            if (argument(2u) != 0u) {
                b2r_native_service_write_u32(
                    state, argument(2u), written_length);
            }
        } else if (service->value == kNtQueryVolumeInformationFile) {
            constexpr uint32_t kStatusInvalidHandle = 0xc0000008u;
            constexpr uint32_t kStatusInvalidInfoClass = 0xc0000003u;
            constexpr uint32_t kStatusInfoLengthMismatch = 0xc0000004u;
            B2RNativeFileEntry* file = b2r_find_native_file(
                state, argument(0u));
            const uint32_t io_status = argument(1u);
            const uint32_t destination = argument(2u);
            const uint32_t requested_length = argument(3u);
            const uint32_t information_class = argument(4u);
            uint32_t bytes_written = 0u;
            if (file == nullptr || (file->flags & 4u) != 0u) {
                eax = kStatusInvalidHandle;
            } else if (information_class == 3u) {
                constexpr uint32_t kPayloadSize = 24u;
                if (requested_length < kPayloadSize || destination == 0u) {
                    eax = kStatusInfoLengthMismatch;
                } else {
                    const bool disc = (file->flags & 2u) != 0u;
                    b2r_native_service_write_u64(
                        state, destination, disc ? 3820880u : 312501u);
                    b2r_native_service_write_u64(
                        state, destination + 8u, disc ? 0u : 312501u);
                    b2r_native_service_write_u32(
                        state, destination + 16u, disc ? 1u : 32u);
                    b2r_native_service_write_u32(
                        state, destination + 20u, disc ? 2048u : 512u);
                    bytes_written = kPayloadSize;
                    eax = 0u;
                }
            } else if (information_class == 1u) {
                static constexpr char kDiscLabel[] = "BURNOUT2";
                static constexpr char kSaveLabel[] = "XBOX DATA";
                const bool disc = (file->flags & 2u) != 0u;
                const char* label = disc ? kDiscLabel : kSaveLabel;
                const uint32_t label_length = disc ? 8u : 9u;
                const uint32_t payload_size = 17u + label_length;
                if (requested_length < payload_size || destination == 0u) {
                    eax = kStatusInfoLengthMismatch;
                } else {
                    b2r_native_service_write_u64(state, destination, 0u);
                    b2r_native_service_write_u32(
                        state, destination + 8u, 0x41430019u);
                    b2r_native_service_write_u32(
                        state, destination + 12u, label_length);
                    b2r_native_service_write_u8(state, destination + 16u, 0u);
                    for (uint32_t index = 0u; index < label_length; ++index) {
                        b2r_native_service_write_u8(
                            state, destination + 17u + index,
                            static_cast<uint8_t>(label[index]));
                    }
                    bytes_written = payload_size;
                    eax = 0u;
                }
            } else {
                eax = kStatusInvalidInfoClass;
            }
            if (io_status != 0u) {
                b2r_native_service_write_u32(state, io_status, eax);
                b2r_native_service_write_u32(
                    state, io_status + 4u, eax == 0u ? bytes_written : 0u);
            }
        } else if (service->value == kNtDeviceIoControlFile) {
            constexpr uint32_t kStatusInvalidHandle = 0xc0000008u;
            constexpr uint32_t kStatusInvalidDeviceRequest = 0xc0000010u;
            constexpr uint32_t kStatusBufferTooSmall = 0xc0000023u;
            constexpr uint32_t kIoctlDiskGetDriveGeometry = 0x00070000u;
            constexpr uint32_t kIoctlDiskGetPartitionInfo = 0x00074004u;
            constexpr uint64_t kCachePartitionSize = 32ull * 1024ull * 1024ull;
            B2RNativeFileEntry* file = b2r_find_native_file(
                state, argument(0u));
            const uint32_t io_status = argument(4u);
            const uint32_t io_control_code = argument(5u);
            const uint32_t output = argument(8u);
            const uint32_t output_length = argument(9u);
            uint32_t bytes_written = 0u;
            if (file == nullptr || (file->flags & 4u) != 0u) {
                eax = kStatusInvalidHandle;
            } else if ((file->flags & 16u) == 0u) {
                eax = kStatusInvalidDeviceRequest;
            } else if (io_control_code == kIoctlDiskGetDriveGeometry) {
                constexpr uint32_t kPayloadSize = 24u;
                if (output == 0u || output_length < kPayloadSize) {
                    eax = kStatusBufferTooSmall;
                } else {
                    b2r_native_service_write_u64(state, output, 128u);
                    b2r_native_service_write_u32(state, output + 8u, 12u);
                    b2r_native_service_write_u32(state, output + 12u, 32u);
                    b2r_native_service_write_u32(state, output + 16u, 16u);
                    b2r_native_service_write_u32(state, output + 20u, 512u);
                    bytes_written = kPayloadSize;
                    eax = 0u;
                }
            } else if (io_control_code == kIoctlDiskGetPartitionInfo) {
                constexpr uint32_t kPayloadSize = 32u;
                if (output == 0u || output_length < kPayloadSize) {
                    eax = kStatusBufferTooSmall;
                } else {
                    uint32_t partition_number = 0u;
                    b2r_native_path_raw_cache_partition(
                        file->guest_path,
                        static_cast<uint32_t>(std::strlen(file->guest_path)),
                        &partition_number);
                    b2r_native_service_write_u64(state, output, 0u);
                    b2r_native_service_write_u64(
                        state, output + 8u, kCachePartitionSize);
                    b2r_native_service_write_u32(state, output + 16u, 0u);
                    b2r_native_service_write_u32(
                        state, output + 20u, partition_number);
                    b2r_native_service_write_u32(state, output + 24u, 0u);
                    b2r_native_service_write_u32(state, output + 28u, 0u);
                    bytes_written = kPayloadSize;
                    eax = 0u;
                }
            } else {
                eax = kStatusInvalidDeviceRequest;
            }
            if (io_status != 0u) {
                b2r_native_service_write_u32(state, io_status, eax);
                b2r_native_service_write_u32(
                    state, io_status + 4u,
                    eax == 0u ? bytes_written : 0u);
            }
        } else if (service->value == kNtFsControlFile) {
            constexpr uint32_t kStatusInvalidHandle = 0xc0000008u;
            constexpr uint32_t kStatusInvalidDeviceRequest = 0xc0000010u;
            constexpr uint32_t kStatusInvalidParameter = 0xc000000du;
            constexpr uint32_t kFsctlDismountVolume = 0x00090020u;
            B2RNativeFileEntry* file = b2r_find_native_file(
                state, argument(0u));
            const uint32_t io_status = argument(4u);
            if (file == nullptr || (file->flags & 4u) != 0u) {
                eax = kStatusInvalidHandle;
            } else if ((file->flags & 16u) == 0u ||
                       argument(5u) != kFsctlDismountVolume) {
                eax = kStatusInvalidDeviceRequest;
            } else {
                std::FILE* host_file = reinterpret_cast<std::FILE*>(
                    static_cast<uintptr_t>(file->host_file));
                eax = host_file != nullptr && std::fflush(host_file) == 0
                    ? 0u : kStatusInvalidParameter;
            }
            if (io_status != 0u) {
                b2r_native_service_write_u32(state, io_status, eax);
                b2r_native_service_write_u32(state, io_status + 4u, 0u);
            }
        } else if (service->value == kAvGetSavedDataAddress) {
            eax = state->av_saved_data_address;
        } else if (service->value == kAvSetDisplayMode) {
            ++state->av_display_mode_set_count;
            eax = 0u;
        } else if (service->value == kAvSetSavedDataAddress) {
            state->av_saved_data_address = argument(0u);
            eax = 0u;
        } else if (service->value == kNtReadFile) {
            constexpr uint32_t kStatusInvalidHandle = 0xc0000008u;
            constexpr uint32_t kStatusInvalidParameter = 0xc000000du;
            B2RNativeFileEntry* file = b2r_find_native_file(
                state, argument(0u));
            const uint32_t io_status = argument(4u);
            const uint32_t destination = argument(5u);
            const uint32_t requested = argument(6u);
            const uint32_t offset_address = argument(7u);
            uint32_t copied = 0u;
            std::FILE* host_file = file != nullptr
                ? reinterpret_cast<std::FILE*>(
                    static_cast<uintptr_t>(file->host_file))
                : nullptr;
            if (file == nullptr || host_file == nullptr) {
                eax = kStatusInvalidHandle;
            } else if (destination == 0u && requested != 0u) {
                eax = kStatusInvalidParameter;
            } else {
                uint64_t offset = file->position;
                if (offset_address != 0u) {
                    offset = b2r_native_service_read_u64(
                        state, offset_address);
                }
                if (offset > static_cast<uint64_t>(LONG_MAX) ||
                    std::fseek(host_file, static_cast<long>(offset), SEEK_SET) != 0 ||
                    !b2r_native_allocate_pages(
                        state, destination, requested != 0u ? requested : 1u)) {
                    eax = kStatusInvalidParameter;
                } else {
                    while (copied < requested) {
                        const uint32_t address = destination + copied;
                        const uint32_t cached =
                            b2r_native_service_cache_address(state, address);
                        const uint32_t page = cached >> 12u;
                        const uint32_t page_offset = cached & 0xfffu;
                        uint32_t chunk = 0x1000u - page_offset;
                        if (chunk > requested - copied) {
                            chunk = requested - copied;
                        }
                        const size_t read = std::fread(
                            state->read_pages[page] + page_offset,
                            1u, chunk, host_file);
                        if (read != 0u) {
                            b2r_native_service_mark_dirty(
                                state, page,
                                static_cast<uint16_t>(page_offset),
                                static_cast<uint16_t>(read));
                            copied += static_cast<uint32_t>(read);
                        }
                        if (read != chunk) { break; }
                    }
                    file->position = offset + copied;
                    eax = std::ferror(host_file) == 0
                        ? 0u : kStatusInvalidParameter;
                }
            }
            if (io_status != 0u) {
                b2r_native_service_write_u32(state, io_status, eax);
                b2r_native_service_write_u32(
                    state, io_status + 4u, eax == 0u ? copied : 0u);
            }
        } else if (service->value == kNtSetInformationFile) {
            constexpr uint32_t kStatusInvalidHandle = 0xc0000008u;
            constexpr uint32_t kStatusInvalidParameter = 0xc000000du;
            B2RNativeFileEntry* file = b2r_find_native_file(
                state, argument(0u));
            const uint32_t io_status = argument(1u);
            const uint32_t source = argument(2u);
            const uint32_t source_length = argument(3u);
            const uint32_t information_class = argument(4u);
            uint32_t consumed = 0u;
            if (file == nullptr || (file->flags & 4u) != 0u) {
                eax = kStatusInvalidHandle;
            } else if (information_class == 14u) {
                if (source == 0u || source_length < 8u) {
                    eax = kStatusInvalidParameter;
                } else {
                    const uint64_t position = b2r_native_service_read_u64(
                        state, source);
                    if ((position & 0x8000000000000000ull) != 0u) {
                        eax = kStatusInvalidParameter;
                    } else {
                        file->position = position;
                        consumed = 8u;
                        eax = 0u;
                    }
                }
            } else {
                consumed = source_length < 8u ? source_length : 8u;
                eax = 0u;
            }
            if (io_status != 0u) {
                b2r_native_service_write_u32(state, io_status, eax);
                b2r_native_service_write_u32(
                    state, io_status + 4u, eax == 0u ? consumed : 0u);
            }
        } else if (service->value == kNtWriteFile) {
            constexpr uint32_t kStatusInvalidHandle = 0xc0000008u;
            constexpr uint32_t kStatusInvalidParameter = 0xc000000du;
            constexpr uint32_t kStatusAccessDenied = 0xc0000022u;
            B2RNativeFileEntry* file = b2r_find_native_file(
                state, argument(0u));
            const uint32_t io_status = argument(4u);
            const uint32_t source = argument(5u);
            const uint32_t requested = argument(6u);
            const uint32_t offset_address = argument(7u);
            uint32_t written = 0u;
            std::FILE* host_file = file != nullptr
                ? reinterpret_cast<std::FILE*>(
                    static_cast<uintptr_t>(file->host_file))
                : nullptr;
            if (file == nullptr || host_file == nullptr) {
                eax = kStatusInvalidHandle;
            } else if ((file->flags & 8u) == 0u) {
                eax = kStatusAccessDenied;
            } else if (source == 0u && requested != 0u) {
                eax = kStatusInvalidParameter;
            } else {
                uint64_t offset = file->position;
                if (offset_address != 0u) {
                    offset = b2r_native_service_read_u64(
                        state, offset_address);
                }
                if (offset > static_cast<uint64_t>(LONG_MAX) ||
                    std::fseek(host_file, static_cast<long>(offset), SEEK_SET) != 0) {
                    eax = kStatusInvalidParameter;
                } else {
                    uint8_t buffer[4096] = {};
                    while (written < requested) {
                        uint32_t chunk = requested - written;
                        if (chunk > sizeof(buffer)) {
                            chunk = static_cast<uint32_t>(sizeof(buffer));
                        }
                        for (uint32_t index = 0u; index < chunk; ++index) {
                            buffer[index] = b2r_native_service_read_u8(
                                state, source + written + index);
                        }
                        const size_t host_written = std::fwrite(
                            buffer, 1u, chunk, host_file);
                        written += static_cast<uint32_t>(host_written);
                        if (host_written != chunk) { break; }
                    }
                    std::fflush(host_file);
                    file->position = offset + written;
                    if (file->position > file->size) {
                        file->size = file->position;
                    }
                    eax = written == requested && std::ferror(host_file) == 0
                        ? 0u : kStatusInvalidParameter;
                }
            }
            if (io_status != 0u) {
                b2r_native_service_write_u32(state, io_status, eax);
                b2r_native_service_write_u32(
                    state, io_status + 4u, eax == 0u ? written : 0u);
            }
        } else if (service->value == kXcShaInit) {
            const uint32_t context_address = argument(0u);
            if (context_address != 0u) {
                B2RSha1Context context = {};
                b2r_sha1_init(&context);
                b2r_store_xc_sha_context(state, context_address, &context);
            }
            // XcSHAInit is void; preserve EAX.
        } else if (service->value == kXcShaUpdate) {
            const uint32_t context_address = argument(0u);
            if (context_address != 0u) {
                B2RSha1Context context = {};
                b2r_load_xc_sha_context(state, context_address, &context);
                b2r_sha1_update_guest(
                    state, &context, argument(1u), argument(2u));
                b2r_store_xc_sha_context(state, context_address, &context);
            }
            // XcSHAUpdate is void; preserve EAX.
        } else if (service->value == kXcShaFinal) {
            const uint32_t context_address = argument(0u);
            const uint32_t digest_address = argument(1u);
            if (context_address != 0u && digest_address != 0u) {
                B2RSha1Context context = {};
                b2r_load_xc_sha_context(state, context_address, &context);
                uint8_t digest[20] = {};
                b2r_sha1_final(&context, digest);
                for (uint32_t index = 0u; index < 20u; ++index) {
                    b2r_native_service_write_u8(
                        state, digest_address + index, digest[index]);
                }
                for (uint32_t index = 0u;
                     index < sizeof(B2RSha1Context); ++index) {
                    b2r_native_service_write_u8(
                        state,
                        context_address + B2R_XC_SHA_CONTEXT_OFFSET + index,
                        0u);
                }
            }
            // XcSHAFinal is void; preserve EAX.
        } else if (service->value == kXcRc4Key) {
            const uint32_t context_address = argument(0u);
            const uint32_t key_size = argument(1u);
            const uint32_t key_address = argument(2u);
            if (context_address != 0u) {
                uint8_t permutation[256] = {};
                for (uint32_t index = 0u; index < 256u; ++index) {
                    permutation[index] = static_cast<uint8_t>(index);
                }
                uint32_t j = 0u;
                for (uint32_t index = 0u; index < 256u; ++index) {
                    const uint8_t key_byte = key_size != 0u
                        ? b2r_native_service_read_u8(
                            state, key_address + (index % key_size))
                        : 0u;
                    j = (j + permutation[index] + key_byte) & 0xffu;
                    std::swap(permutation[index], permutation[j]);
                }
                for (uint32_t index = 0u; index < 256u; ++index) {
                    b2r_native_service_write_u8(
                        state, context_address + index, permutation[index]);
                }
                b2r_native_service_write_u32(
                    state, context_address + 256u, 0u);
                b2r_native_service_write_u32(
                    state, context_address + 260u, 0u);
            }
            // XcRC4Key is void; preserve EAX.
        } else if (service->value == kXcRc4Crypt) {
            const uint32_t context_address = argument(0u);
            const uint32_t payload_size = argument(1u);
            const uint32_t payload_address = argument(2u);
            if (context_address != 0u) {
                uint8_t permutation[256] = {};
                for (uint32_t index = 0u; index < 256u; ++index) {
                    permutation[index] = b2r_native_service_read_u8(
                        state, context_address + index);
                }
                uint32_t i = b2r_native_service_read_u32(
                    state, context_address + 256u) & 0xffu;
                uint32_t j = b2r_native_service_read_u32(
                    state, context_address + 260u) & 0xffu;
                for (uint32_t index = 0u; index < payload_size; ++index) {
                    i = (i + 1u) & 0xffu;
                    j = (j + permutation[i]) & 0xffu;
                    std::swap(permutation[i], permutation[j]);
                    const uint8_t key_byte = permutation[
                        (permutation[i] + permutation[j]) & 0xffu];
                    const uint8_t value = b2r_native_service_read_u8(
                        state, payload_address + index);
                    b2r_native_service_write_u8(
                        state, payload_address + index, value ^ key_byte);
                }
                for (uint32_t index = 0u; index < 256u; ++index) {
                    b2r_native_service_write_u8(
                        state, context_address + index, permutation[index]);
                }
                b2r_native_service_write_u32(
                    state, context_address + 256u, i);
                b2r_native_service_write_u32(
                    state, context_address + 260u, j);
            }
            // XcRC4Crypt is void; preserve EAX.
        } else if (service->value == kXcHmac) {
            if (argument(6u) != 0u) {
                b2r_xc_hmac(
                    state,
                    argument(0u), argument(1u),
                    argument(2u), argument(3u),
                    argument(4u), argument(5u),
                    argument(6u));
            }
            // XcHMAC is void; preserve EAX.
        } else if (service->value == kRtlTimeFieldsToTime) {
            const uint32_t fields_address = argument(0u);
            const uint32_t time_address = argument(1u);
            eax = 0u;
            if (fields_address != 0u && time_address != 0u) {
                SYSTEMTIME system_time = {};
                system_time.wYear = b2r_native_service_read_u16(
                    state, fields_address);
                system_time.wMonth = b2r_native_service_read_u16(
                    state, fields_address + 2u);
                system_time.wDay = b2r_native_service_read_u16(
                    state, fields_address + 4u);
                system_time.wHour = b2r_native_service_read_u16(
                    state, fields_address + 6u);
                system_time.wMinute = b2r_native_service_read_u16(
                    state, fields_address + 8u);
                system_time.wSecond = b2r_native_service_read_u16(
                    state, fields_address + 10u);
                system_time.wMilliseconds = b2r_native_service_read_u16(
                    state, fields_address + 12u);
                FILETIME file_time = {};
                if (SystemTimeToFileTime(&system_time, &file_time) != FALSE) {
                    b2r_native_service_write_u64(
                        state,
                        time_address,
                        static_cast<uint64_t>(file_time.dwLowDateTime) |
                            (static_cast<uint64_t>(
                                file_time.dwHighDateTime) << 32u));
                    eax = 1u;
                }
            }
        } else if (service->value == kRtlTimeToTimeFields) {
            const uint32_t time_address = argument(0u);
            const uint32_t fields_address = argument(1u);
            if (time_address != 0u && fields_address != 0u) {
                const uint64_t value = b2r_native_service_read_u64(
                    state, time_address);
                FILETIME file_time = {};
                file_time.dwLowDateTime = static_cast<uint32_t>(value);
                file_time.dwHighDateTime = static_cast<uint32_t>(value >> 32u);
                SYSTEMTIME system_time = {};
                if (FileTimeToSystemTime(&file_time, &system_time) != FALSE) {
                    const uint16_t fields[8] = {
                        system_time.wYear,
                        system_time.wMonth,
                        system_time.wDay,
                        system_time.wHour,
                        system_time.wMinute,
                        system_time.wSecond,
                        system_time.wMilliseconds,
                        system_time.wDayOfWeek,
                    };
                    for (uint32_t index = 0u; index < 8u; ++index) {
                        b2r_native_service_write_u16(
                            state, fields_address + index * 2u, fields[index]);
                    }
                }
            }
            // RtlTimeToTimeFields is void; preserve EAX.
        } else {
            return false;
        }
        break;
    }
    default:
        return false;
    }
    if (state->service_trace_count < B2R_NATIVE_SERVICE_TRACE_CAPACITY) {
        const uint32_t index = state->service_trace_count++;
        state->service_trace_targets[index] = target;
        state->service_trace_kinds[index] = service->kind;
        state->service_trace_values[index] = service->value;
        state->service_trace_results[index] = eax;
        state->service_trace_return_addresses[index] = return_address;
    } else {
        ++state->service_trace_overflow_count;
    }
    state->last_service_target = target;
    state->last_service_kind = service->kind;
    state->last_service_value = service->value;
    state->last_service_result = eax;
    state->last_service_return_address = return_address;
    if (forward_original) {
        state->native_service_bypass_target = target;
        eip = target;
        *next_target = target;
        ++state->service_call_counts[service->kind];
        ++state->native_call_count;
        ++state->native_observer_count;
        return true;
    }
    esp += 4u + service->stack_cleanup_bytes;
    eip = return_address;
    *next_target = return_address;
    ++state->service_call_counts[service->kind];
    ++state->native_call_count;
    return true;
}

static uint32_t b2r_native_dispatch_context(
    void* context,
    uint32_t target,
    const uint32_t* keys,
    B2RNativeEntry const* entries,
    uint32_t mask,
    const uint32_t* host_call_keys,
    uint32_t host_call_mask,
    B2RNativeHostServiceState* native_host_services,
    B2RHostCall host_call,
    void* host_user,
    uint64_t* host_call_count,
    const bool* yield_requested,
    const uint32_t* fault_code,
    const uint64_t* steps,
    const uint64_t* step_budget,
    uint64_t* module_call_count,
    const uint32_t* module_exit_reason,
    bool profile_targets,
    uint64_t* exit_reason_counts,
    uint64_t* target_exit_reason_counts,
    uint64_t* target_call_counts,
    uint64_t* target_step_counts,
    uint32_t* touched_target_slots,
    uint32_t* touched_target_count,
    uint64_t* edge_keys,
    uint8_t* edge_reasons,
    uint64_t* edge_counts,
    uint32_t edge_mask,
    uint32_t* touched_edge_slots,
    uint32_t* touched_edge_count,
    uint64_t* edge_overflow_count,
    uint64_t* dispatcher_self_time_ns
) {
    uint64_t dispatcher_segment_started_ns =
        profile_targets ? b2r_dispatch_now_ns() : 0u;
    for (;;) {
        if (native_host_services != nullptr &&
            native_host_services->normal_runtime_enabled &&
            native_host_services->live_control_mapping != nullptr &&
            native_host_services->live_control_size >= 236u) {
            uint8_t* mapping = native_host_services->live_control_mapping;
            const B2RContext* active_context =
                static_cast<const B2RContext*>(context);
            const uint32_t handle =
                native_host_services->current_worker_handle;
            const uint64_t main_steps =
                native_host_services->scheduler_last_main_steps;
            const uint64_t active_steps = *steps;
            const uint32_t phase = 4u;
            std::memcpy(mapping + 160u, &handle, sizeof(handle));
            std::memcpy(mapping + 164u, &target, sizeof(target));
            std::memcpy(mapping + 168u, &main_steps, sizeof(main_steps));
            std::memcpy(mapping + 176u, &active_steps, sizeof(active_steps));
            std::memcpy(mapping + 184u, &phase, sizeof(phase));
            std::memcpy(mapping + 204u, &active_context->eax, sizeof(uint32_t));
            std::memcpy(mapping + 208u, &active_context->ecx, sizeof(uint32_t));
            std::memcpy(mapping + 212u, &active_context->edx, sizeof(uint32_t));
            std::memcpy(mapping + 216u, &active_context->ebx, sizeof(uint32_t));
            std::memcpy(mapping + 220u, &active_context->esp, sizeof(uint32_t));
            std::memcpy(mapping + 224u, &active_context->ebp, sizeof(uint32_t));
            std::memcpy(mapping + 228u, &active_context->esi, sizeof(uint32_t));
            std::memcpy(mapping + 232u, &active_context->edi, sizeof(uint32_t));
            MemoryBarrier();
        }
        uint32_t slot = (target * 2654435761u) & mask;
        uint32_t probe_count = 0u;
        while (entries[slot] != nullptr && keys[slot] != target &&
               probe_count <= mask) {
            slot = (slot + 1u) & mask;
            ++probe_count;
        }
        const B2RNativeEntry entry = probe_count <= mask
            ? entries[slot] : nullptr;
        bool compiled_host_call_target = false;
        if (entry != nullptr && host_call_keys != nullptr) {
            uint32_t host_slot = (target * 2654435761u) & host_call_mask;
            uint32_t host_probe_count = 0u;
            for (;;) {
                const uint32_t key = host_call_keys[host_slot];
                if (key == target) {
                    compiled_host_call_target = true;
                    break;
                }
                if (key == 0xffffffffu || host_probe_count++ >= host_call_mask) {
                    break;
                }
                host_slot = (host_slot + 1u) & host_call_mask;
            }
        }
        if (compiled_host_call_target && b2r_try_native_host_service(
                native_host_services, target, context, &target)) {
            static_cast<B2RContext*>(context)->callback_bypass_target = target;
            ++*host_call_count;
            if (*yield_requested || *fault_code ||
                (*step_budget != 0u && *steps >= *step_budget)) {
                return target;
            }
            continue;
        }
        if (entry == nullptr) {
            if (native_host_services != nullptr &&
                native_host_services->normal_runtime_enabled &&
                native_host_services->live_control_mapping != nullptr &&
                native_host_services->live_control_size >= 188u) {
                const uint32_t phase = 40u;
                std::memcpy(native_host_services->live_control_mapping + 184u,
                            &phase, sizeof(phase));
                MemoryBarrier();
            }
            if (b2r_try_native_host_service(
                    native_host_services, target, context, &target)) {
                ++*host_call_count;
                if (*yield_requested || *fault_code ||
                    (*step_budget != 0u && *steps >= *step_budget)) {
                    return target;
                }
                continue;
            }
            if (native_host_services != nullptr &&
                native_host_services->normal_runtime_enabled &&
                native_host_services->live_control_mapping != nullptr &&
                native_host_services->live_control_size >= 188u) {
                const uint32_t phase = 41u;
                std::memcpy(native_host_services->live_control_mapping + 184u,
                            &phase, sizeof(phase));
                MemoryBarrier();
            }
            if (host_call_keys != nullptr && host_call != nullptr) {
                uint32_t host_slot =
                    (target * 2654435761u) & host_call_mask;
                uint32_t host_probe_count = 0u;
                for (;;) {
                    const uint32_t key = host_call_keys[host_slot];
                    if (key == target) {
                        if (profile_targets) {
                            *dispatcher_self_time_ns +=
                                b2r_dispatch_now_ns() - dispatcher_segment_started_ns;
                        }
                        target = host_call(host_user, target, context);
                        ++*host_call_count;
                        if (profile_targets) {
                            dispatcher_segment_started_ns = b2r_dispatch_now_ns();
                        }
                        if (*yield_requested || *fault_code ||
                            (*step_budget != 0u && *steps >= *step_budget)) {
                            if (profile_targets) {
                                *dispatcher_self_time_ns +=
                                    b2r_dispatch_now_ns() - dispatcher_segment_started_ns;
                            }
                            return target;
                        }
                        break;
                    }
                    if (key == 0xffffffffu) {
                        if (profile_targets) {
                            *dispatcher_self_time_ns +=
                                b2r_dispatch_now_ns() - dispatcher_segment_started_ns;
                        }
                        return target;
                    }
                    if (host_probe_count++ >= host_call_mask) {
                        return target;
                    }
                    host_slot = (host_slot + 1u) & host_call_mask;
                }
                continue;
            }
            if (profile_targets) {
                *dispatcher_self_time_ns +=
                    b2r_dispatch_now_ns() - dispatcher_segment_started_ns;
            }
            return target;
        }
        const uint32_t entry_target = target;
        uint64_t steps_before = 0u;
        if (profile_targets) {
            steps_before = *steps;
            *dispatcher_self_time_ns +=
                b2r_dispatch_now_ns() - dispatcher_segment_started_ns;
        }
        if (native_host_services != nullptr &&
            native_host_services->normal_runtime_enabled &&
            native_host_services->live_control_mapping != nullptr &&
            native_host_services->live_control_size >= 188u) {
            const uint32_t phase = 5u;
            std::memcpy(native_host_services->live_control_mapping + 184u,
                        &phase, sizeof(phase));
            MemoryBarrier();
        }
        target = entry(context);
        ++*module_call_count;
        if (profile_targets) {
            dispatcher_segment_started_ns = b2r_dispatch_now_ns();
            uint32_t reason = *module_exit_reason;
            if (reason >= B2R_MODULE_EXIT_REASON_COUNT) {
                reason = 0u;
            }
            ++exit_reason_counts[reason];
            ++target_exit_reason_counts[
                slot * B2R_MODULE_EXIT_REASON_COUNT + reason
            ];
            if (target_call_counts[slot] == 0u) {
                touched_target_slots[(*touched_target_count)++] = slot;
            }
            ++target_call_counts[slot];
            target_step_counts[slot] += *steps - steps_before;
            b2r_record_dispatch_edge(
                entry_target,
                target,
                reason,
                edge_keys,
                edge_reasons,
                edge_counts,
                edge_mask,
                touched_edge_slots,
                touched_edge_count,
                edge_overflow_count
            );
        }
        if (*yield_requested || *fault_code ||
            (*step_budget != 0u && *steps >= *step_budget)) {
            if (profile_targets) {
                *dispatcher_self_time_ns +=
                    b2r_dispatch_now_ns() - dispatcher_segment_started_ns;
            }
            return target;
        }
    }
}

static void b2r_copy_shared_context_state(
    B2RContext* destination,
    const B2RContext* source
) {
    destination->user = source->user;
    destination->read_u32 = source->read_u32;
    destination->write_u32 = source->write_u32;
    destination->read_u8 = source->read_u8;
    destination->write_u8 = source->write_u8;
    destination->native_memory_user = source->native_memory_user;
    destination->native_read_u32 = source->native_read_u32;
    destination->native_write_u32 = source->native_write_u32;
    destination->native_read_u8 = source->native_read_u8;
    destination->native_write_u8 = source->native_write_u8;
    destination->call = source->call;
    destination->observe = source->observe;
    destination->read_pages = source->read_pages;
    destination->callback_pages = source->callback_pages;
    destination->callback_address_keys = source->callback_address_keys;
    destination->callback_address_mask = source->callback_address_mask;
    destination->write_callback_pages = source->write_callback_pages;
    destination->write_callback_address_keys =
        source->write_callback_address_keys;
    destination->write_callback_address_mask =
        source->write_callback_address_mask;
    destination->zero_read_callback_pages = source->zero_read_callback_pages;
    destination->zero_read_callback_address_keys =
        source->zero_read_callback_address_keys;
    destination->zero_read_callback_address_mask =
        source->zero_read_callback_address_mask;
    destination->cache_physical_aliases = source->cache_physical_aliases;
    destination->dirty_pages = source->dirty_pages;
    destination->dirty_page_generations = source->dirty_page_generations;
    destination->dirty_page_indices = source->dirty_page_indices;
    destination->dirty_page_min_offsets = source->dirty_page_min_offsets;
    destination->dirty_page_max_offsets = source->dirty_page_max_offsets;
    destination->dirty_page_count = source->dirty_page_count;
    destination->dirty_page_capacity = source->dirty_page_capacity;
    destination->observed_write_range_start =
        source->observed_write_range_start;
    destination->observed_write_range_end = source->observed_write_range_end;
    destination->observed_write_eips = source->observed_write_eips;
    destination->observed_write_source_addresses =
        source->observed_write_source_addresses;
    destination->observed_write_addresses = source->observed_write_addresses;
    destination->observed_write_values = source->observed_write_values;
    destination->observed_write_steps = source->observed_write_steps;
    destination->observed_write_sizes = source->observed_write_sizes;
    destination->observed_write_count = source->observed_write_count;
    destination->observed_write_capacity = source->observed_write_capacity;
    destination->observed_write_packet_header =
        source->observed_write_packet_header;
    destination->observed_write_packet_next_address =
        source->observed_write_packet_next_address;
    destination->observed_write_packet_yield_count =
        source->observed_write_packet_yield_count;
    destination->zero_read_callback_bypass_count =
        source->zero_read_callback_bypass_count;
    destination->direct_observed_write_count =
        source->direct_observed_write_count;
    destination->direct_observed_write_byte_count =
        source->direct_observed_write_byte_count;
    destination->direct_observed_payload = source->direct_observed_payload;
    destination->direct_observed_payload_size =
        source->direct_observed_payload_size;
    destination->direct_observed_payload_capacity =
        source->direct_observed_payload_capacity;
    destination->direct_observed_span_addresses =
        source->direct_observed_span_addresses;
    destination->direct_observed_span_payload_offsets =
        source->direct_observed_span_payload_offsets;
    destination->direct_observed_span_payload_sizes =
        source->direct_observed_span_payload_sizes;
    destination->direct_observed_span_write_counts =
        source->direct_observed_span_write_counts;
    destination->direct_observed_span_flags =
        source->direct_observed_span_flags;
    destination->direct_observed_span_count =
        source->direct_observed_span_count;
    destination->direct_observed_span_capacity =
        source->direct_observed_span_capacity;
    destination->direct_observed_span_sealed =
        source->direct_observed_span_sealed;
    destination->direct_observed_write_transport =
        source->direct_observed_write_transport;
    destination->native_fast_path_address_keys =
        source->native_fast_path_address_keys;
    destination->native_fast_path_call_counts =
        source->native_fast_path_call_counts;
    destination->native_fast_path_address_mask =
        source->native_fast_path_address_mask;
}

static B2RContext* b2r_initialize_worker_context(
    B2RNativeHostServiceState* state,
    B2RContext* main_context,
    B2RNativeWorkerLifecycleEntry* worker,
    uint32_t worker_index,
    B2RNativeWorkerExecutionSlot* slot
) {
    if (state == nullptr || main_context == nullptr || worker == nullptr ||
        slot == nullptr) {
        return nullptr;
    }
    B2RContext* context = new (std::nothrow) B2RContext(*main_context);
    if (context == nullptr) { return nullptr; }
    context->eax = 0u;
    context->ecx = 0u;
    context->edx = 0u;
    context->ebx = 0u;
    context->ebp = 0u;
    context->esi = 0u;
    context->edi = 0u;
    context->fs_base = 0x72000000u + (worker_index + 1u) * 0x10000u;
    context->cs_selector = 8u;
    context->gdtr_base = 0u;
    context->gdtr_limit = 0xffffu;
    context->timestamp_counter = 0u;
    context->mxcsr = 0x1f80u;
    context->fpu_control_word = 0x037fu;
    context->fpu_status_word = 0u;
    std::memset(context->fpu_stack, 0, sizeof(context->fpu_stack));
    context->fpu_depth = 0u;
    std::memset(context->xmm, 0, sizeof(context->xmm));
    std::memset(context->mmx, 0, sizeof(context->mmx));
    std::memset(&context->flags, 0, sizeof(context->flags));
    context->flags.interrupt_enabled = true;
    context->eip = worker->start_address;
    context->fault_code = 0u;
    context->fault_eip = 0u;
    context->module_exit_reason = 0u;
    context->steps = 0u;
    context->step_budget = 0u;
    context->yield_requested = false;
    const uint32_t stack_base =
        0x71000000u + worker_index * 0x10000u;
    const uint32_t return_sentinel =
        0xb2100000u + worker_index * 4u;
    context->esp = stack_base;
    if (!b2r_native_allocate_pages(state, stack_base, 12u) ||
        !b2r_native_allocate_pages(state, context->fs_base, 0x254u)) {
        delete context;
        return nullptr;
    }
    b2r_native_service_write_u32(state, stack_base, return_sentinel);
    b2r_native_service_write_u32(
        state, stack_base + 4u, worker->start_context1);
    b2r_native_service_write_u32(
        state, stack_base + 8u, worker->start_context2);
    b2r_native_service_write_u32(
        state, context->fs_base + 0x20u, context->fs_base);
    b2r_native_service_write_u32(
        state, context->fs_base + 0x250u, 0u);
    slot->handle = worker->handle;
    slot->return_sentinel = return_sentinel;
    slot->context = reinterpret_cast<uint64_t>(context);
    return context;
}

static void b2r_schedule_native_d3d_vblank(
    B2RNativeHostServiceState* state,
    uint32_t d3d_context
) {
    if (state == nullptr || d3d_context == 0u ||
        state->native_d3d_vblank_callback_pending ||
        state->native_d3d_vblank_callback_context != 0u) {
        return;
    }
    uint32_t callback_address = 0u;
    if (!b2r_native_service_try_read_u32(
            state, d3d_context + 0x1988u, &callback_address) ||
        callback_address == 0u || callback_address >= 0x80000000u ||
        callback_address == B2R_D3D_VBLANK_RETURN_SENTINEL) {
        state->native_d3d_vblank_next_deadline_qpc = 0;
        return;
    }
    LARGE_INTEGER frequency{};
    LARGE_INTEGER now{};
    if (!QueryPerformanceFrequency(&frequency) || frequency.QuadPart <= 0 ||
        !QueryPerformanceCounter(&now)) {
        return;
    }
    const int64_t interval = std::max<int64_t>(
        1, (frequency.QuadPart + 30) / 60);
    if (state->native_d3d_vblank_next_deadline_qpc == 0) {
        state->native_d3d_vblank_next_deadline_qpc =
            now.QuadPart + interval;
        return;
    }
    if (now.QuadPart < state->native_d3d_vblank_next_deadline_qpc) {
        return;
    }
    const uint64_t elapsed_vblanks = 1u + static_cast<uint64_t>(
        (now.QuadPart - state->native_d3d_vblank_next_deadline_qpc) /
        interval);
    state->native_d3d_vblank_next_deadline_qpc +=
        static_cast<int64_t>(elapsed_vblanks) * interval;
    state->native_d3d_vblank_sequence +=
        static_cast<uint32_t>(elapsed_vblanks);
    state->native_d3d_vblank_tick_count += elapsed_vblanks;
    state->native_d3d_vblank_callback_address = callback_address;
    b2r_native_service_write_u32(
        state, B2R_D3D_VBLANK_DATA_ADDRESS,
        state->native_d3d_vblank_sequence);
    b2r_native_service_write_u32(
        state, B2R_D3D_VBLANK_DATA_ADDRESS + 4u,
        static_cast<uint32_t>(state->live_flip_count));
    b2r_native_service_write_u32(
        state, B2R_D3D_VBLANK_DATA_ADDRESS + 8u, 0u);
    state->native_d3d_vblank_callback_pending = true;
    ++state->native_d3d_vblank_callback_schedule_count;
}

static B2RContext* b2r_initialize_d3d_vblank_callback_context(
    B2RNativeHostServiceState* state,
    B2RContext* main_context
) {
    if (state == nullptr || main_context == nullptr ||
        !state->native_d3d_vblank_callback_pending ||
        state->native_d3d_vblank_callback_address == 0u) {
        return nullptr;
    }
    B2RContext* context = new (std::nothrow) B2RContext(*main_context);
    if (context == nullptr) { return nullptr; }
    if (!b2r_native_allocate_pages(
            state, B2R_D3D_VBLANK_STACK_TOP - 0x1000u, 0x2000u) ||
        !b2r_native_allocate_pages(
            state, B2R_D3D_VBLANK_DATA_ADDRESS, 12u)) {
        delete context;
        return nullptr;
    }
    context->esp = B2R_D3D_VBLANK_STACK_TOP;
    context->eip = state->native_d3d_vblank_callback_address;
    context->fault_code = 0u;
    context->fault_eip = 0u;
    context->module_exit_reason = 0u;
    context->callback_bypass_target = 0u;
    context->steps = 0u;
    context->step_budget = 0u;
    context->yield_requested = false;
    b2r_native_service_write_u32(
        state, B2R_D3D_VBLANK_STACK_TOP,
        B2R_D3D_VBLANK_RETURN_SENTINEL);
    b2r_native_service_write_u32(
        state, B2R_D3D_VBLANK_STACK_TOP + 4u,
        B2R_D3D_VBLANK_DATA_ADDRESS);
    state->native_d3d_vblank_callback_context =
        reinterpret_cast<uint64_t>(context);
    state->native_d3d_vblank_callback_pending = false;
    return context;
}

static uint32_t b2r_service_native_normal_runtime(
    B2RNativeHostServiceState* state,
    B2RContext* context);
static void b2r_shutdown_native_normal_runtime(
    B2RNativeHostServiceState* state);

extern "C" __declspec(dllexport) uint32_t b2r_native_dispatch(
    void* context,
    uint32_t target,
    const uint32_t* keys,
    B2RNativeEntry const* entries,
    uint32_t mask,
    const uint32_t* host_call_keys,
    uint32_t host_call_mask,
    B2RNativeHostServiceState* native_host_services,
    B2RHostCall host_call,
    void* host_user,
    uint64_t* host_call_count,
    const bool* yield_requested,
    const uint32_t* fault_code,
    const uint64_t* steps,
    const uint64_t* step_budget,
    uint64_t* module_call_count,
    const uint32_t* module_exit_reason,
    bool profile_targets,
    uint64_t* exit_reason_counts,
    uint64_t* target_exit_reason_counts,
    uint64_t* target_call_counts,
    uint64_t* target_step_counts,
    uint32_t* touched_target_slots,
    uint32_t* touched_target_count,
    uint64_t* edge_keys,
    uint8_t* edge_reasons,
    uint64_t* edge_counts,
    uint32_t edge_mask,
    uint32_t* touched_edge_slots,
    uint32_t* touched_edge_count,
    uint64_t* edge_overflow_count,
    uint64_t* dispatcher_self_time_ns
) {
    if (native_host_services == nullptr ||
        !native_host_services->normal_runtime_enabled) {
        return b2r_native_dispatch_context(
            context, target, keys, entries, mask, host_call_keys,
            host_call_mask, native_host_services, host_call, host_user,
            host_call_count, yield_requested, fault_code, steps, step_budget,
            module_call_count, module_exit_reason, profile_targets,
            exit_reason_counts, target_exit_reason_counts,
            target_call_counts, target_step_counts, touched_target_slots,
            touched_target_count, edge_keys, edge_reasons, edge_counts,
            edge_mask, touched_edge_slots, touched_edge_count,
            edge_overflow_count, dispatcher_self_time_ns);
    }

    B2RContext* main_context = static_cast<B2RContext*>(context);
    const uint64_t external_step_budget = main_context->step_budget;
    const uint64_t quantum = native_host_services->scheduler_quantum != 0u
        ? native_host_services->scheduler_quantum : 100000u;
    const uint32_t primary_worker_handle =
        native_host_services->current_worker_handle;
    auto publish_scheduler_debug = [&](uint32_t handle, uint32_t eip,
                                       uint64_t worker_steps,
                                       uint32_t phase) {
        uint8_t* mapping = native_host_services->live_control_mapping;
        if (mapping == nullptr || native_host_services->live_control_size < 188u) {
            return;
        }
        std::memcpy(mapping + 160u, &handle, sizeof(handle));
        std::memcpy(mapping + 164u, &eip, sizeof(eip));
        std::memcpy(mapping + 168u, &main_context->steps,
                    sizeof(main_context->steps));
        std::memcpy(mapping + 176u, &worker_steps, sizeof(worker_steps));
        std::memcpy(mapping + 184u, &phase, sizeof(phase));
        MemoryBarrier();
    };
    for (;;) {
        publish_scheduler_debug(
            primary_worker_handle, target, 0u, 1u);
        const uint64_t quantum_budget = main_context->steps + quantum;
        main_context->step_budget = external_step_budget != 0u &&
                external_step_budget < quantum_budget
            ? external_step_budget : quantum_budget;
        target = b2r_native_dispatch_context(
            main_context, target, keys, entries, mask, host_call_keys,
            host_call_mask, native_host_services, host_call, host_user,
            host_call_count, &main_context->yield_requested,
            &main_context->fault_code, &main_context->steps,
            &main_context->step_budget, module_call_count,
            &main_context->module_exit_reason, profile_targets,
            exit_reason_counts, target_exit_reason_counts,
            target_call_counts, target_step_counts, touched_target_slots,
            touched_target_count, edge_keys, edge_reasons, edge_counts,
            edge_mask, touched_edge_slots, touched_edge_count,
            edge_overflow_count, dispatcher_self_time_ns);
        if (main_context->fault_code != 0u) { return target; }
        const bool reached_budget =
            main_context->steps >= main_context->step_budget;
        const bool requested_yield = main_context->yield_requested;
        if (!reached_budget && !requested_yield) {
            if (target != 0u) {
                native_host_services->normal_runtime_failure_code = 9u;
                native_host_services->normal_runtime_failure_target = target;
            }
            return target;
        }
        if (b2r_service_native_normal_runtime(
                native_host_services, main_context) != 0u) {
            main_context->step_budget = external_step_budget;
            return target;
        }
        B2RContext* vblank_context = reinterpret_cast<B2RContext*>(
            static_cast<uintptr_t>(
                native_host_services->native_d3d_vblank_callback_context));
        if (vblank_context == nullptr &&
            native_host_services->native_d3d_vblank_callback_pending) {
            vblank_context = b2r_initialize_d3d_vblank_callback_context(
                native_host_services, main_context);
            if (vblank_context == nullptr) {
                native_host_services->native_d3d_vblank_callback_pending = false;
                ++native_host_services->native_d3d_vblank_callback_failure_count;
                native_host_services->normal_runtime_failure_code = 10u;
                native_host_services->normal_runtime_failure_target =
                    native_host_services->native_d3d_vblank_callback_address;
                main_context->step_budget = external_step_budget;
                return target;
            }
        }
        if (vblank_context != nullptr) {
            b2r_copy_shared_context_state(vblank_context, main_context);
            vblank_context->yield_requested = false;
            vblank_context->step_budget = vblank_context->steps + quantum;
            native_host_services->dirty_page_count =
                &vblank_context->dirty_page_count;
            const uint64_t callback_steps_before = vblank_context->steps;
            const uint32_t callback_target = b2r_native_dispatch_context(
                vblank_context, vblank_context->eip, keys, entries, mask,
                host_call_keys, host_call_mask, native_host_services,
                nullptr, nullptr, host_call_count,
                &vblank_context->yield_requested,
                &vblank_context->fault_code, &vblank_context->steps,
                &vblank_context->step_budget, module_call_count,
                &vblank_context->module_exit_reason, profile_targets,
                exit_reason_counts, target_exit_reason_counts,
                target_call_counts, target_step_counts, touched_target_slots,
                touched_target_count, edge_keys, edge_reasons, edge_counts,
                edge_mask, touched_edge_slots, touched_edge_count,
                edge_overflow_count, dispatcher_self_time_ns);
            vblank_context->eip = callback_target;
            ++native_host_services->native_d3d_vblank_callback_run_count;
            native_host_services->native_d3d_vblank_callback_step_count +=
                vblank_context->steps - callback_steps_before;
            b2r_copy_shared_context_state(main_context, vblank_context);
            native_host_services->dirty_page_count =
                &main_context->dirty_page_count;
            const bool vblank_unhandled_target =
                callback_target != B2R_D3D_VBLANK_RETURN_SENTINEL &&
                !vblank_context->yield_requested &&
                vblank_context->steps < vblank_context->step_budget;
            if (vblank_context->fault_code != 0u ||
                vblank_unhandled_target) {
                ++native_host_services->native_d3d_vblank_callback_failure_count;
                native_host_services->normal_runtime_failure_code =
                    vblank_unhandled_target ? 9u : 10u;
                native_host_services->normal_runtime_failure_target =
                    callback_target;
                main_context->step_budget = external_step_budget;
                return callback_target;
            }
            if (callback_target == B2R_D3D_VBLANK_RETURN_SENTINEL) {
                delete vblank_context;
                native_host_services->native_d3d_vblank_callback_context = 0u;
                ++native_host_services->
                    native_d3d_vblank_callback_completion_count;
            } else {
                vblank_context->yield_requested = false;
            }
        }
        if (external_step_budget != 0u &&
            main_context->steps >= external_step_budget) {
            main_context->step_budget = external_step_budget;
            return target;
        }
        main_context->yield_requested = false;
        ++native_host_services->scheduler_service_count;
        native_host_services->scheduler_last_main_steps = main_context->steps;

        B2RNativeWorkerLifecycleState* lifecycle =
            native_host_services->worker_lifecycle;
        if (lifecycle == nullptr) { continue; }
        for (uint32_t index = 0u; index < lifecycle->entry_count; ++index) {
            B2RNativeWorkerLifecycleEntry* worker = &lifecycle->entries[index];
            if (worker->handle == primary_worker_handle) { continue; }
            if (worker->status == B2R_WORKER_WAITING &&
                worker->wait_handle == 0u) {
                b2r_set_worker_status(lifecycle, worker, B2R_WORKER_READY);
            }
            if (worker->status != B2R_WORKER_READY &&
                worker->status != B2R_WORKER_RUNNING) {
                continue;
            }
            B2RNativeWorkerExecutionSlot* slot =
                &native_host_services->worker_execution[index];
            B2RContext* worker_context = reinterpret_cast<B2RContext*>(
                static_cast<uintptr_t>(slot->context));
            if (worker_context == nullptr || slot->handle != worker->handle) {
                if (worker_context != nullptr) { delete worker_context; }
                worker_context = b2r_initialize_worker_context(
                    native_host_services, main_context, worker, index + 1u, slot);
                if (worker_context == nullptr) {
                    b2r_set_worker_status(
                        lifecycle, worker, B2R_WORKER_FAILED);
                    ++native_host_services->scheduler_worker_failure_count;
                    continue;
                }
            }
            b2r_set_worker_status(lifecycle, worker, B2R_WORKER_RUNNING);
            b2r_copy_shared_context_state(worker_context, main_context);
            worker_context->yield_requested = false;
            worker_context->step_budget = worker_context->steps + quantum;
            native_host_services->current_worker_handle = worker->handle;
            native_host_services->dirty_page_count =
                &worker_context->dirty_page_count;
            const uint64_t worker_steps_before = worker_context->steps;
            publish_scheduler_debug(
                worker->handle, worker_context->eip,
                worker_context->steps, 2u);
            const uint32_t worker_target = b2r_native_dispatch_context(
                worker_context, worker_context->eip, keys, entries, mask,
                nullptr, 0u, native_host_services, nullptr, nullptr,
                host_call_count, &worker_context->yield_requested,
                &worker_context->fault_code, &worker_context->steps,
                &worker_context->step_budget, module_call_count,
                &worker_context->module_exit_reason, profile_targets,
                exit_reason_counts, target_exit_reason_counts,
                target_call_counts, target_step_counts, touched_target_slots,
                touched_target_count, edge_keys, edge_reasons, edge_counts,
                edge_mask, touched_edge_slots, touched_edge_count,
                edge_overflow_count, dispatcher_self_time_ns);
            worker_context->eip = worker_target;
            const uint64_t worker_steps =
                worker_context->steps - worker_steps_before;
            publish_scheduler_debug(
                worker->handle, worker_target,
                worker_context->steps, 3u);
            ++slot->run_count;
            slot->step_count += worker_steps;
            slot->last_target = worker_target;
            slot->last_reason = worker_context->module_exit_reason;
            ++native_host_services->scheduler_worker_run_count;
            native_host_services->scheduler_worker_step_count += worker_steps;
            b2r_copy_shared_context_state(main_context, worker_context);
            native_host_services->dirty_page_count =
                &main_context->dirty_page_count;
            if (worker_context->fault_code != 0u) {
                b2r_set_worker_status(lifecycle, worker, B2R_WORKER_FAILED);
                ++native_host_services->scheduler_worker_failure_count;
            } else if (worker_target == slot->return_sentinel ||
                       worker->status == B2R_WORKER_COMPLETED) {
                b2r_set_worker_status(
                    lifecycle, worker, B2R_WORKER_COMPLETED);
                ++native_host_services->scheduler_worker_completion_count;
            } else if (!worker_context->yield_requested &&
                       worker_context->steps < worker_context->step_budget) {
                b2r_set_worker_status(lifecycle, worker, B2R_WORKER_FAILED);
                ++native_host_services->scheduler_worker_failure_count;
                native_host_services->normal_runtime_failure_code = 9u;
                native_host_services->normal_runtime_failure_target =
                    worker_target;
                native_host_services->current_worker_handle =
                    primary_worker_handle;
                native_host_services->dirty_page_count =
                    &main_context->dirty_page_count;
                main_context->step_budget = external_step_budget;
                return worker_target;
            } else if (worker->status == B2R_WORKER_WAITING) {
                ++native_host_services->scheduler_worker_wait_count;
            }
            worker_context->yield_requested = false;
        }
        native_host_services->current_worker_handle = primary_worker_handle;
        native_host_services->dirty_page_count = &main_context->dirty_page_count;
    }
}

extern "C" __declspec(dllexport) void b2r_native_runtime_shutdown(
    B2RNativeHostServiceState* state
) {
    if (state == nullptr) { return; }
    for (uint32_t index = 0u;
         index < B2R_WORKER_LIFECYCLE_CAPACITY; ++index) {
        B2RNativeWorkerExecutionSlot* slot = &state->worker_execution[index];
        B2RContext* context = reinterpret_cast<B2RContext*>(
            static_cast<uintptr_t>(slot->context));
        delete context;
        slot->context = 0u;
    }
    b2r_shutdown_native_normal_runtime(state);
}

struct B2RCooperativeSchedulerState {
    uint64_t last_steps;
    uint64_t pending_steps;
    uint64_t last_serviced_flip;
};

extern "C" __declspec(dllexport) void b2r_update_cooperative_scheduler(
    B2RCooperativeSchedulerState* state,
    uint64_t steps,
    uint64_t completed_flips,
    uint64_t instruction_quantum,
    bool force_instruction_tick,
    uint64_t* instruction_ticks,
    uint64_t* video_ticks
) {
    *instruction_ticks = 0u;
    *video_ticks = 0u;
    if (state == nullptr || instruction_quantum == 0u) {
        return;
    }
    state->pending_steps +=
        steps >= state->last_steps ? steps - state->last_steps : steps;
    state->last_steps = steps;
    if (completed_flips != 0u) {
        if (completed_flips > state->last_serviced_flip) {
            *video_ticks = completed_flips - state->last_serviced_flip;
            state->last_serviced_flip = completed_flips;
        }
        state->pending_steps = 0u;
        if (*video_ticks == 0u && force_instruction_tick) {
            *instruction_ticks = 1u;
        }
        return;
    }
    *instruction_ticks = state->pending_steps / instruction_quantum;
    state->pending_steps %= instruction_quantum;
    if (*instruction_ticks == 0u && force_instruction_tick) {
        *instruction_ticks = 1u;
    }
}

extern "C" __declspec(dllexport) void b2r_sync_worker_lifecycle(
    B2RNativeWorkerLifecycleState* state,
    const B2RNativeWorkerLifecycleEntry* workers,
    uint32_t worker_count,
    uint32_t primary_handle
) {
    if (state == nullptr || workers == nullptr) { return; }
    for (uint32_t index = 0u; index < worker_count; ++index) {
        const B2RNativeWorkerLifecycleEntry& incoming = workers[index];
        if (incoming.handle == 0u || incoming.handle == primary_handle ||
            incoming.start_address == 0u) {
            continue;
        }
        B2RNativeWorkerLifecycleEntry* worker = b2r_find_worker(
            state, incoming.handle);
        if (worker == nullptr) {
            b2r_upsert_worker(
                state,
                incoming.handle,
                incoming.start_address,
                incoming.start_context1,
                incoming.start_context2,
                incoming.status == B2R_WORKER_SUSPENDED
                    ? B2R_WORKER_SUSPENDED : B2R_WORKER_READY
            );
            continue;
        }
        worker->start_address = incoming.start_address;
        worker->start_context1 = incoming.start_context1;
        worker->start_context2 = incoming.start_context2;
        if (worker->status == B2R_WORKER_COMPLETED ||
            worker->status == B2R_WORKER_BLOCKED ||
            worker->status == B2R_WORKER_FAILED) {
            continue;
        }
        if (incoming.status == B2R_WORKER_SUSPENDED) {
            b2r_set_worker_status(state, worker, B2R_WORKER_SUSPENDED);
        } else if (worker->status == B2R_WORKER_SUSPENDED) {
            b2r_set_worker_status(state, worker, B2R_WORKER_READY);
        }
    }
}

extern "C" __declspec(dllexport) bool b2r_set_worker_lifecycle_status(
    B2RNativeWorkerLifecycleState* state,
    uint32_t handle,
    uint32_t status
) {
    if (status < B2R_WORKER_READY || status > B2R_WORKER_SUSPENDED) {
        return false;
    }
    return b2r_set_worker_status(
        state, b2r_find_worker(state, handle), status);
}

extern "C" __declspec(dllexport) uint32_t b2r_select_runnable_workers(
    B2RNativeWorkerLifecycleState* state,
    uint32_t* handles,
    uint32_t handle_capacity
) {
    if (state == nullptr || handles == nullptr || handle_capacity == 0u ||
        state->entry_count == 0u) {
        return 0u;
    }
    const uint32_t start = state->selection_cursor % state->entry_count;
    uint32_t selected = 0u;
    for (uint32_t offset = 0u;
         offset < state->entry_count && selected < handle_capacity;
         ++offset) {
        const uint32_t index = (start + offset) % state->entry_count;
        B2RNativeWorkerLifecycleEntry* worker = &state->entries[index];
        if (worker->status != B2R_WORKER_READY &&
            worker->status != B2R_WORKER_RUNNING) {
            continue;
        }
        if (worker->status == B2R_WORKER_READY) {
            b2r_set_worker_status(state, worker, B2R_WORKER_RUNNING);
        }
        handles[selected++] = worker->handle;
    }
    state->selection_cursor = (start + 1u) % state->entry_count;
    return selected;
}

extern "C" __declspec(dllexport) uint32_t b2r_pack_observed_write_records(
    uint8_t* output,
    uint32_t output_capacity,
    uint8_t* texture_payload,
    uint32_t* texture_runs,
    uint32_t* texture_run_count,
    const uint32_t* addresses,
    const uint64_t* values,
    const uint8_t* sizes,
    uint32_t count,
    uint32_t range_start,
    uint32_t range_end,
    uint32_t normalized_base
) {
    if (output == nullptr || texture_payload == nullptr ||
        texture_runs == nullptr || texture_run_count == nullptr ||
        addresses == nullptr || values == nullptr || sizes == nullptr ||
        count > output_capacity || range_end < range_start) {
        return 0u;
    }
    *texture_run_count = 0u;
    bool contiguous_u32 = count != 0u;
    uint32_t expected_address = count != 0u ? addresses[0] : 0u;
    uint32_t payload_size = 0u;
    uint32_t run_count = 0u;
    uint32_t run_end = 0u;
    for (uint32_t index = 0u; index < count; ++index) {
        const uint32_t address = addresses[index];
        const uint32_t size = sizes[index];
        if ((size != 1u && size != 4u && size != 8u) ||
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
        if (index == 0u || address != run_end) {
            uint32_t* run = texture_runs + static_cast<size_t>(run_count) * 4u;
            run[0] = normalized_address;
            run[1] = payload_size;
            run[2] = 0u;
            run[3] = index;
            ++run_count;
        }
        std::memcpy(texture_payload + payload_size, &values[index], size);
        payload_size += size;
        texture_runs[static_cast<size_t>(run_count - 1u) * 4u + 2u] += size;
        run_end = address + size;
        std::memcpy(record + 4u, &normalized_address, sizeof(normalized_address));
        std::memset(record + 8u, 0, 8u);
        std::memcpy(record + 8u, &values[index], size);
    }
    *texture_run_count = run_count;
    return 1u | (contiguous_u32 ? 2u : 0u);
}

static bool b2r_is_tracked_resource_method(uint32_t method) {
    if (method == 0x012Cu || method == 0x17FCu ||
        method == 0x1800u || method == 0x1808u) {
        return true;
    }
    if ((method >= 0x1720u && method <= 0x179Cu) &&
        ((method - 0x1720u) & 3u) == 0u) {
        return true;
    }
    if (method >= 0x1B00u && method < 0x1C00u) {
        const uint32_t stage_register = (method - 0x1B00u) & 0x3Fu;
        return stage_register == 0u || stage_register == 4u ||
            stage_register == 0x1Cu;
    }
    return false;
}

extern "C" __declspec(dllexport) bool b2r_scan_resource_methods(
    const uint8_t* payload,
    uint32_t payload_size,
    uint32_t* output_methods,
    uint32_t* output_values,
    uint32_t output_capacity,
    uint32_t* output_count,
    uint64_t* data_word_count,
    uint64_t* tracked_word_count,
    uint64_t* skipped_word_count,
    uint64_t* aggregated_index_word_count
) {
    if (payload == nullptr || output_methods == nullptr ||
        output_values == nullptr || output_count == nullptr ||
        data_word_count == nullptr || tracked_word_count == nullptr ||
        skipped_word_count == nullptr ||
        aggregated_index_word_count == nullptr || (payload_size & 3u) != 0u) {
        return false;
    }
    *output_count = 0u;
    *data_word_count = 0u;
    *tracked_word_count = 0u;
    *skipped_word_count = 0u;
    *aggregated_index_word_count = 0u;
    const uint32_t word_count = payload_size / 4u;
    auto read_word = [&](uint32_t index) {
        uint32_t value = 0u;
        std::memcpy(&value, payload + static_cast<size_t>(index) * 4u,
                    sizeof(value));
        return value;
    };
    auto append = [&](uint32_t method, uint32_t value) {
        if (*output_count >= output_capacity) {
            return false;
        }
        output_methods[*output_count] = method;
        output_values[*output_count] = value;
        ++*output_count;
        return true;
    };
    uint32_t index = 0u;
    while (index < word_count) {
        const uint32_t command = read_word(index);
        bool non_increasing = false;
        uint32_t header_words = 1u;
        uint32_t count = 0u;
        if ((command & 0xE0030003u) == 0u) {
            count = (command >> 18u) & 0x7FFu;
        } else if ((command & 0xE0030003u) == 0x40000000u) {
            count = (command >> 18u) & 0x7FFu;
            non_increasing = true;
        } else if ((command & 0xFFFF0003u) == 0x00030000u &&
                   index + 1u < word_count) {
            count = read_word(index + 1u) & 0x00FFFFFFu;
            non_increasing = true;
            header_words = 2u;
        } else {
            ++index;
            continue;
        }
        const uint32_t first_method = ((command >> 2u) & 0x7FFu) * 4u;
        const uint32_t data_start = std::min(index + header_words, word_count);
        const uint32_t available = word_count - data_start;
        const uint32_t packet_word_count = std::min(count, available);
        const uint32_t data_end = data_start + packet_word_count;
        *data_word_count += packet_word_count;
        uint64_t packet_tracked_count = 0u;
        if (non_increasing &&
            (first_method == 0x1800u || first_method == 0x1808u)) {
            uint32_t maximum = 0u;
            if (first_method == 0x1800u) {
                for (uint32_t data_index = data_start;
                     data_index < data_end; ++data_index) {
                    const uint32_t value = read_word(data_index);
                    maximum = std::max(maximum, value & 0xFFFFu);
                    maximum = std::max(maximum, value >> 16u);
                }
            } else {
                for (uint32_t data_index = data_start;
                     data_index < data_end; ++data_index) {
                    maximum = std::max(maximum, read_word(data_index));
                }
            }
            if (packet_word_count != 0u && !append(0x1808u, maximum)) {
                return false;
            }
            packet_tracked_count = packet_word_count;
            *aggregated_index_word_count += packet_word_count;
        } else if (non_increasing) {
            if (b2r_is_tracked_resource_method(first_method)) {
                for (uint32_t data_index = data_start;
                     data_index < data_end; ++data_index) {
                    if (!append(first_method, read_word(data_index))) {
                        return false;
                    }
                }
                packet_tracked_count = packet_word_count;
            }
        } else {
            for (uint32_t data_index = data_start;
                 data_index < data_end; ++data_index) {
                const uint32_t method = first_method +
                    (data_index - data_start) * 4u;
                if (!b2r_is_tracked_resource_method(method)) {
                    continue;
                }
                if (!append(method, read_word(data_index))) {
                    return false;
                }
                ++packet_tracked_count;
            }
        }
        *tracked_word_count += packet_tracked_count;
        *skipped_word_count += packet_word_count - packet_tracked_count;
        index = data_end;
    }
    return true;
}

struct B2RResourceSpanScanState {
    uint32_t next_address;
    uint32_t partial_word;
    uint32_t partial_size;
    uint32_t run_active;
    uint32_t pending_long_header;
    uint32_t pending_packet;
    uint32_t non_increasing;
    uint32_t first_method;
    uint32_t remaining;
    uint32_t method_index;
    uint32_t index_packet;
    uint32_t index_maximum;
    uint32_t index_has_data;
    uint32_t texture_offsets[4];
    uint32_t texture_formats[4];
    uint32_t texture_image_rects[4];
    uint32_t texture_offset_mask;
    uint32_t texture_format_mask;
    uint32_t texture_image_rect_mask;
    uint32_t vertex_array_offsets[16];
    uint32_t vertex_array_formats[16];
    uint32_t vertex_array_offset_mask;
    uint32_t vertex_array_format_mask;
    uint32_t active_vertex_primitive;
    uint32_t active_vertex_max_index;
    uint32_t active_vertex_has_index;
    uint32_t frame_binding_count;
    uint32_t frame_binding_stages[8192];
    uint32_t frame_binding_addresses[8192];
    uint32_t frame_binding_formats[8192];
    uint32_t frame_binding_image_rects[8192];
    uint32_t frame_range_count;
    uint32_t frame_range_starts[1024];
    uint32_t frame_range_ends[1024];
};

static constexpr uint32_t B2R_RESOURCE_FRAME_BINDING_CAPACITY = 8192u;
static constexpr uint32_t B2R_RESOURCE_FRAME_RANGE_CAPACITY = 1024u;
static constexpr uint32_t B2R_RESOURCE_BINDING_ADDRESS_EVENT_BASE = 0xFFF00000u;
static constexpr uint32_t B2R_RESOURCE_BINDING_FORMAT_EVENT_BASE = 0xFFF00010u;
static constexpr uint32_t B2R_RESOURCE_BINDING_RECT_EVENT_BASE = 0xFFF00020u;
static constexpr uint32_t B2R_RESOURCE_RANGE_START_EVENT = 0xFFF00100u;
static constexpr uint32_t B2R_RESOURCE_RANGE_END_EVENT = 0xFFF00104u;

extern "C" __declspec(dllexport) bool b2r_scan_resource_method_spans(
    B2RResourceSpanScanState* state,
    const uint8_t* payload,
    uint32_t payload_size,
    const uint32_t* addresses,
    const uint32_t* payload_offsets,
    const uint32_t* payload_sizes,
    const uint8_t* flags,
    uint32_t span_count,
    bool finish,
    uint32_t* output_methods,
    uint32_t* output_values,
    uint32_t* output_positions,
    uint32_t output_capacity,
    uint32_t* output_count,
    uint64_t* data_word_count,
    uint64_t* tracked_word_count,
    uint64_t* skipped_word_count,
    uint64_t* aggregated_index_word_count
) {
    if (state == nullptr || output_methods == nullptr ||
        output_values == nullptr || output_positions == nullptr ||
        output_count == nullptr || data_word_count == nullptr ||
        tracked_word_count == nullptr || skipped_word_count == nullptr ||
        aggregated_index_word_count == nullptr ||
        (payload_size != 0u && payload == nullptr) ||
        (span_count != 0u && (addresses == nullptr ||
                             payload_offsets == nullptr ||
                             payload_sizes == nullptr || flags == nullptr))) {
        return false;
    }
    *output_count = 0u;
    *data_word_count = 0u;
    *tracked_word_count = 0u;
    *skipped_word_count = 0u;
    *aggregated_index_word_count = 0u;

    auto append = [&](uint32_t method, uint32_t value, uint32_t position) {
        if (*output_count >= output_capacity) {
            return false;
        }
        output_methods[*output_count] = method;
        output_values[*output_count] = value;
        output_positions[*output_count] = position;
        ++*output_count;
        return true;
    };
    auto merge_frame_range = [&](uint32_t start, uint32_t end) {
        if (start == 0u || end <= start || end - start > 16u * 1024u * 1024u) {
            return true;
        }
        for (uint32_t index = 0u; index < state->frame_range_count;) {
            const uint32_t existing_start = state->frame_range_starts[index];
            const uint32_t existing_end = state->frame_range_ends[index];
            if (end < existing_start || existing_end < start) {
                ++index;
                continue;
            }
            start = std::min(start, existing_start);
            end = std::max(end, existing_end);
            --state->frame_range_count;
            state->frame_range_starts[index] =
                state->frame_range_starts[state->frame_range_count];
            state->frame_range_ends[index] =
                state->frame_range_ends[state->frame_range_count];
        }
        if (state->frame_range_count >= B2R_RESOURCE_FRAME_RANGE_CAPACITY) {
            return false;
        }
        const uint32_t index = state->frame_range_count++;
        state->frame_range_starts[index] = start;
        state->frame_range_ends[index] = end;
        return true;
    };
    auto vertex_element_size = [](uint32_t format) {
        const uint32_t type = format & 0xFu;
        const uint32_t components = (format >> 4u) & 0xFu;
        if (components == 0u) {
            return 0u;
        }
        if (type == 2u) {
            return components * 4u;
        }
        if (type == 0u || type == 4u || type == 6u) {
            return 4u;
        }
        return components * 2u;
    };
    auto retain_active_vertex_ranges = [&]() {
        if (state->active_vertex_has_index == 0u) {
            return true;
        }
        const uint32_t populated_mask = state->vertex_array_offset_mask &
            state->vertex_array_format_mask;
        for (uint32_t slot = 0u; slot < 16u; ++slot) {
            if ((populated_mask & (1u << slot)) == 0u) {
                continue;
            }
            const uint32_t address = state->vertex_array_offsets[slot];
            const uint32_t format = state->vertex_array_formats[slot];
            const uint32_t stride = format >> 8u;
            const uint32_t element_size = vertex_element_size(format);
            const uint64_t end = static_cast<uint64_t>(address) +
                static_cast<uint64_t>(state->active_vertex_max_index) * stride +
                element_size;
            if (address == 0u || stride == 0u || element_size == 0u ||
                end > 0xFFFFFFFFull ||
                !merge_frame_range(address, static_cast<uint32_t>(end))) {
                if (address != 0u && stride != 0u && element_size != 0u) {
                    return false;
                }
            }
        }
        state->active_vertex_max_index = 0u;
        state->active_vertex_has_index = 0u;
        return true;
    };
    auto retain_texture_binding = [&](uint32_t stage) {
        const uint32_t stage_mask = 1u << stage;
        if ((state->texture_offset_mask & stage_mask) == 0u ||
            (state->texture_format_mask & stage_mask) == 0u) {
            return true;
        }
        const uint32_t address = state->texture_offsets[stage];
        const uint32_t format = state->texture_formats[stage];
        const uint32_t image_rect = state->texture_image_rects[stage];
        for (uint32_t index = 0u; index < state->frame_binding_count; ++index) {
            if (state->frame_binding_stages[index] == stage &&
                state->frame_binding_addresses[index] == address &&
                state->frame_binding_formats[index] == format &&
                state->frame_binding_image_rects[index] == image_rect) {
                return true;
            }
        }
        if (state->frame_binding_count >= B2R_RESOURCE_FRAME_BINDING_CAPACITY) {
            return false;
        }
        const uint32_t index = state->frame_binding_count++;
        state->frame_binding_stages[index] = stage;
        state->frame_binding_addresses[index] = address;
        state->frame_binding_formats[index] = format;
        state->frame_binding_image_rects[index] = image_rect;
        return true;
    };
    auto apply_resource_method = [&](uint32_t method, uint32_t value) {
        if (method == 0x17FCu) {
            if (!retain_active_vertex_ranges()) {
                return false;
            }
            state->active_vertex_primitive = value;
            if (value != 0u) {
                for (uint32_t stage = 0u; stage < 4u; ++stage) {
                    if (!retain_texture_binding(stage)) {
                        return false;
                    }
                }
            }
            return true;
        }
        if ((method == 0x1800u || method == 0x1808u) &&
            state->active_vertex_primitive != 0u) {
            if (method == 0x1800u) {
                value = std::max(value & 0xFFFFu, value >> 16u);
            }
            state->active_vertex_max_index = state->active_vertex_has_index != 0u
                ? std::max(state->active_vertex_max_index, value)
                : value;
            state->active_vertex_has_index = 1u;
            return true;
        }
        if (method >= 0x1720u && method <= 0x175Cu &&
            ((method - 0x1720u) & 3u) == 0u) {
            const uint32_t slot = (method - 0x1720u) / 4u;
            state->vertex_array_offsets[slot] = value;
            state->vertex_array_offset_mask |= 1u << slot;
            return true;
        }
        if (method >= 0x1760u && method <= 0x179Cu &&
            ((method - 0x1760u) & 3u) == 0u) {
            const uint32_t slot = (method - 0x1760u) / 4u;
            state->vertex_array_formats[slot] = value;
            state->vertex_array_format_mask |= 1u << slot;
            return true;
        }
        if (method >= 0x1B00u && method < 0x1C00u) {
            const uint32_t stage = (method - 0x1B00u) / 0x40u;
            const uint32_t stage_register = (method - 0x1B00u) & 0x3Fu;
            const uint32_t stage_mask = 1u << stage;
            if (stage_register == 0u) {
                state->texture_offsets[stage] = value;
                state->texture_offset_mask |= stage_mask;
            } else if (stage_register == 4u) {
                state->texture_formats[stage] = value;
                state->texture_format_mask |= stage_mask;
            } else if (stage_register == 0x1Cu) {
                state->texture_image_rects[stage] = value;
                state->texture_image_rect_mask |= stage_mask;
            }
        }
        return true;
    };
    auto flush_frame_state = [&](uint32_t position) {
        for (uint32_t index = 0u; index < state->frame_binding_count; ++index) {
            const uint32_t stage = state->frame_binding_stages[index];
            if (!append(B2R_RESOURCE_BINDING_ADDRESS_EVENT_BASE + stage,
                        state->frame_binding_addresses[index], position) ||
                !append(B2R_RESOURCE_BINDING_FORMAT_EVENT_BASE + stage,
                        state->frame_binding_formats[index], position) ||
                !append(B2R_RESOURCE_BINDING_RECT_EVENT_BASE + stage,
                        state->frame_binding_image_rects[index], position)) {
                return false;
            }
        }
        for (uint32_t index = 0u; index < state->frame_range_count; ++index) {
            if (!append(B2R_RESOURCE_RANGE_START_EVENT,
                        state->frame_range_starts[index], position) ||
                !append(B2R_RESOURCE_RANGE_END_EVENT,
                        state->frame_range_ends[index], position)) {
                return false;
            }
        }
        state->frame_binding_count = 0u;
        state->frame_range_count = 0u;
        return true;
    };
    auto finish_index_packet = [&](uint32_t position) {
        if (state->index_packet != 0u && state->index_has_data != 0u) {
            if (!apply_resource_method(0x1808u, state->index_maximum)) {
                return false;
            }
        }
        state->index_packet = 0u;
        state->index_maximum = 0u;
        state->index_has_data = 0u;
        return true;
    };
    auto reset_run = [&](uint32_t position) {
        if (!finish_index_packet(position)) {
            return false;
        }
        state->next_address = 0u;
        state->partial_word = 0u;
        state->partial_size = 0u;
        state->run_active = 0u;
        state->pending_long_header = 0u;
        state->pending_packet = 0u;
        state->non_increasing = 0u;
        state->first_method = 0u;
        state->remaining = 0u;
        state->method_index = 0u;
        return true;
    };
    auto begin_packet = [&](uint32_t count, bool non_increasing) {
        state->remaining = count;
        state->method_index = 0u;
        state->non_increasing = non_increasing ? 1u : 0u;
        state->pending_packet = count != 0u ? 1u : 0u;
        state->index_packet = (
            non_increasing &&
            (state->first_method == 0x1800u ||
             state->first_method == 0x1808u)
        ) ? 1u : 0u;
        state->index_maximum = 0u;
        state->index_has_data = 0u;
    };
    auto consume_word = [&](uint32_t word, uint32_t position) {
        if (state->pending_long_header != 0u) {
            state->pending_long_header = 0u;
            begin_packet(word & 0x00FFFFFFu, true);
            return true;
        }
        if (state->pending_packet != 0u) {
            ++*data_word_count;
            const uint32_t method = state->non_increasing != 0u
                ? state->first_method
                : state->first_method + state->method_index * 4u;
            if (state->index_packet != 0u) {
                if (state->first_method == 0x1800u) {
                    state->index_maximum = std::max(
                        state->index_maximum,
                        word & 0xFFFFu
                    );
                    state->index_maximum = std::max(
                        state->index_maximum,
                        word >> 16u
                    );
                } else {
                    state->index_maximum = std::max(
                        state->index_maximum,
                        word
                    );
                }
                state->index_has_data = 1u;
                ++*tracked_word_count;
                ++*aggregated_index_word_count;
            } else if (b2r_is_tracked_resource_method(method)) {
                ++*tracked_word_count;
                // Span flags carry the exact flip boundary. Suppressing the
                // packet event here avoids a duplicate flip and also handles
                // a packet split at the push-buffer ring boundary.
                if (method != 0x012Cu && !apply_resource_method(method, word)) {
                    return false;
                }
            } else {
                ++*skipped_word_count;
            }
            ++state->method_index;
            --state->remaining;
            if (state->remaining == 0u) {
                state->pending_packet = 0u;
                if (!finish_index_packet(position)) {
                    return false;
                }
            }
            return true;
        }

        bool non_increasing = false;
        uint32_t count = 0u;
        if ((word & 0xE0030003u) == 0u) {
            count = (word >> 18u) & 0x7FFu;
        } else if ((word & 0xE0030003u) == 0x40000000u) {
            count = (word >> 18u) & 0x7FFu;
            non_increasing = true;
        } else if ((word & 0xFFFF0003u) == 0x00030000u) {
            state->first_method = ((word >> 2u) & 0x7FFu) * 4u;
            state->pending_long_header = 1u;
            return true;
        } else {
            return true;
        }
        state->first_method = ((word >> 2u) & 0x7FFu) * 4u;
        begin_packet(count, non_increasing);
        return true;
    };

    uint32_t consumed_payload_size = 0u;
    for (uint32_t span_index = 0u; span_index < span_count; ++span_index) {
        const uint32_t offset = payload_offsets[span_index];
        const uint32_t size = payload_sizes[span_index];
        const uint32_t address = addresses[span_index];
        if (size == 0u || offset != consumed_payload_size ||
            offset > payload_size || size > payload_size - offset ||
            (flags[span_index] & ~1u) != 0u) {
            return false;
        }
        if (state->run_active != 0u && address != state->next_address &&
            !reset_run(span_index * 2u)) {
            return false;
        }
        state->run_active = 1u;
        uint32_t byte_index = 0u;
        while (state->partial_size != 0u && byte_index < size) {
            state->partial_word |=
                static_cast<uint32_t>(payload[offset + byte_index]) <<
                (state->partial_size * 8u);
            ++state->partial_size;
            ++byte_index;
            if (state->partial_size != 4u) {
                continue;
            }
            const uint32_t word = state->partial_word;
            state->partial_word = 0u;
            state->partial_size = 0u;
            if (!consume_word(word, span_index * 2u + 1u)) {
                return false;
            }
        }
        while (byte_index + 4u <= size) {
            uint32_t word = 0u;
            std::memcpy(
                &word,
                payload + offset + byte_index,
                sizeof(word)
            );
            byte_index += 4u;
            if (!consume_word(word, span_index * 2u + 1u)) {
                return false;
            }
        }
        while (byte_index < size) {
            state->partial_word |=
                static_cast<uint32_t>(payload[offset + byte_index]) <<
                (state->partial_size * 8u);
            ++state->partial_size;
            ++byte_index;
        }
        state->next_address = address + size;
        consumed_payload_size = offset + size;
        if ((flags[span_index] & 1u) != 0u) {
            const uint32_t position = span_index * 2u + 1u;
            if (!reset_run(position) ||
                !retain_active_vertex_ranges() ||
                !flush_frame_state(position)) {
                return false;
            }
        }
    }
    if (consumed_payload_size != payload_size) {
        return false;
    }
    if (!finish) {
        return true;
    }
    const uint32_t position = span_count * 2u;
    return reset_run(position) && retain_active_vertex_ranges() &&
        flush_frame_state(position);
}

struct B2RSha256 {
    uint32_t hash[8] = {
        0x6a09e667u, 0xbb67ae85u, 0x3c6ef372u, 0xa54ff53au,
        0x510e527fu, 0x9b05688cu, 0x1f83d9abu, 0x5be0cd19u};
    uint8_t block[64]{};
    uint64_t byte_count = 0u;
    uint32_t block_size = 0u;

    static uint32_t rotate(uint32_t value, uint32_t count) {
        return (value >> count) | (value << (32u - count));
    }

    void transform(const uint8_t* input) {
        static constexpr uint32_t constants[64] = {
            0x428a2f98u, 0x71374491u, 0xb5c0fbcfu, 0xe9b5dba5u,
            0x3956c25bu, 0x59f111f1u, 0x923f82a4u, 0xab1c5ed5u,
            0xd807aa98u, 0x12835b01u, 0x243185beu, 0x550c7dc3u,
            0x72be5d74u, 0x80deb1feu, 0x9bdc06a7u, 0xc19bf174u,
            0xe49b69c1u, 0xefbe4786u, 0x0fc19dc6u, 0x240ca1ccu,
            0x2de92c6fu, 0x4a7484aau, 0x5cb0a9dcu, 0x76f988dau,
            0x983e5152u, 0xa831c66du, 0xb00327c8u, 0xbf597fc7u,
            0xc6e00bf3u, 0xd5a79147u, 0x06ca6351u, 0x14292967u,
            0x27b70a85u, 0x2e1b2138u, 0x4d2c6dfcu, 0x53380d13u,
            0x650a7354u, 0x766a0abbu, 0x81c2c92eu, 0x92722c85u,
            0xa2bfe8a1u, 0xa81a664bu, 0xc24b8b70u, 0xc76c51a3u,
            0xd192e819u, 0xd6990624u, 0xf40e3585u, 0x106aa070u,
            0x19a4c116u, 0x1e376c08u, 0x2748774cu, 0x34b0bcb5u,
            0x391c0cb3u, 0x4ed8aa4au, 0x5b9cca4fu, 0x682e6ff3u,
            0x748f82eeu, 0x78a5636fu, 0x84c87814u, 0x8cc70208u,
            0x90befffau, 0xa4506cebu, 0xbef9a3f7u, 0xc67178f2u};
        uint32_t words[64]{};
        for (uint32_t index = 0u; index < 16u; ++index) {
            words[index] =
                static_cast<uint32_t>(input[index * 4u]) << 24u |
                static_cast<uint32_t>(input[index * 4u + 1u]) << 16u |
                static_cast<uint32_t>(input[index * 4u + 2u]) << 8u |
                static_cast<uint32_t>(input[index * 4u + 3u]);
        }
        for (uint32_t index = 16u; index < 64u; ++index) {
            const uint32_t first = rotate(words[index - 15u], 7u) ^
                rotate(words[index - 15u], 18u) ^
                (words[index - 15u] >> 3u);
            const uint32_t second = rotate(words[index - 2u], 17u) ^
                rotate(words[index - 2u], 19u) ^
                (words[index - 2u] >> 10u);
            words[index] = words[index - 16u] + first +
                words[index - 7u] + second;
        }
        uint32_t a = hash[0];
        uint32_t b = hash[1];
        uint32_t c = hash[2];
        uint32_t d = hash[3];
        uint32_t e = hash[4];
        uint32_t f = hash[5];
        uint32_t g = hash[6];
        uint32_t h = hash[7];
        for (uint32_t index = 0u; index < 64u; ++index) {
            const uint32_t sum1 = rotate(e, 6u) ^ rotate(e, 11u) ^
                rotate(e, 25u);
            const uint32_t choice = (e & f) ^ (~e & g);
            const uint32_t temporary1 = h + sum1 + choice +
                constants[index] + words[index];
            const uint32_t sum0 = rotate(a, 2u) ^ rotate(a, 13u) ^
                rotate(a, 22u);
            const uint32_t majority = (a & b) ^ (a & c) ^ (b & c);
            const uint32_t temporary2 = sum0 + majority;
            h = g;
            g = f;
            f = e;
            e = d + temporary1;
            d = c;
            c = b;
            b = a;
            a = temporary1 + temporary2;
        }
        hash[0] += a;
        hash[1] += b;
        hash[2] += c;
        hash[3] += d;
        hash[4] += e;
        hash[5] += f;
        hash[6] += g;
        hash[7] += h;
    }

    void update(const uint8_t* input, uint32_t size) {
        byte_count += size;
        while (size != 0u) {
            const uint32_t chunk = std::min(64u - block_size, size);
            std::memcpy(block + block_size, input, chunk);
            block_size += chunk;
            input += chunk;
            size -= chunk;
            if (block_size == 64u) {
                transform(block);
                block_size = 0u;
            }
        }
    }

    void finish(uint8_t output[32]) {
        const uint64_t bit_count = byte_count * 8u;
        block[block_size++] = 0x80u;
        if (block_size > 56u) {
            std::memset(block + block_size, 0, 64u - block_size);
            transform(block);
            block_size = 0u;
        }
        std::memset(block + block_size, 0, 56u - block_size);
        for (uint32_t index = 0u; index < 8u; ++index) {
            block[63u - index] = static_cast<uint8_t>(
                bit_count >> (index * 8u));
        }
        transform(block);
        for (uint32_t index = 0u; index < 8u; ++index) {
            output[index * 4u] = static_cast<uint8_t>(hash[index] >> 24u);
            output[index * 4u + 1u] =
                static_cast<uint8_t>(hash[index] >> 16u);
            output[index * 4u + 2u] =
                static_cast<uint8_t>(hash[index] >> 8u);
            output[index * 4u + 3u] = static_cast<uint8_t>(hash[index]);
        }
    }
};

static constexpr uint32_t B2R_RESOURCE_SCAN_OUTPUT_CAPACITY =
    B2R_RESOURCE_FRAME_BINDING_CAPACITY * 3u +
    B2R_RESOURCE_FRAME_RANGE_CAPACITY * 2u;

struct B2RNativeNormalRuntimeState {
    B2RResourceSpanScanState resource_scan{};
    std::vector<uint32_t> methods;
    std::vector<uint32_t> values;
    std::vector<uint32_t> positions;
    struct AudioFormat {
        uint16_t tag = 0u;
        uint16_t channels = 0u;
        uint32_t sample_rate = 0u;
        uint16_t block_align = 0u;
        uint16_t bits_per_sample = 0u;
        uint16_t samples_per_block = 0u;
    };
    struct AudioBufferMirror {
        uint32_t key = 0u;
        uint32_t data = 0u;
        uint32_t size = 0u;
        AudioFormat format{};
        int32_t volume = 0;
        uint32_t frequency = 0u;
        uint32_t current_position = 0u;
        bool playing = false;
    };
    struct AudioPendingBuffer {
        uint32_t output = 0u;
        uint32_t data = 0u;
        uint32_t size = 0u;
        AudioFormat format{};
    };
    struct AudioPendingStream {
        uint32_t output = 0u;
        AudioFormat format{};
    };
    struct AudioStreamMirror {
        uint32_t key = 0u;
        AudioFormat format{};
    };
    struct AudioPlayback {
        uint32_t key = 0u;
        std::vector<int16_t> samples;
        uint64_t cursor_frame_q32 = 0u;
        uint64_t step_q32 = 1ull << 32u;
        size_t loop_start_frame = 0u;
        size_t loop_end_frame = 0u;
        int32_t gain_q16 = 1 << 16u;
        bool loop = false;
        uint32_t encoded_data = 0u;
        uint32_t encoded_size = 0u;
        AudioFormat format{};
        uint64_t payload_generation = 0u;
    };
    struct AudioStreamPlayback {
        uint32_t key = 0u;
        std::deque<std::vector<int16_t>> packets;
        size_t cursor = 0u;
    };
    std::mutex audio_mutex;
    std::condition_variable audio_condition;
    std::thread audio_thread;
    std::vector<AudioBufferMirror> audio_buffer_mirrors;
    std::vector<AudioPendingBuffer> audio_pending_buffers;
    std::vector<AudioPendingStream> audio_pending_streams;
    std::vector<AudioStreamMirror> audio_stream_mirrors;
    std::deque<AudioPlayback> audio_buffer_playbacks;
    std::deque<AudioStreamPlayback> audio_stream_playbacks;
    bool audio_shutdown = false;
    HMODULE audio_library = nullptr;
    int (__cdecl *audio_open)() = nullptr;
    int (__cdecl *audio_queue)(const void*, uint32_t) = nullptr;
    int (__cdecl *audio_clear)() = nullptr;
    void (__cdecl *audio_close)() = nullptr;
    uint64_t (__cdecl *audio_queued_bytes)() = nullptr;
    B2RNativeHostServiceState* host_state = nullptr;

    B2RNativeNormalRuntimeState()
        : methods(B2R_RESOURCE_SCAN_OUTPUT_CAPACITY),
          values(B2R_RESOURCE_SCAN_OUTPUT_CAPACITY),
          positions(B2R_RESOURCE_SCAN_OUTPUT_CAPACITY) {}
};

struct B2RLiveResourceDescriptor {
    uint32_t stage;
    uint32_t address;
    uint32_t source_address;
    uint32_t width;
    uint32_t height;
    uint32_t byte_count;
    char format[32];
};

struct B2RLiveTextureBinding {
    uint32_t stage;
    uint32_t address;
    uint32_t format;
    uint32_t image_rect;
};

static B2RNativeNormalRuntimeState* b2r_get_normal_runtime_state(
    B2RNativeHostServiceState* state
) {
    if (state == nullptr) { return nullptr; }
    B2RNativeNormalRuntimeState* runtime =
        reinterpret_cast<B2RNativeNormalRuntimeState*>(
            static_cast<uintptr_t>(state->normal_runtime_opaque));
    if (runtime == nullptr) {
        runtime = new (std::nothrow) B2RNativeNormalRuntimeState();
        if (runtime == nullptr) { return nullptr; }
        runtime->host_state = state;
        state->normal_runtime_opaque = reinterpret_cast<uint64_t>(runtime);
    }
    return runtime;
}

static std::vector<int16_t> b2r_audio_normalize_pcm16(
    const uint8_t* payload,
    size_t payload_size,
    uint32_t sample_rate,
    uint32_t channels
) {
    constexpr uint32_t kMinimumSampleRate = 4000u;
    constexpr uint32_t kMaximumSampleRate = 192000u;
    constexpr uint64_t kMaximumOutputFrames = 32u * 1024u * 1024u;
    std::vector<int16_t> output;
    if (payload == nullptr || payload_size < channels * 2u ||
        payload_size % (channels * 2u) != 0u ||
        sample_rate < kMinimumSampleRate ||
        sample_rate > kMaximumSampleRate ||
        (channels != 1u && channels != 2u)) {
        return output;
    }
    const size_t frame_count = payload_size / (channels * 2u);
    const uint64_t output_frame_count =
        (static_cast<uint64_t>(frame_count) * 48000u + sample_rate / 2u) /
        sample_rate;
    if (output_frame_count == 0u ||
        output_frame_count > kMaximumOutputFrames) {
        return output;
    }
    const size_t output_frames = static_cast<size_t>(output_frame_count);
    output.resize(output_frames * 2u);
    for (size_t frame = 0u; frame < output_frames; ++frame) {
        const size_t source_frame = std::min(
            frame_count - 1u,
            static_cast<size_t>(
                static_cast<uint64_t>(frame) * sample_rate / 48000u));
        int16_t left = 0;
        int16_t right = 0;
        std::memcpy(
            &left, payload + source_frame * channels * 2u, sizeof(left));
        if (channels == 2u) {
            std::memcpy(
                &right,
                payload + (source_frame * channels + 1u) * 2u,
                sizeof(right));
        } else {
            right = left;
        }
        output[frame * 2u] = static_cast<int16_t>(left / 2);
        output[frame * 2u + 1u] = static_cast<int16_t>(right / 2);
    }
    return output;
}

static int16_t b2r_audio_clamp_sample(int32_t value) {
    return static_cast<int16_t>(
        std::max(-32768, std::min(32767, value)));
}

static bool b2r_audio_decode_adpcm_channel(
    const uint8_t block[36], int16_t output[64]
) {
    static constexpr int index_table[16] = {
        -1, -1, -1, -1, 2, 4, 6, 8,
        -1, -1, -1, -1, 2, 4, 6, 8};
    static constexpr int step_table[89] = {
        7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31,
        34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130,
        143, 157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449,
        494, 544, 598, 658, 724, 796, 876, 963, 1060, 1166, 1282, 1411,
        1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026,
        4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442,
        11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623,
        27086, 29794, 32767};
    int16_t initial = 0;
    std::memcpy(&initial, block, sizeof(initial));
    int predictor = initial;
    int step_index = block[2];
    if (step_index >= 89) { return false; }
    output[0] = initial;
    uint32_t sample = 1u;
    for (uint32_t byte_index = 4u; byte_index < 36u && sample < 64u;
         ++byte_index) {
        const uint8_t packed = block[byte_index];
        for (uint32_t half = 0u; half < 2u && sample < 64u; ++half) {
            const uint8_t nibble = half == 0u ? packed & 0x0fu : packed >> 4u;
            const int step = step_table[step_index];
            int difference = step >> 3;
            if ((nibble & 1u) != 0u) { difference += step >> 2; }
            if ((nibble & 2u) != 0u) { difference += step >> 1; }
            if ((nibble & 4u) != 0u) { difference += step; }
            predictor += (nibble & 8u) != 0u ? -difference : difference;
            predictor = std::max(-32768, std::min(32767, predictor));
            step_index = std::max(
                0, std::min(88, step_index + index_table[nibble]));
            output[sample++] = static_cast<int16_t>(predictor);
        }
    }
    return sample == 64u;
}

static bool b2r_audio_decode_xbox_adpcm(
    const uint8_t* encoded,
    size_t encoded_size,
    uint32_t sample_rate,
    uint32_t channel_count,
    std::vector<int16_t>& output
) {
    if (encoded == nullptr || (channel_count != 1u && channel_count != 2u) ||
        sample_rate == 0u) {
        return false;
    }
    const size_t block_align = 36u * channel_count;
    if (encoded_size == 0u || encoded_size % block_align != 0u) {
        return false;
    }
    std::vector<int16_t> decoded;
    decoded.reserve(encoded_size / block_align * 64u * channel_count);
    for (size_t block_offset = 0u; block_offset < encoded_size;
         block_offset += block_align) {
        int16_t channels[2][64]{};
        for (uint32_t channel = 0u; channel < channel_count; ++channel) {
            uint8_t block[36]{};
            std::memcpy(
                block, encoded + block_offset + channel * 4u, 4u);
            for (uint32_t group = 0u; group < 8u; ++group) {
                const size_t group_offset = block_offset + channel_count * 4u +
                    group * channel_count * 4u;
                std::memcpy(
                    block + 4u + group * 4u,
                    encoded + group_offset + channel * 4u, 4u);
            }
            if (!b2r_audio_decode_adpcm_channel(block, channels[channel])) {
                return false;
            }
        }
        for (uint32_t sample = 0u; sample < 64u; ++sample) {
            for (uint32_t channel = 0u; channel < channel_count; ++channel) {
                decoded.push_back(channels[channel][sample]);
            }
        }
    }
    output = b2r_audio_normalize_pcm16(
        reinterpret_cast<const uint8_t*>(decoded.data()),
        decoded.size() * sizeof(int16_t), sample_rate, channel_count);
    return !output.empty();
}

static void b2r_audio_worker(B2RNativeNormalRuntimeState* runtime) {
    constexpr size_t kChunkSamples = 4800u;
    std::vector<int16_t> mixed(kChunkSamples);
    for (;;) {
        std::unique_lock<std::mutex> lock(runtime->audio_mutex);
        runtime->audio_condition.wait(lock, [&]() {
            return runtime->audio_shutdown ||
                !runtime->audio_buffer_playbacks.empty() ||
                !runtime->audio_stream_playbacks.empty();
        });
        if (runtime->audio_shutdown) { return; }
        const auto queued_bytes = runtime->audio_queued_bytes;
        lock.unlock();
        if (queued_bytes != nullptr && queued_bytes() >= 38400u) {
            Sleep(5u);
            continue;
        }
        lock.lock();
        std::fill(mixed.begin(), mixed.end(), 0);
        for (auto& playback : runtime->audio_buffer_playbacks) {
            const size_t sample_frame_count = playback.samples.size() / 2u;
            for (size_t index = 0u; index + 1u < kChunkSamples; index += 2u) {
                size_t source_frame = static_cast<size_t>(
                    playback.cursor_frame_q32 >> 32u);
                const size_t playback_end = playback.loop
                    ? playback.loop_end_frame : sample_frame_count;
                if (source_frame >= playback_end) {
                    if (!playback.loop ||
                        playback.loop_start_frame >= playback.loop_end_frame) {
                        break;
                    }
                    const size_t loop_frames =
                        playback.loop_end_frame - playback.loop_start_frame;
                    source_frame = playback.loop_start_frame +
                        (source_frame - playback.loop_start_frame) % loop_frames;
                    playback.cursor_frame_q32 =
                        static_cast<uint64_t>(source_frame) << 32u;
                }
                const int32_t left = static_cast<int32_t>(
                    playback.samples[source_frame * 2u]) * playback.gain_q16 /
                    (1 << 16u);
                const int32_t right = static_cast<int32_t>(
                    playback.samples[source_frame * 2u + 1u]) *
                    playback.gain_q16 / (1 << 16u);
                mixed[index] = b2r_audio_clamp_sample(
                    static_cast<int32_t>(mixed[index]) + left);
                mixed[index + 1u] = b2r_audio_clamp_sample(
                    static_cast<int32_t>(mixed[index + 1u]) + right);
                playback.cursor_frame_q32 += playback.step_q32;
            }
        }
        runtime->audio_buffer_playbacks.erase(
            std::remove_if(
                runtime->audio_buffer_playbacks.begin(),
                runtime->audio_buffer_playbacks.end(),
                [&](const B2RNativeNormalRuntimeState::AudioPlayback& playback) {
                    const bool finished = !playback.loop &&
                        (playback.cursor_frame_q32 >> 32u) >=
                            playback.samples.size() / 2u;
                    if (finished) {
                        for (auto& mirror : runtime->audio_buffer_mirrors) {
                            if (mirror.key == playback.key) {
                                mirror.playing = false;
                                break;
                            }
                        }
                    }
                    return finished;
                }),
            runtime->audio_buffer_playbacks.end());
        for (auto& playback : runtime->audio_stream_playbacks) {
            for (size_t index = 0u; index < kChunkSamples;) {
                while (!playback.packets.empty() &&
                       playback.cursor >= playback.packets.front().size()) {
                    playback.packets.pop_front();
                    playback.cursor = 0u;
                }
                if (playback.packets.empty()) { break; }
                const auto& packet = playback.packets.front();
                mixed[index] = b2r_audio_clamp_sample(
                    static_cast<int32_t>(mixed[index]) +
                    packet[playback.cursor++]);
                ++index;
            }
        }
        runtime->audio_stream_playbacks.erase(
            std::remove_if(
                runtime->audio_stream_playbacks.begin(),
                runtime->audio_stream_playbacks.end(),
                [](const B2RNativeNormalRuntimeState::AudioStreamPlayback& playback) {
                    return playback.packets.empty();
                }),
            runtime->audio_stream_playbacks.end());
        runtime->host_state->native_audio_active_buffer_count =
            static_cast<uint32_t>(runtime->audio_buffer_playbacks.size());
        runtime->host_state->native_audio_active_stream_count =
            static_cast<uint32_t>(runtime->audio_stream_playbacks.size());
        runtime->host_state->native_audio_looping =
            std::any_of(
                runtime->audio_buffer_playbacks.begin(),
                runtime->audio_buffer_playbacks.end(),
                [](const B2RNativeNormalRuntimeState::AudioPlayback& playback) {
                    return playback.loop;
                });
        auto queue = runtime->audio_queue;
        lock.unlock();
        const uint32_t byte_count = static_cast<uint32_t>(
            mixed.size() * sizeof(int16_t));
        if (queue == nullptr || !queue(mixed.data(), byte_count)) {
            ++runtime->host_state->native_audio_output_error_count;
            Sleep(50u);
            continue;
        }
        ++runtime->host_state->native_audio_submitted_buffer_count;
        runtime->host_state->native_audio_submitted_byte_count += byte_count;
        ++runtime->host_state->native_audio_mixed_chunk_count;
        runtime->host_state->native_audio_queued_bytes =
            queued_bytes != nullptr ? queued_bytes() : 0u;
    }
}

static bool b2r_audio_ensure_output(
    B2RNativeHostServiceState* state,
    B2RNativeNormalRuntimeState* runtime
) {
    if (runtime->audio_library != nullptr) { return true; }
    if (state->audio_library_path[0] == '\0') { return false; }
    runtime->audio_library = LoadLibraryExA(
        state->audio_library_path, nullptr,
        LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
    if (runtime->audio_library == nullptr) {
        ++state->native_audio_output_error_count;
        return false;
    }
#define B2R_AUDIO_SYMBOL(field, name) \
    runtime->field = reinterpret_cast<decltype(runtime->field)>( \
        GetProcAddress(runtime->audio_library, name))
    B2R_AUDIO_SYMBOL(audio_open, "b2r_audio_open");
    B2R_AUDIO_SYMBOL(audio_queue, "b2r_audio_queue");
    B2R_AUDIO_SYMBOL(audio_clear, "b2r_audio_clear");
    B2R_AUDIO_SYMBOL(audio_close, "b2r_audio_close");
    B2R_AUDIO_SYMBOL(audio_queued_bytes, "b2r_audio_queued_bytes");
#undef B2R_AUDIO_SYMBOL
    if (runtime->audio_open == nullptr || runtime->audio_queue == nullptr ||
        runtime->audio_clear == nullptr || runtime->audio_close == nullptr ||
        runtime->audio_queued_bytes == nullptr || !runtime->audio_open()) {
        ++state->native_audio_output_error_count;
        FreeLibrary(runtime->audio_library);
        runtime->audio_library = nullptr;
        return false;
    }
    state->native_audio_output_open = true;
    runtime->audio_thread = std::thread(b2r_audio_worker, runtime);
    return true;
}

static B2RNativeNormalRuntimeState::AudioFormat b2r_audio_guest_format(
    B2RNativeHostServiceState* state, uint32_t address
) {
    B2RNativeNormalRuntimeState::AudioFormat format{};
    if (state == nullptr || address == 0u) { return format; }
    format.tag = b2r_native_service_read_u16(state, address);
    format.channels = b2r_native_service_read_u16(state, address + 2u);
    format.sample_rate = b2r_native_service_read_u32(state, address + 4u);
    format.block_align = b2r_native_service_read_u16(state, address + 0xcu);
    format.bits_per_sample = b2r_native_service_read_u16(state, address + 0xeu);
    format.samples_per_block = b2r_native_service_read_u16(state, address + 0x12u);
    return format;
}

static bool b2r_audio_read_guest_bytes(
    B2RNativeHostServiceState* state,
    uint32_t address,
    uint32_t size,
    std::vector<uint8_t>& output
) {
    constexpr uint32_t kMaximumBufferSize = 64u * 1024u * 1024u;
    if (state == nullptr || state->read_pages == nullptr || address == 0u ||
        size == 0u || size > kMaximumBufferSize) {
        return false;
    }
    output.resize(size);
    uint32_t copied = 0u;
    while (copied < size) {
        const uint32_t cached = b2r_native_service_cache_address(
            state, address + copied);
        const uint32_t page = cached >> 12u;
        const uint32_t page_offset = cached & 0xfffu;
        const uint32_t chunk = std::min(0x1000u - page_offset, size - copied);
        if (state->read_pages[page] == nullptr) {
            output.clear();
            return false;
        }
        std::memcpy(
            output.data() + copied,
            state->read_pages[page] + page_offset,
            chunk);
        copied += chunk;
    }
    return true;
}

static bool b2r_audio_decode_guest_payload(
    B2RNativeHostServiceState* state,
    uint32_t data,
    uint32_t size,
    const B2RNativeNormalRuntimeState::AudioFormat& format,
    std::vector<int16_t>& output
) {
    std::vector<uint8_t> encoded;
    if (!b2r_audio_read_guest_bytes(state, data, size, encoded)) { return false; }
    if (format.tag == 1u && format.bits_per_sample == 16u) {
        output = b2r_audio_normalize_pcm16(
            encoded.data(), encoded.size(), format.sample_rate, format.channels);
        return !output.empty();
    }
    if (format.tag == 0x69u) {
        return b2r_audio_decode_xbox_adpcm(
            encoded.data(), encoded.size(), format.sample_rate,
            format.channels, output);
    }
    return false;
}

static int32_t b2r_audio_volume_gain_q16(int32_t volume) {
    if (volume <= -10000) { return 0; }
    if (volume >= 0) { return 1 << 16u; }
    const double gain = std::pow(10.0, static_cast<double>(volume) / 2000.0);
    return static_cast<int32_t>(gain * static_cast<double>(1 << 16u) + 0.5);
}

static uint64_t b2r_audio_frequency_step_q32(
    uint32_t frequency,
    uint32_t original_frequency
) {
    if (original_frequency == 0u) { return 1ull << 32u; }
    const uint64_t effective_frequency = frequency != 0u
        ? frequency : original_frequency;
    return std::max<uint64_t>(
        1u,
        (effective_frequency * (1ull << 32u) + original_frequency / 2u) /
            original_frequency);
}

static bool b2r_audio_normalized_frame_count(
    const B2RNativeNormalRuntimeState::AudioFormat& format,
    uint32_t byte_count,
    size_t& frame_count
) {
    uint64_t source_frames = 0u;
    if (format.tag == 1u && format.bits_per_sample == 16u &&
        (format.channels == 1u || format.channels == 2u)) {
        const uint32_t bytes_per_frame = format.channels * 2u;
        if (byte_count % bytes_per_frame != 0u) { return false; }
        source_frames = byte_count / bytes_per_frame;
    } else if (format.tag == 0x69u &&
               (format.channels == 1u || format.channels == 2u)) {
        const uint32_t block_align = format.channels * 36u;
        if (byte_count % block_align != 0u) { return false; }
        source_frames = static_cast<uint64_t>(byte_count / block_align) * 64u;
    } else {
        return false;
    }
    if (format.sample_rate == 0u) { return false; }
    frame_count = static_cast<size_t>(
        (source_frames * 48000u + format.sample_rate / 2u) /
        format.sample_rate);
    return true;
}

static uint64_t b2r_audio_region_generation(
    const B2RNativeHostServiceState* state,
    uint32_t data,
    uint32_t size
) {
    constexpr uint64_t kOffsetBasis = 1469598103934665603ull;
    constexpr uint64_t kPrime = 1099511628211ull;
    if (state == nullptr || state->dirty_page_generations == nullptr ||
        data == 0u || size == 0u) {
        return 0u;
    }
    const uint64_t end = static_cast<uint64_t>(data) + size - 1u;
    if (end > 0xffffffffull) { return 0u; }
    const uint32_t first_page = data >> 12u;
    const uint32_t last_page = static_cast<uint32_t>(end) >> 12u;
    uint64_t generation = kOffsetBasis;
    for (uint32_t page = first_page; page <= last_page; ++page) {
        generation ^= static_cast<uint64_t>(page) << 32u |
            state->dirty_page_generations[page];
        generation *= kPrime;
    }
    return generation;
}

static bool b2r_audio_normalized_frame_byte_position(
    const B2RNativeNormalRuntimeState::AudioFormat& format,
    size_t normalized_frame,
    uint32_t& position
) {
    if (format.sample_rate == 0u) { return false; }
    const uint64_t source_frames =
        static_cast<uint64_t>(normalized_frame) * format.sample_rate / 48000u;
    uint64_t byte_position = 0u;
    if (format.tag == 1u && format.bits_per_sample == 16u &&
        (format.channels == 1u || format.channels == 2u)) {
        byte_position = source_frames * format.channels * 2u;
    } else if (format.tag == 0x69u &&
               (format.channels == 1u || format.channels == 2u)) {
        byte_position = source_frames / 64u * format.channels * 36u;
    } else {
        return false;
    }
    if (byte_position > 0xffffffffull) { return false; }
    position = static_cast<uint32_t>(byte_position);
    return true;
}

static B2RNativeNormalRuntimeState::AudioBufferMirror*
b2r_audio_buffer_mirror(
    B2RNativeNormalRuntimeState* runtime,
    uint32_t key,
    bool create
) {
    if (runtime == nullptr || key == 0u) { return nullptr; }
    for (auto& mirror : runtime->audio_buffer_mirrors) {
        if (mirror.key == key) { return &mirror; }
    }
    if (!create) { return nullptr; }
    if (runtime->audio_buffer_mirrors.size() >= 256u) {
        runtime->audio_buffer_mirrors.erase(
            runtime->audio_buffer_mirrors.begin());
    }
    runtime->audio_buffer_mirrors.push_back({key});
    return &runtime->audio_buffer_mirrors.back();
}

static B2RNativeNormalRuntimeState::AudioStreamMirror*
b2r_audio_stream_mirror(
    B2RNativeNormalRuntimeState* runtime,
    uint32_t key,
    bool create
) {
    if (runtime == nullptr || key == 0u) { return nullptr; }
    for (auto& mirror : runtime->audio_stream_mirrors) {
        if (mirror.key == key) { return &mirror; }
    }
    if (!create) { return nullptr; }
    if (runtime->audio_stream_mirrors.size() >= 32u) {
        runtime->audio_stream_mirrors.erase(
            runtime->audio_stream_mirrors.begin());
    }
    runtime->audio_stream_mirrors.push_back({key});
    return &runtime->audio_stream_mirrors.back();
}

static void b2r_audio_resolve_pending_buffer(
    B2RNativeHostServiceState* state,
    B2RNativeNormalRuntimeState* runtime,
    uint32_t buffer
) {
    if (state == nullptr || runtime == nullptr || buffer == 0u) { return; }
    for (size_t index = 0u; index < runtime->audio_pending_buffers.size();
         ++index) {
        const auto pending = runtime->audio_pending_buffers[index];
        if (b2r_native_service_read_u32(state, pending.output) != buffer) {
            continue;
        }
        B2RNativeNormalRuntimeState::AudioBufferMirror* mirror =
            b2r_audio_buffer_mirror(runtime, buffer, true);
        if (mirror != nullptr) {
            mirror->data = pending.data;
            mirror->size = pending.size;
            mirror->format = pending.format;
        }
        runtime->audio_pending_buffers.erase(
            runtime->audio_pending_buffers.begin() + index);
        return;
    }
}

static void b2r_native_audio_buffer_create(
    B2RNativeHostServiceState* state,
    uint32_t descriptor,
    uint32_t output
) {
    if (state == nullptr || !state->normal_runtime_enabled) { return; }
    ++state->native_audio_buffer_create_count;
    B2RNativeNormalRuntimeState* runtime = b2r_get_normal_runtime_state(state);
    if (runtime == nullptr || descriptor == 0u || output == 0u) { return; }
    if (runtime->audio_pending_buffers.size() >= 256u) {
        runtime->audio_pending_buffers.erase(
            runtime->audio_pending_buffers.begin());
    }
    runtime->audio_pending_buffers.push_back({
        output,
        b2r_native_service_read_u32(state, descriptor + 8u),
        b2r_native_service_read_u32(state, descriptor + 0xcu),
        b2r_audio_guest_format(
            state, b2r_native_service_read_u32(state, descriptor + 0x10u)),
    });
}

static void b2r_native_audio_buffer_set_data(
    B2RNativeHostServiceState* state,
    uint32_t buffer,
    uint32_t data,
    uint32_t size
) {
    if (state == nullptr || !state->normal_runtime_enabled) { return; }
    B2RNativeNormalRuntimeState* runtime = b2r_get_normal_runtime_state(state);
    b2r_audio_resolve_pending_buffer(state, runtime, buffer);
    B2RNativeNormalRuntimeState::AudioBufferMirror* mirror =
        b2r_audio_buffer_mirror(runtime, buffer, true);
    if (mirror != nullptr) {
        mirror->data = data;
        mirror->size = size;
    }
    ++state->native_audio_buffer_data_count;
}

static void b2r_native_audio_buffer_set_format(
    B2RNativeHostServiceState* state,
    uint32_t buffer,
    uint32_t format
) {
    if (state == nullptr || !state->normal_runtime_enabled) { return; }
    B2RNativeNormalRuntimeState* runtime = b2r_get_normal_runtime_state(state);
    b2r_audio_resolve_pending_buffer(state, runtime, buffer);
    B2RNativeNormalRuntimeState::AudioBufferMirror* mirror =
        b2r_audio_buffer_mirror(runtime, buffer, true);
    if (mirror != nullptr) {
        mirror->format = b2r_audio_guest_format(state, format);
    }
    ++state->native_audio_buffer_format_count;
}

static void b2r_native_audio_buffer_set_volume(
    B2RNativeHostServiceState* state,
    uint32_t buffer,
    int32_t volume
) {
    if (state == nullptr || !state->normal_runtime_enabled) { return; }
    B2RNativeNormalRuntimeState* runtime = b2r_get_normal_runtime_state(state);
    b2r_audio_resolve_pending_buffer(state, runtime, buffer);
    B2RNativeNormalRuntimeState::AudioBufferMirror* mirror =
        b2r_audio_buffer_mirror(runtime, buffer, true);
    if (mirror != nullptr) { mirror->volume = volume; }
    ++state->native_audio_buffer_volume_count;
    state->native_audio_last_volume = volume;
    if (runtime == nullptr) { return; }
    const int32_t gain_q16 = b2r_audio_volume_gain_q16(volume);
    std::lock_guard<std::mutex> lock(runtime->audio_mutex);
    for (auto& playback : runtime->audio_buffer_playbacks) {
        if (playback.key == buffer) { playback.gain_q16 = gain_q16; }
    }
}

static void b2r_native_audio_buffer_set_frequency(
    B2RNativeHostServiceState* state,
    uint32_t buffer,
    uint32_t frequency
) {
    if (state == nullptr || !state->normal_runtime_enabled) { return; }
    B2RNativeNormalRuntimeState* runtime = b2r_get_normal_runtime_state(state);
    b2r_audio_resolve_pending_buffer(state, runtime, buffer);
    B2RNativeNormalRuntimeState::AudioBufferMirror* mirror =
        b2r_audio_buffer_mirror(runtime, buffer, true);
    if (mirror != nullptr) { mirror->frequency = frequency; }
    ++state->native_audio_buffer_frequency_count;
    state->native_audio_last_frequency = frequency;
    if (runtime == nullptr || mirror == nullptr) { return; }
    const uint64_t step_q32 = b2r_audio_frequency_step_q32(
        frequency, mirror->format.sample_rate);
    std::lock_guard<std::mutex> lock(runtime->audio_mutex);
    for (auto& playback : runtime->audio_buffer_playbacks) {
        if (playback.key == buffer) { playback.step_q32 = step_q32; }
    }
}

static uint32_t b2r_native_audio_buffer_get_position(
    B2RNativeHostServiceState* state,
    uint32_t buffer,
    uint32_t play_cursor_output,
    uint32_t write_cursor_output
) {
    constexpr uint32_t kSuccess = 0u;
    constexpr uint32_t kFailure = 0x80004005u;
    if (state == nullptr || !state->normal_runtime_enabled || buffer == 0u) {
        return kFailure;
    }
    ++state->native_audio_buffer_get_position_count;
    B2RNativeNormalRuntimeState* runtime = b2r_get_normal_runtime_state(state);
    b2r_audio_resolve_pending_buffer(state, runtime, buffer);
    B2RNativeNormalRuntimeState::AudioBufferMirror* mirror =
        b2r_audio_buffer_mirror(runtime, buffer, false);
    if (runtime == nullptr || mirror == nullptr) { return kFailure; }
    uint32_t play_cursor = mirror->current_position;
    std::lock_guard<std::mutex> lock(runtime->audio_mutex);
    for (auto& playback : runtime->audio_buffer_playbacks) {
        if (playback.key != buffer) { continue; }
        const uint64_t generation = b2r_audio_region_generation(
            state, playback.encoded_data, playback.encoded_size);
        if (generation != playback.payload_generation) {
            std::vector<int16_t> samples;
            if (b2r_audio_decode_guest_payload(
                    state, playback.encoded_data, playback.encoded_size,
                    playback.format, samples) &&
                playback.loop_end_frame <= samples.size() / 2u) {
                playback.samples = std::move(samples);
                ++state->native_audio_buffer_refresh_count;
            } else {
                ++state->native_audio_decode_failure_count;
            }
            playback.payload_generation = generation;
        }
        if (!b2r_audio_normalized_frame_byte_position(
                playback.format,
                static_cast<size_t>(playback.cursor_frame_q32 >> 32u),
                play_cursor)) {
            return kFailure;
        }
        break;
    }
    const uint32_t descriptor = b2r_native_service_read_u32(state, buffer);
    const uint32_t play_length = descriptor != 0u
        ? b2r_native_service_read_u32(state, descriptor + 0xc4u) : mirror->size;
    if (play_length != 0u) { play_cursor %= play_length; }
    mirror->current_position = play_cursor;
    if (play_cursor_output != 0u) {
        b2r_native_service_write_u32(state, play_cursor_output, play_cursor);
    }
    if (write_cursor_output != 0u) {
        const uint32_t lead = mirror->format.block_align != 0u
            ? mirror->format.block_align : 1u;
        const uint32_t write_cursor = play_length != 0u
            ? (play_cursor + lead) % play_length : play_cursor;
        b2r_native_service_write_u32(state, write_cursor_output, write_cursor);
    }
    return kSuccess;
}

static uint32_t b2r_native_audio_buffer_set_position(
    B2RNativeHostServiceState* state,
    uint32_t buffer,
    uint32_t position
) {
    constexpr uint32_t kSuccess = 0u;
    constexpr uint32_t kFailure = 0x80004005u;
    if (state == nullptr || !state->normal_runtime_enabled || buffer == 0u) {
        return kFailure;
    }
    ++state->native_audio_buffer_set_position_count;
    B2RNativeNormalRuntimeState* runtime = b2r_get_normal_runtime_state(state);
    b2r_audio_resolve_pending_buffer(state, runtime, buffer);
    B2RNativeNormalRuntimeState::AudioBufferMirror* mirror =
        b2r_audio_buffer_mirror(runtime, buffer, false);
    const uint32_t descriptor = b2r_native_service_read_u32(state, buffer);
    const uint32_t play_length = descriptor != 0u
        ? b2r_native_service_read_u32(state, descriptor + 0xc4u) : 0u;
    if (runtime == nullptr || mirror == nullptr || play_length == 0u ||
        position >= play_length) {
        return kFailure;
    }
    size_t position_frame = 0u;
    if (!b2r_audio_normalized_frame_count(
            mirror->format, position, position_frame)) {
        return kFailure;
    }
    std::lock_guard<std::mutex> lock(runtime->audio_mutex);
    mirror->current_position = position;
    for (auto& playback : runtime->audio_buffer_playbacks) {
        if (playback.key == buffer) {
            playback.cursor_frame_q32 =
                static_cast<uint64_t>(position_frame) << 32u;
            break;
        }
    }
    return kSuccess;
}

static bool b2r_audio_select_buffer_region(
    B2RNativeHostServiceState* state,
    const B2RNativeNormalRuntimeState::AudioBufferMirror* mirror,
    uint32_t buffer,
    uint32_t flags,
    uint32_t& data,
    uint32_t& size,
    uint32_t& loop_start_bytes,
    uint32_t& loop_size_bytes
) {
    constexpr uint32_t kPlayStartOffset = 0xc0u;
    constexpr uint32_t kPlayLengthOffset = 0xc4u;
    constexpr uint32_t kLoopStartOffset = 0xc8u;
    constexpr uint32_t kLoopLengthOffset = 0xccu;
    if (state == nullptr || mirror == nullptr || buffer == 0u) { return false; }
    const uint32_t descriptor = b2r_native_service_read_u32(state, buffer);
    if (descriptor == 0u) { return false; }
    const uint32_t play_start = b2r_native_service_read_u32(
        state, descriptor + kPlayStartOffset);
    const uint32_t play_length = b2r_native_service_read_u32(
        state, descriptor + kPlayLengthOffset);
    const bool loop = (flags & 1u) != 0u;
    const uint32_t requested_loop_start = loop
        ? b2r_native_service_read_u32(state, descriptor + kLoopStartOffset)
        : 0u;
    const uint32_t requested_loop_length = loop
        ? b2r_native_service_read_u32(state, descriptor + kLoopLengthOffset)
        : play_length;
    const uint32_t effective_loop_start = loop ? requested_loop_start : 0u;
    const uint32_t effective_loop_length = loop && requested_loop_length != 0u
        ? requested_loop_length
        : (effective_loop_start <= play_length
            ? play_length - effective_loop_start : 0u);
    const uint64_t play_end = static_cast<uint64_t>(play_start) + play_length;
    const uint64_t loop_end =
        static_cast<uint64_t>(effective_loop_start) + effective_loop_length;
    const uint64_t region_address =
        static_cast<uint64_t>(mirror->data) + play_start;
    if (play_length == 0u || play_end > mirror->size ||
        effective_loop_length == 0u || loop_end > play_length ||
        region_address > 0xffffffffull) {
        return false;
    }
    data = static_cast<uint32_t>(region_address);
    size = play_length;
    loop_start_bytes = effective_loop_start;
    loop_size_bytes = effective_loop_length;
    return true;
}

static uint32_t b2r_native_audio_buffer_play(
    B2RNativeHostServiceState* state,
    uint32_t buffer,
    uint32_t flags
) {
    constexpr uint32_t kSuccess = 0u;
    constexpr uint32_t kFailure = 0x80004005u;
    constexpr uint32_t kVoicePointerOffset = 0x4u;
    constexpr uint32_t kVoiceFlagsOffset = 0x12u;
    constexpr uint16_t kVoiceAllocated = 0x0001u;
    constexpr uint16_t kVoicePlaying = 0x0002u;
    constexpr uint16_t kVoicePending = 0x8000u;
    if (state == nullptr || !state->normal_runtime_enabled) { return kFailure; }
    state->native_audio_buffer_play_stage = 1u;
    state->native_audio_last_buffer = buffer;
    state->native_audio_last_data = 0u;
    state->native_audio_last_size = 0u;
    state->native_audio_last_sample_rate = 0u;
    state->native_audio_last_format_tag = 0u;
    state->native_audio_last_channels = 0u;
    state->native_audio_last_bits_per_sample = 0u;
    state->native_audio_last_block_align = 0u;
    state->native_audio_last_samples_per_block = 0u;
    auto publish_debug = [&]() {
        if (state->live_control_mapping == nullptr ||
            state->live_control_size < 256u) {
            return;
        }
        std::memcpy(
            state->live_control_mapping + 236u,
            &state->native_audio_buffer_play_stage,
            sizeof(uint32_t));
        std::memcpy(
            state->live_control_mapping + 240u,
            &state->native_audio_last_buffer,
            sizeof(uint32_t));
        std::memcpy(
            state->live_control_mapping + 244u,
            &state->native_audio_last_data,
            sizeof(uint32_t));
        std::memcpy(
            state->live_control_mapping + 248u,
            &state->native_audio_last_size,
            sizeof(uint32_t));
        std::memcpy(
            state->live_control_mapping + 252u,
            &state->native_audio_last_sample_rate,
            sizeof(uint32_t));
        MemoryBarrier();
    };
    publish_debug();
    ++state->native_audio_buffer_play_count;
    B2RNativeNormalRuntimeState* runtime = b2r_get_normal_runtime_state(state);
    if (runtime == nullptr || buffer == 0u) { return kFailure; }
    const uint32_t voice = b2r_native_service_read_u32(
        state, buffer + kVoicePointerOffset);
    if (voice == 0u) { return kFailure; }
    const uint32_t voice_flags_address = voice + kVoiceFlagsOffset;
    const uint16_t voice_flags = b2r_native_service_read_u16(
        state, voice_flags_address);
    b2r_native_service_write_u16(
        state,
        voice_flags_address,
        static_cast<uint16_t>(
            (voice_flags | kVoiceAllocated | kVoicePlaying) & ~kVoicePending));
    b2r_audio_resolve_pending_buffer(state, runtime, buffer);
    B2RNativeNormalRuntimeState::AudioBufferMirror* mirror =
        b2r_audio_buffer_mirror(runtime, buffer, false);
    uint32_t selected_data = 0u;
    uint32_t selected_size = 0u;
    uint32_t loop_start_bytes = 0u;
    uint32_t loop_size_bytes = 0u;
    const bool selected = b2r_audio_select_buffer_region(
        state, mirror, buffer, flags, selected_data, selected_size,
        loop_start_bytes, loop_size_bytes);
    state->native_audio_buffer_play_stage = 2u;
    if (selected) {
        state->native_audio_last_data = selected_data;
        state->native_audio_last_size = selected_size;
        state->native_audio_last_sample_rate = mirror->format.sample_rate;
        state->native_audio_last_format_tag = mirror->format.tag;
        state->native_audio_last_channels = mirror->format.channels;
        state->native_audio_last_bits_per_sample = mirror->format.bits_per_sample;
        state->native_audio_last_block_align = mirror->format.block_align;
        state->native_audio_last_samples_per_block =
            mirror->format.samples_per_block;
        if ((flags & 1u) != 0u &&
            selected_size > state->native_audio_largest_loop_play_length) {
            state->native_audio_largest_loop_buffer = buffer;
            state->native_audio_largest_loop_data = selected_data;
            state->native_audio_largest_loop_play_length = selected_size;
            state->native_audio_largest_loop_start = loop_start_bytes;
            state->native_audio_largest_loop_length = loop_size_bytes;
            state->native_audio_largest_loop_sample_rate =
                mirror->format.sample_rate;
            state->native_audio_largest_loop_frequency = mirror->frequency;
            state->native_audio_largest_loop_format_tag = mirror->format.tag;
        }
    }
    publish_debug();
    const bool loop = (flags & 1u) != 0u;
    size_t loop_start_frame = 0u;
    size_t loop_end_frame = 0u;
    size_t current_position_frame = 0u;
    if (selected &&
        (!b2r_audio_normalized_frame_count(
            mirror->format, loop_start_bytes, loop_start_frame) ||
         !b2r_audio_normalized_frame_count(
            mirror->format,
            loop ? loop_start_bytes + loop_size_bytes : selected_size,
            loop_end_frame) ||
         !b2r_audio_normalized_frame_count(
            mirror->format, mirror->current_position,
            current_position_frame) ||
         loop_start_frame >= loop_end_frame)) {
        ++state->native_audio_decode_failure_count;
        return kSuccess;
    }
    bool was_playing = false;
    if (mirror != nullptr) {
        std::lock_guard<std::mutex> lock(runtime->audio_mutex);
        was_playing = mirror->playing;
        mirror->playing = true;
        for (auto& playback : runtime->audio_buffer_playbacks) {
            if (playback.key != buffer) { continue; }
            playback.step_q32 = b2r_audio_frequency_step_q32(
                mirror->frequency, mirror->format.sample_rate);
            playback.loop_start_frame = loop_start_frame;
            playback.loop_end_frame = loop_end_frame;
            playback.gain_q16 = b2r_audio_volume_gain_q16(mirror->volume);
            playback.loop = loop;
            break;
        }
    }
    if (was_playing) {
        ++state->native_audio_buffer_repeated_play_count;
        state->native_audio_buffer_play_stage = 6u;
        publish_debug();
        return kSuccess;
    }
    std::vector<int16_t> samples;
    state->native_audio_buffer_play_stage = 3u;
    publish_debug();
    if (!selected || !b2r_audio_decode_guest_payload(
            state, selected_data, selected_size, mirror->format, samples)) {
        state->native_audio_buffer_play_stage = 20u;
        publish_debug();
        ++state->native_audio_decode_failure_count;
        return kSuccess;
    }
    state->native_audio_buffer_play_stage = 4u;
    publish_debug();
    ++state->native_audio_decoded_buffer_count;
    if (!b2r_audio_ensure_output(state, runtime)) {
        state->native_audio_buffer_play_stage = 21u;
        publish_debug();
        return kSuccess;
    }
    state->native_audio_buffer_play_stage = 5u;
    publish_debug();
    std::lock_guard<std::mutex> lock(runtime->audio_mutex);
    runtime->audio_buffer_playbacks.erase(
        std::remove_if(
            runtime->audio_buffer_playbacks.begin(),
            runtime->audio_buffer_playbacks.end(),
            [&](const B2RNativeNormalRuntimeState::AudioPlayback& playback) {
                return playback.key == buffer;
            }),
        runtime->audio_buffer_playbacks.end());
    if (runtime->audio_buffer_playbacks.size() >= 64u) {
        runtime->audio_buffer_playbacks.pop_front();
        ++state->native_audio_dropped_buffer_count;
    }
    if (loop_end_frame > samples.size() / 2u) {
        ++state->native_audio_decode_failure_count;
        return kSuccess;
    }
    runtime->audio_buffer_playbacks.push_back({
        buffer,
        std::move(samples),
        static_cast<uint64_t>(current_position_frame) << 32u,
        b2r_audio_frequency_step_q32(
            mirror->frequency, mirror->format.sample_rate),
        loop_start_frame,
        loop_end_frame,
        b2r_audio_volume_gain_q16(mirror->volume),
        loop,
        selected_data,
        selected_size,
        mirror->format,
        b2r_audio_region_generation(state, selected_data, selected_size),
    });
    state->native_audio_active_buffer_count = static_cast<uint32_t>(
        runtime->audio_buffer_playbacks.size());
    runtime->audio_condition.notify_one();
    state->native_audio_buffer_play_stage = 6u;
    publish_debug();
    return kSuccess;
}

static void b2r_native_audio_buffer_stop(
    B2RNativeHostServiceState* state,
    uint32_t buffer
) {
    if (state == nullptr || !state->normal_runtime_enabled) { return; }
    ++state->native_audio_buffer_stop_count;
    B2RNativeNormalRuntimeState* runtime = b2r_get_normal_runtime_state(state);
    if (runtime == nullptr) { return; }
    B2RNativeNormalRuntimeState::AudioBufferMirror* mirror =
        b2r_audio_buffer_mirror(runtime, buffer, false);
    std::lock_guard<std::mutex> lock(runtime->audio_mutex);
    if (mirror != nullptr) {
        mirror->playing = false;
        for (const auto& playback : runtime->audio_buffer_playbacks) {
            if (playback.key != buffer) { continue; }
            uint32_t position = mirror->current_position;
            if (b2r_audio_normalized_frame_byte_position(
                    playback.format,
                    static_cast<size_t>(playback.cursor_frame_q32 >> 32u),
                    position)) {
                mirror->current_position = position;
            }
            break;
        }
    }
    runtime->audio_buffer_playbacks.erase(
        std::remove_if(
            runtime->audio_buffer_playbacks.begin(),
            runtime->audio_buffer_playbacks.end(),
            [&](const B2RNativeNormalRuntimeState::AudioPlayback& playback) {
                return playback.key == buffer;
            }),
        runtime->audio_buffer_playbacks.end());
    state->native_audio_active_buffer_count = static_cast<uint32_t>(
        runtime->audio_buffer_playbacks.size());
}

static uint32_t b2r_native_audio_buffer_stop_ex(
    B2RNativeHostServiceState* state,
    uint32_t buffer
) {
    constexpr uint32_t kSuccess = 0u;
    constexpr uint32_t kFailure = 0x80004005u;
    constexpr uint32_t kVoicePointerOffset = 0x4u;
    constexpr uint32_t kVoiceFlagsOffset = 0x12u;
    constexpr uint16_t kVoicePlaying = 0x0002u;
    constexpr uint16_t kVoicePending = 0x8000u;
    if (state == nullptr || !state->normal_runtime_enabled || buffer == 0u) {
        return kFailure;
    }
    b2r_native_audio_buffer_stop(state, buffer);
    const uint32_t voice = b2r_native_service_read_u32(
        state, buffer + kVoicePointerOffset);
    if (voice == 0u) { return kFailure; }
    const uint32_t voice_flags_address = voice + kVoiceFlagsOffset;
    const uint16_t voice_flags = b2r_native_service_read_u16(
        state, voice_flags_address);
    b2r_native_service_write_u16(
        state,
        voice_flags_address,
        static_cast<uint16_t>(voice_flags & ~(kVoicePlaying | kVoicePending)));
    return kSuccess;
}

static void b2r_native_audio_stream_create(
    B2RNativeHostServiceState* state,
    uint32_t descriptor,
    uint32_t output
) {
    if (state == nullptr || !state->normal_runtime_enabled) { return; }
    ++state->native_audio_stream_create_count;
    B2RNativeNormalRuntimeState* runtime = b2r_get_normal_runtime_state(state);
    if (runtime == nullptr || descriptor == 0u || output == 0u) { return; }
    const uint32_t format = b2r_native_service_read_u32(
        state, descriptor + 8u);
    if (runtime->audio_pending_streams.size() >= 32u) {
        runtime->audio_pending_streams.erase(
            runtime->audio_pending_streams.begin());
    }
    runtime->audio_pending_streams.push_back(
        {output, b2r_audio_guest_format(state, format)});
}

static void b2r_native_audio_stream_set_format(
    B2RNativeHostServiceState* state,
    uint32_t stream,
    uint32_t format
) {
    if (state == nullptr || !state->normal_runtime_enabled) { return; }
    B2RNativeNormalRuntimeState* runtime = b2r_get_normal_runtime_state(state);
    B2RNativeNormalRuntimeState::AudioStreamMirror* mirror =
        b2r_audio_stream_mirror(runtime, stream, true);
    if (mirror != nullptr) {
        mirror->format = b2r_audio_guest_format(state, format);
    }
}

static void b2r_native_audio_stream_process(
    B2RNativeHostServiceState* state,
    uint32_t stream,
    uint32_t packet
) {
    if (state == nullptr || !state->normal_runtime_enabled) { return; }
    ++state->native_audio_stream_process_count;
    B2RNativeNormalRuntimeState* runtime = b2r_get_normal_runtime_state(state);
    if (runtime == nullptr || stream == 0u || packet == 0u) { return; }
    for (size_t index = 0u; index < runtime->audio_pending_streams.size();) {
        const auto pending = runtime->audio_pending_streams[index];
        if (b2r_native_service_read_u32(state, pending.output) == stream) {
            B2RNativeNormalRuntimeState::AudioStreamMirror* mirror =
                b2r_audio_stream_mirror(runtime, stream, true);
            if (mirror != nullptr) { mirror->format = pending.format; }
            runtime->audio_pending_streams.erase(
                runtime->audio_pending_streams.begin() + index);
            break;
        }
        ++index;
    }
    B2RNativeNormalRuntimeState::AudioStreamMirror* mirror =
        b2r_audio_stream_mirror(runtime, stream, false);
    const uint32_t data = b2r_native_service_read_u32(state, packet);
    const uint32_t size = b2r_native_service_read_u32(state, packet + 4u);
    if (data == 0u || size == 0u) { return; }
    state->native_audio_stream_packet_byte_count += size;
    std::vector<int16_t> samples;
    if (mirror == nullptr || !b2r_audio_decode_guest_payload(
            state, data, size, mirror->format, samples)) {
        ++state->native_audio_decode_failure_count;
        return;
    }
    ++state->native_audio_decoded_stream_packet_count;
    if (!b2r_audio_ensure_output(state, runtime)) { return; }
    std::lock_guard<std::mutex> lock(runtime->audio_mutex);
    B2RNativeNormalRuntimeState::AudioStreamPlayback* playback = nullptr;
    for (auto& candidate : runtime->audio_stream_playbacks) {
        if (candidate.key == stream) { playback = &candidate; break; }
    }
    if (playback == nullptr) {
        if (runtime->audio_stream_playbacks.size() >= 16u) {
            runtime->audio_stream_playbacks.pop_front();
            ++state->native_audio_dropped_buffer_count;
        }
        runtime->audio_stream_playbacks.push_back({stream});
        playback = &runtime->audio_stream_playbacks.back();
    }
    if (playback->packets.size() >= 64u) {
        playback->packets.pop_front();
        playback->cursor = 0u;
        ++state->native_audio_dropped_buffer_count;
    }
    playback->packets.push_back(std::move(samples));
    state->native_audio_active_stream_count = static_cast<uint32_t>(
        runtime->audio_stream_playbacks.size());
    runtime->audio_condition.notify_one();
}

static void b2r_native_audio_stream_flush(
    B2RNativeHostServiceState* state,
    uint32_t stream
) {
    if (state == nullptr || !state->normal_runtime_enabled) { return; }
    ++state->native_audio_stream_flush_count;
    B2RNativeNormalRuntimeState* runtime = b2r_get_normal_runtime_state(state);
    if (runtime == nullptr) { return; }
    std::lock_guard<std::mutex> lock(runtime->audio_mutex);
    runtime->audio_stream_playbacks.erase(
        std::remove_if(
            runtime->audio_stream_playbacks.begin(),
            runtime->audio_stream_playbacks.end(),
            [&](const B2RNativeNormalRuntimeState::AudioStreamPlayback& playback) {
                return playback.key == stream;
            }),
        runtime->audio_stream_playbacks.end());
    state->native_audio_active_stream_count = static_cast<uint32_t>(
        runtime->audio_stream_playbacks.size());
}

static uint32_t b2r_live_read_u32(const uint8_t* bytes, uint32_t offset) {
    uint32_t value = 0u;
    std::memcpy(&value, bytes + offset, sizeof(value));
    return value;
}

static uint64_t b2r_live_read_u64(const uint8_t* bytes, uint32_t offset) {
    uint64_t value = 0u;
    std::memcpy(&value, bytes + offset, sizeof(value));
    return value;
}

static void b2r_live_write_u32(
    uint8_t* bytes, uint32_t offset, uint32_t value
) {
    std::memcpy(bytes + offset, &value, sizeof(value));
}

static void b2r_live_write_u64(
    uint8_t* bytes, uint32_t offset, uint64_t value
) {
    std::memcpy(bytes + offset, &value, sizeof(value));
}

static bool b2r_live_has_header(
    const uint8_t* mapping,
    uint64_t size,
    const char (&magic)[9],
    uint32_t expected_size
) {
    return mapping != nullptr && size >= expected_size &&
        std::memcmp(mapping, magic, 8u) == 0 &&
        b2r_live_read_u32(mapping, 8u) == 1u &&
        b2r_live_read_u32(mapping, 12u) == expected_size;
}

static bool b2r_live_ring_write(
    B2RNativeHostServiceState* state,
    const uint8_t* payload,
    uint32_t payload_size
) {
    constexpr uint64_t kDataOffset = 64u;
    uint8_t* mapping = state->live_command_mapping;
    const uint64_t capacity = state->live_command_size - kDataOffset;
    uint64_t write_cursor = state->live_command_write_cursor;
    if (write_cursor == 0u) {
        write_cursor = b2r_live_read_u64(mapping, 24u);
    }
    const uint64_t read_cursor = b2r_live_read_u64(mapping, 32u);
    if (read_cursor > write_cursor ||
        payload_size > capacity - (write_cursor - read_cursor)) {
        state->normal_runtime_failure_code = 2u;
        return false;
    }
    const uint64_t start = write_cursor % capacity;
    const uint32_t first = static_cast<uint32_t>(std::min<uint64_t>(
        payload_size, capacity - start));
    std::memcpy(mapping + kDataOffset + start, payload, first);
    if (first < payload_size) {
        std::memcpy(
            mapping + kDataOffset, payload + first, payload_size - first);
    }
    write_cursor += payload_size;
    state->live_command_write_cursor = write_cursor;
    MemoryBarrier();
    b2r_live_write_u64(mapping, 24u, write_cursor);
    return true;
}

static uint32_t b2r_live_resource_source_address(
    B2RNativeHostServiceState* state,
    uint32_t address
) {
    if (address != 0u && address < 64u * 1024u * 1024u &&
        state->read_pages != nullptr) {
        const uint32_t direct = address | 0x80000000u;
        if (state->read_pages[direct >> 12u] != nullptr) {
            return direct;
        }
    }
    return address;
}

static void b2r_live_read_resource_bytes(
    B2RNativeHostServiceState* state,
    uint32_t source_address,
    uint8_t* output,
    uint32_t size,
    B2RSha256* hash
) {
    static constexpr uint8_t zeros[4096]{};
    while (size != 0u) {
        const uint32_t cached = b2r_native_service_cache_address(
            state, source_address);
        const uint32_t offset = cached & 0xfffu;
        const uint32_t chunk = std::min(size, 0x1000u - offset);
        const uint8_t* page = state->read_pages != nullptr
            ? state->read_pages[cached >> 12u] : nullptr;
        const uint8_t* input = page != nullptr ? page + offset : zeros;
        if (output != nullptr) {
            std::memcpy(output, input, chunk);
            output += chunk;
        }
        if (hash != nullptr) { hash->update(input, chunk); }
        source_address += chunk;
        size -= chunk;
    }
}

static void b2r_live_append_resource(
    std::vector<B2RLiveResourceDescriptor>& resources,
    uint32_t stage,
    uint32_t address,
    uint32_t width,
    uint32_t height,
    uint32_t byte_count,
    const char* format,
    B2RNativeHostServiceState* state
) {
    if (address == 0u || width == 0u || height == 0u || byte_count == 0u ||
        byte_count > 16u * 1024u * 1024u) {
        return;
    }
    for (const B2RLiveResourceDescriptor& existing : resources) {
        if (existing.address == address && existing.width == width &&
            existing.height == height &&
            std::strcmp(existing.format, format) == 0) {
            return;
        }
    }
    B2RLiveResourceDescriptor resource{};
    resource.stage = stage;
    resource.address = address;
    resource.source_address = b2r_live_resource_source_address(state, address);
    resource.width = width;
    resource.height = height;
    resource.byte_count = byte_count;
    std::snprintf(resource.format, sizeof(resource.format), "%s", format);
    resources.push_back(resource);
}

static bool b2r_live_publish_resources(
    B2RNativeHostServiceState* state,
    const uint32_t* methods,
    const uint32_t* values,
    uint32_t output_count
) {
    std::vector<B2RLiveResourceDescriptor> resources;
    std::vector<B2RLiveTextureBinding> texture_bindings;
    uint32_t texture_addresses[4]{};
    uint32_t texture_formats[4]{};
    uint32_t texture_rects[4]{};
    uint32_t address_mask = 0u;
    uint32_t format_mask = 0u;
    uint32_t rect_mask = 0u;
    uint32_t range_start = 0u;
    for (uint32_t index = 0u; index < output_count; ++index) {
        const uint32_t method = methods[index];
        const uint32_t value = values[index];
        if (method >= B2R_RESOURCE_BINDING_ADDRESS_EVENT_BASE &&
            method < B2R_RESOURCE_BINDING_ADDRESS_EVENT_BASE + 4u) {
            const uint32_t stage =
                method - B2R_RESOURCE_BINDING_ADDRESS_EVENT_BASE;
            texture_addresses[stage] = value;
            address_mask |= 1u << stage;
            continue;
        }
        if (method >= B2R_RESOURCE_BINDING_FORMAT_EVENT_BASE &&
            method < B2R_RESOURCE_BINDING_FORMAT_EVENT_BASE + 4u) {
            const uint32_t stage =
                method - B2R_RESOURCE_BINDING_FORMAT_EVENT_BASE;
            texture_formats[stage] = value;
            format_mask |= 1u << stage;
            continue;
        }
        if (method >= B2R_RESOURCE_BINDING_RECT_EVENT_BASE &&
            method < B2R_RESOURCE_BINDING_RECT_EVENT_BASE + 4u) {
            const uint32_t stage =
                method - B2R_RESOURCE_BINDING_RECT_EVENT_BASE;
            texture_rects[stage] = value;
            rect_mask |= 1u << stage;
            const uint32_t stage_mask = 1u << stage;
            if ((address_mask & stage_mask) != 0u &&
                (format_mask & stage_mask) != 0u) {
                texture_bindings.push_back(B2RLiveTextureBinding{
                    stage,
                    texture_addresses[stage],
                    texture_formats[stage],
                    texture_rects[stage],
                });
                address_mask &= ~stage_mask;
                format_mask &= ~stage_mask;
                rect_mask &= ~stage_mask;
            }
            continue;
        }
        if (method == B2R_RESOURCE_RANGE_START_EVENT) {
            range_start = value;
            continue;
        }
        if (method == B2R_RESOURCE_RANGE_END_EVENT && range_start != 0u &&
            value > range_start) {
            b2r_live_append_resource(
                resources, 0u, range_start, value - range_start, 1u,
                value - range_start, "VERTEX_BUFFER", state);
            range_start = 0u;
        }
    }
    for (const B2RLiveTextureBinding& binding : texture_bindings) {
        const uint32_t format_raw = binding.format;
        const uint32_t color_format = (format_raw >> 8u) & 0xffu;
        const char* format = nullptr;
        char unknown_format[16]{};
        switch (color_format) {
        case 0x05u: format = "R5G6B5"; break;
        case 0x06u: format = "A8R8G8B8"; break;
        case 0x07u: format = "X8R8G8B8"; break;
        case 0x0cu: format = "DXT1"; break;
        case 0x0eu: format = "DXT3"; break;
        case 0x0fu: format = "DXT5"; break;
        case 0x12u: format = "A8R8G8B8_LINEAR"; break;
        case 0x1eu: format = "X8R8G8B8_LINEAR"; break;
        default:
            std::snprintf(
                unknown_format, sizeof(unknown_format),
                "format_%02X", color_format);
            format = unknown_format;
            break;
        }
        const bool linear = color_format == 0x12u || color_format == 0x1eu;
        const uint32_t image_rect = binding.image_rect;
        uint32_t width = linear && image_rect != 0u
            ? image_rect >> 16u : 1u << ((format_raw >> 20u) & 0xfu);
        uint32_t height = linear && image_rect != 0u
            ? image_rect & 0xffffu : 1u << ((format_raw >> 24u) & 0xfu);
        uint32_t levels = std::max(1u, (format_raw >> 16u) & 0xfu);
        if (linear) {
            levels = 1u;
        } else {
            uint32_t maximum = std::max(width, height);
            uint32_t maximum_levels = 0u;
            do { ++maximum_levels; maximum >>= 1u; } while (maximum != 0u);
            levels = std::min(levels, maximum_levels);
        }
        uint64_t byte_count = 0u;
        uint32_t mip_width = width;
        uint32_t mip_height = height;
        for (uint32_t level = 0u; level < levels; ++level) {
            if (color_format == 0x0cu) {
                byte_count += std::max<uint64_t>(
                    8u, static_cast<uint64_t>((mip_width + 3u) / 4u) *
                        ((mip_height + 3u) / 4u) * 8u);
            } else if (color_format == 0x0eu || color_format == 0x0fu) {
                byte_count += std::max<uint64_t>(
                    16u, static_cast<uint64_t>((mip_width + 3u) / 4u) *
                        ((mip_height + 3u) / 4u) * 16u);
            } else if (color_format == 0x05u) {
                byte_count += static_cast<uint64_t>(mip_width) *
                    mip_height * 2u;
            } else {
                byte_count += static_cast<uint64_t>(mip_width) *
                    mip_height * 4u;
            }
            mip_width = std::max(1u, mip_width / 2u);
            mip_height = std::max(1u, mip_height / 2u);
        }
        if (byte_count <= UINT32_MAX) {
            b2r_live_append_resource(
                resources, binding.stage, binding.address, width, height,
                static_cast<uint32_t>(byte_count), format, state);
        }
    }

    uint8_t* mapping = state->live_resource_mapping;
    const uint64_t slot_capacity = (state->live_resource_size - 64u) / 2u;
    const uint64_t generation = ++state->live_resource_generation;
    const uint32_t slot = static_cast<uint32_t>(generation & 1u);
    const uint32_t metadata_offset = slot == 0u ? 24u : 40u;
    const uint32_t sequence = b2r_live_read_u32(mapping, metadata_offset);
    const uint32_t publishing = (sequence + 1u) | 1u;
    const uint32_t published = publishing + 1u;
    b2r_live_write_u32(mapping, metadata_offset, publishing);
    b2r_live_write_u64(mapping, metadata_offset + 8u, generation);
    uint8_t* output = mapping + 64u + slot * slot_capacity;
    std::memcpy(output, "B2TEX001", 8u);
    b2r_live_write_u32(
        output, 8u, static_cast<uint32_t>(resources.size()));
    uint64_t payload_size = 12u;
    for (const B2RLiveResourceDescriptor& resource : resources) {
        const uint32_t format_size = static_cast<uint32_t>(
            std::strlen(resource.format));
        const uint64_t required = 56u + format_size + resource.byte_count;
        if (payload_size > slot_capacity || required > slot_capacity - payload_size) {
            state->normal_runtime_failure_code = 3u;
            return false;
        }
        uint8_t hash[32]{};
        B2RSha256 sha;
        b2r_live_read_resource_bytes(
            state, resource.source_address, nullptr,
            resource.byte_count, &sha);
        sha.finish(hash);
        uint8_t* header = output + payload_size;
        b2r_live_write_u32(header, 0u, resource.stage);
        b2r_live_write_u32(header, 4u, resource.address);
        b2r_live_write_u32(header, 8u, resource.width);
        b2r_live_write_u32(header, 12u, resource.height);
        b2r_live_write_u32(header, 16u, format_size);
        b2r_live_write_u32(header, 20u, resource.byte_count);
        std::memcpy(header + 24u, hash, sizeof(hash));
        std::memcpy(header + 56u, resource.format, format_size);
        b2r_live_read_resource_bytes(
            state, resource.source_address,
            header + 56u + format_size, resource.byte_count, nullptr);
        payload_size += required;
    }
    b2r_live_write_u32(
        mapping, metadata_offset + 4u, static_cast<uint32_t>(payload_size));
    MemoryBarrier();
    b2r_live_write_u32(mapping, metadata_offset, published);
    state->live_resource_slot = slot;
    state->live_resource_payload_size = static_cast<uint32_t>(payload_size);
    ++state->live_resource_publish_count;
    return true;
}

static bool b2r_live_publish_manifest(
    B2RNativeHostServiceState* state,
    B2RContext* context
) {
    uint8_t payload[4096]{};
    std::memcpy(payload, "B2MAN001", 8u);
    b2r_live_write_u32(payload, 8u, 1u);
    uint32_t record_count = 0u;
    uint32_t size = 16u;
    auto append = [&](uint16_t field, uint8_t kind,
                      const void* value, uint32_t value_size) {
        if (size + 8u + value_size > sizeof(payload)) { return false; }
        std::memcpy(payload + size, &field, sizeof(field));
        payload[size + 2u] = kind;
        payload[size + 3u] = 0u;
        b2r_live_write_u32(payload, size + 4u, value_size);
        std::memcpy(payload + size + 8u, value, value_size);
        size += 8u + value_size;
        ++record_count;
        return true;
    };
    auto append_u64 = [&](uint16_t field, uint64_t value) {
        return append(field, 1u, &value, sizeof(value));
    };
    auto append_bool = [&](uint16_t field, bool value) {
        const uint8_t encoded = value ? 1u : 0u;
        return append(field, 2u, &encoded, sizeof(encoded));
    };
    auto append_string = [&](uint16_t field, const char* value) {
        return append(
            field, 3u, value, static_cast<uint32_t>(std::strlen(value)));
    };
    char presentation_event_name[64]{};
    char publication_event_name[64]{};
    std::snprintf(
        presentation_event_name, sizeof(presentation_event_name),
        "b2_recomp_presented_%016llX",
        static_cast<unsigned long long>(state->command_stream_generation));
    std::snprintf(
        publication_event_name, sizeof(publication_event_name),
        "b2_recomp_published_%016llX",
        static_cast<unsigned long long>(state->command_stream_generation));
    if (!append_bool(1u, false) ||
        !append_string(2u, "shared-memory-resource-slot") ||
        !append_string(3u, "shared-memory-command-spans") ||
        (state->live_frontend_text_pending &&
         (!append_string(4u, state->live_frontend_text) ||
          !append_u64(5u, state->live_frontend_text_x_bits) ||
          !append_u64(6u, state->live_frontend_text_y_bits) ||
          !append_u64(7u, state->live_frontend_text_size_bits) ||
          !append_u64(8u, state->live_frontend_text_color_argb))) ||
        !append_u64(10u, state->live_published_record_count) ||
        !append_u64(17u, state->live_flip_count) ||
        !append_u64(18u, context->steps) ||
        !append_u64(21u, state->live_published_record_count) ||
        !append_u64(22u, state->live_published_record_count) ||
        !append_u64(23u, 0u) ||
        !append_string(24u, "shared_memory_span_v1") ||
        !append_u64(25u, state->live_published_byte_count) ||
        !append_u64(26u, 0u) ||
        !append_u64(27u, state->live_resource_generation) ||
        !append_u64(28u, state->command_stream_generation) ||
        !append_string(30u, presentation_event_name) ||
        !append_string(31u, publication_event_name) ||
        !append_u64(32u, state->live_resource_slot) ||
        !append_u64(33u, state->live_resource_payload_size) ||
        !append_string(34u, "shared_memory_slot_v1")) {
        state->normal_runtime_failure_code = 4u;
        return false;
    }
    b2r_live_write_u32(payload, 12u, record_count);
    uint8_t* mapping = state->live_control_mapping;
    const uint32_t sequence = b2r_live_read_u32(mapping, 256u);
    const uint32_t publishing = (sequence + 1u) | 1u;
    const uint32_t published = publishing + 1u;
    b2r_live_write_u32(mapping, 256u, publishing);
    b2r_live_write_u32(mapping, 260u, size);
    std::memcpy(mapping + 264u, payload, size);
    MemoryBarrier();
    b2r_live_write_u32(mapping, 256u, published);
    ++state->live_manifest_publish_count;
    if (state->publication_event_handle != 0u) {
        SetEvent(reinterpret_cast<HANDLE>(
            static_cast<uintptr_t>(state->publication_event_handle)));
    }
    return true;
}

static void b2r_live_pace_video_frame(B2RNativeHostServiceState* state) {
    if (state == nullptr) { return; }
    LARGE_INTEGER frequency{};
    LARGE_INTEGER now{};
    if (!QueryPerformanceFrequency(&frequency) || frequency.QuadPart <= 0 ||
        !QueryPerformanceCounter(&now)) {
        return;
    }
    const int64_t interval = std::max<int64_t>(
        1, (frequency.QuadPart + 30) / 60);
    if (state->live_next_video_frame_deadline_qpc == 0) {
        state->live_next_video_frame_deadline_qpc = now.QuadPart + interval;
        return;
    }
    const int64_t deadline = state->live_next_video_frame_deadline_qpc;
    if (now.QuadPart < deadline) {
        HANDLE timer = reinterpret_cast<HANDLE>(
            static_cast<uintptr_t>(state->live_video_frame_timer_handle));
        if (timer == nullptr) {
            timer = CreateWaitableTimerExW(
                nullptr, nullptr, 0x00000002u, TIMER_ALL_ACCESS);
            state->live_video_frame_timer_handle = static_cast<uint64_t>(
                reinterpret_cast<uintptr_t>(timer));
        }
        const int64_t remaining_ticks = deadline - now.QuadPart;
        const int64_t remaining_100ns = std::max<int64_t>(
            1,
            (remaining_ticks * 10000000ll + frequency.QuadPart - 1) /
                frequency.QuadPart);
        LARGE_INTEGER due_time{};
        due_time.QuadPart = -remaining_100ns;
        const int64_t wait_started = now.QuadPart;
        if (timer != nullptr &&
            SetWaitableTimerEx(
                timer, &due_time, 0, nullptr, nullptr, nullptr, 0)) {
            WaitForSingleObject(timer, INFINITE);
        } else {
            Sleep(static_cast<DWORD>(std::max<int64_t>(
                1, (remaining_100ns + 9999) / 10000)));
        }
        QueryPerformanceCounter(&now);
        ++state->live_video_pacing_wait_count;
        state->live_video_pacing_wait_microseconds += static_cast<uint64_t>(
            std::max<int64_t>(0, now.QuadPart - wait_started) * 1000000ll /
            frequency.QuadPart);
    }
    int64_t next_deadline = deadline + interval;
    if (next_deadline <= now.QuadPart) {
        next_deadline = now.QuadPart + interval;
    }
    state->live_next_video_frame_deadline_qpc = next_deadline;
}

static bool b2r_live_wait_for_presentation(
    B2RNativeHostServiceState* state
) {
    ++state->live_presentation_wait_count;
    const uint64_t started = GetTickCount64();
    for (;;) {
        if (b2r_live_read_u32(state->live_control_mapping, 32u) != 0u) {
            state->normal_runtime_stop_requested = true;
            return false;
        }
        const uint32_t sequence = b2r_live_read_u32(
            state->live_control_mapping, 128u);
        if (sequence != 0u && (sequence & 1u) == 0u) {
            const uint64_t generation = b2r_live_read_u64(
                state->live_control_mapping, 136u);
            const uint64_t flip = b2r_live_read_u64(
                state->live_control_mapping, 144u);
            const uint32_t confirmed = b2r_live_read_u32(
                state->live_control_mapping, 128u);
            if (confirmed == sequence && (confirmed & 1u) == 0u &&
                generation == state->command_stream_generation &&
                flip >= state->live_flip_count) {
                state->live_presentation_wait_milliseconds +=
                    GetTickCount64() - started;
                return true;
            }
        }
        if (GetTickCount64() - started >= 120000u) {
            state->normal_runtime_failure_code = 5u;
            return false;
        }
        if (state->presentation_event_handle != 0u) {
            WaitForSingleObject(reinterpret_cast<HANDLE>(
                static_cast<uintptr_t>(state->presentation_event_handle)), 10u);
        } else {
            Sleep(1u);
        }
    }
}

static uint32_t b2r_service_native_normal_runtime(
    B2RNativeHostServiceState* state,
    B2RContext* context
) {
    if (state == nullptr || context == nullptr) { return 0u; }
    auto release_direct_observed_spans = [&]() {
        context->direct_observed_payload_size = 0u;
        context->direct_observed_span_count = 0u;
        context->direct_observed_span_sealed = false;
        context->yield_requested = false;
    };
    b2r_refresh_live_controller(state);
    constexpr uint32_t kD3dContextGlobal = 0x002256b8u;
    constexpr uint32_t kD3dPutOffset = 0x2cu;
    constexpr uint32_t kD3dGetPointerOffset = 0x30u;
    uint32_t d3d_context = 0u;
    uint32_t d3d_put = 0u;
    uint32_t d3d_get_pointer = 0u;
    uint32_t d3d_get = 0u;
    if (b2r_native_service_try_read_u32(
            state, kD3dContextGlobal, &d3d_context) &&
        d3d_context != 0u &&
        b2r_native_service_try_read_u32(
            state, d3d_context + kD3dPutOffset, &d3d_put) &&
        b2r_native_service_try_read_u32(
            state, d3d_context + kD3dGetPointerOffset,
            &d3d_get_pointer) &&
        d3d_get_pointer != 0u &&
        b2r_native_service_try_read_u32(
            state, d3d_get_pointer, &d3d_get) &&
        d3d_get != d3d_put) {
        // The title's raw D3D free-space helper polls the dynamically
        // allocated GET pointer. Keep it consistent with the packet/reserve
        // fast paths, which publish consumed work by advancing GET to PUT.
        b2r_native_service_write_u32(state, d3d_get_pointer, d3d_put);
        ++state->native_gpu_get_pointer_sync_count;
    }
    uint32_t d3d_ring_start = 0u;
    uint32_t d3d_ring_end = 0u;
    if (d3d_context != 0u &&
        b2r_native_service_try_read_u32(
            state, d3d_context + 0x24u, &d3d_ring_start) &&
        b2r_native_service_try_read_u32(
            state, d3d_context + 0x28u, &d3d_ring_end) &&
        d3d_ring_start != 0u && d3d_ring_end > d3d_ring_start &&
        d3d_ring_end - d3d_ring_start <= 0x01000000u &&
        (context->observed_write_range_start != d3d_ring_start ||
         context->observed_write_range_end != d3d_ring_end)) {
        // Python used to refresh this range between slices. The resident
        // dispatcher must follow the title's real ring after D3D creates it.
        context->observed_write_range_start = d3d_ring_start;
        context->observed_write_range_end = d3d_ring_end;
        ++state->native_gpu_observed_range_sync_count;
    }
    b2r_schedule_native_d3d_vblank(state, d3d_context);
    if (state->live_control_mapping == nullptr) { return 0u; }
    if (!b2r_live_has_header(
            state->live_control_mapping, state->live_control_size,
            "B2LIV001", 65536u) ||
        !b2r_live_has_header(
            state->live_command_mapping, state->live_command_size,
            "B2CMD001", 64u * 1024u * 1024u) ||
        !b2r_live_has_header(
            state->live_resource_mapping, state->live_resource_size,
            "B2RES001", 64u * 1024u * 1024u)) {
        state->normal_runtime_failure_code = 1u;
        release_direct_observed_spans();
        return 2u;
    }
    if (b2r_live_read_u32(state->live_control_mapping, 32u) != 0u) {
        state->normal_runtime_stop_requested = true;
        release_direct_observed_spans();
        return 1u;
    }
    B2RNativeNormalRuntimeState* runtime =
        reinterpret_cast<B2RNativeNormalRuntimeState*>(
            static_cast<uintptr_t>(state->normal_runtime_opaque));
    if (runtime == nullptr && context->direct_observed_span_count != 0u) {
        // Rendering and audio share this resident state. Always use the
        // common allocator so an object first created by a render flip has
        // the host back-pointer required when audio starts later.
        runtime = b2r_get_normal_runtime_state(state);
        if (runtime == nullptr) {
            state->normal_runtime_failure_code = 7u;
            release_direct_observed_spans();
            return 2u;
        }
    }
    for (uint32_t index = 0u;
         index < context->direct_observed_span_count; ++index) {
        const uint32_t offset = context->direct_observed_span_payload_offsets[index];
        const uint32_t payload_size =
            context->direct_observed_span_payload_sizes[index];
        const uint32_t write_count =
            context->direct_observed_span_write_counts[index];
        const uint8_t flags = context->direct_observed_span_flags[index];
        if (payload_size == 0u || write_count == 0u || (flags & ~1u) != 0u ||
            offset > context->direct_observed_payload_size ||
            payload_size > context->direct_observed_payload_size - offset) {
            state->normal_runtime_failure_code = 6u;
            release_direct_observed_spans();
            return 2u;
        }
        const uint32_t guest_address = b2r_native_service_cache_address(
            state, context->direct_observed_span_addresses[index]);
        const uint32_t observed_start = b2r_native_service_cache_address(
            state, context->observed_write_range_start);
        const uint32_t observed_end = b2r_native_service_cache_address(
            state, context->observed_write_range_end);
        const uint64_t guest_end =
            static_cast<uint64_t>(guest_address) + payload_size;
        if (observed_start >= observed_end || guest_address < observed_start ||
            guest_end > observed_end ||
            observed_end - observed_start > 0x01000000u) {
            state->normal_runtime_failure_code = 6u;
            release_direct_observed_spans();
            return 2u;
        }
        // The renderer's command contract uses one stable 16 MiB aperture.
        // D3D replaces the boot ring with a title-RAM allocation; publishing
        // that guest address directly makes every otherwise-valid span fall
        // outside the renderer's 0x80000000 aperture and yields zero methods.
        const uint32_t address =
            0x80000000u + (guest_address - observed_start);
        const uint32_t local_offset = 0u;
        uint32_t resource_output_count = 0u;
        uint64_t resource_data_words = 0u;
        uint64_t resource_tracked_words = 0u;
        uint64_t resource_skipped_words = 0u;
        uint64_t resource_aggregated_index_words = 0u;
        if (!b2r_scan_resource_method_spans(
                &runtime->resource_scan,
                context->direct_observed_payload + offset,
                payload_size,
                &address,
                &local_offset,
                &payload_size,
                &flags,
                1u,
                false,
                runtime->methods.data(),
                runtime->values.data(),
                runtime->positions.data(),
                B2R_RESOURCE_SCAN_OUTPUT_CAPACITY,
                &resource_output_count,
                &resource_data_words,
                &resource_tracked_words,
                &resource_skipped_words,
                &resource_aggregated_index_words)) {
            state->normal_runtime_failure_code = 8u;
            release_direct_observed_spans();
            return 2u;
        }
        std::vector<uint8_t> record(16u + payload_size);
        record[0] = 1u;
        record[1] = flags;
        std::memcpy(record.data() + 4u, &address, sizeof(address));
        std::memcpy(record.data() + 8u, &payload_size, sizeof(payload_size));
        std::memcpy(record.data() + 12u, &write_count, sizeof(write_count));
        std::memcpy(
            record.data() + 16u,
            context->direct_observed_payload + offset,
            payload_size);
        if (!b2r_live_ring_write(
                state, record.data(), static_cast<uint32_t>(record.size()))) {
            release_direct_observed_spans();
            return 2u;
        }
        state->live_published_record_count += write_count;
        state->live_published_byte_count += record.size();
        ++state->live_published_span_count;
        if ((flags & 1u) != 0u) {
            ++state->live_flip_count;
            b2r_live_pace_video_frame(state);
            if (!b2r_live_publish_resources(
                    state, runtime->methods.data(), runtime->values.data(),
                    resource_output_count) ||
                !b2r_live_publish_manifest(state, context) ||
                !b2r_live_wait_for_presentation(state)) {
                release_direct_observed_spans();
                return state->normal_runtime_stop_requested ? 1u : 2u;
            }
            state->live_frontend_text_pending = false;
        }
    }
    release_direct_observed_spans();
    return 0u;
}

static void b2r_shutdown_native_normal_runtime(
    B2RNativeHostServiceState* state
) {
    if (state == nullptr) { return; }
    B2RContext* vblank_context = reinterpret_cast<B2RContext*>(
        static_cast<uintptr_t>(
            state->native_d3d_vblank_callback_context));
    delete vblank_context;
    state->native_d3d_vblank_callback_context = 0u;
    state->native_d3d_vblank_callback_pending = false;
    state->native_d3d_vblank_next_deadline_qpc = 0;
    B2RNativeNormalRuntimeState* runtime =
        reinterpret_cast<B2RNativeNormalRuntimeState*>(
            static_cast<uintptr_t>(state->normal_runtime_opaque));
    if (runtime != nullptr) {
        {
            std::lock_guard<std::mutex> lock(runtime->audio_mutex);
            runtime->audio_shutdown = true;
            runtime->audio_condition.notify_all();
        }
        if (runtime->audio_thread.joinable()) {
            runtime->audio_thread.join();
        }
        if (runtime->audio_close != nullptr) {
            runtime->audio_close();
        }
        if (runtime->audio_library != nullptr) {
            FreeLibrary(runtime->audio_library);
        }
    }
    delete runtime;
    state->normal_runtime_opaque = 0u;
    HANDLE video_frame_timer = reinterpret_cast<HANDLE>(
        static_cast<uintptr_t>(state->live_video_frame_timer_handle));
    if (video_frame_timer != nullptr) {
        CloseHandle(video_frame_timer);
        state->live_video_frame_timer_handle = 0u;
    }
    state->native_audio_output_open = false;
    state->native_audio_looping = false;
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


class _ResourceSpanScanState(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint32)
        for name in (
            "next_address",
            "partial_word",
            "partial_size",
            "run_active",
            "pending_long_header",
            "pending_packet",
            "non_increasing",
            "first_method",
            "remaining",
            "method_index",
            "index_packet",
            "index_maximum",
            "index_has_data",
        )
    ] + [
        ("texture_offsets", ctypes.c_uint32 * 4),
        ("texture_formats", ctypes.c_uint32 * 4),
        ("texture_image_rects", ctypes.c_uint32 * 4),
        ("texture_offset_mask", ctypes.c_uint32),
        ("texture_format_mask", ctypes.c_uint32),
        ("texture_image_rect_mask", ctypes.c_uint32),
        ("vertex_array_offsets", ctypes.c_uint32 * 16),
        ("vertex_array_formats", ctypes.c_uint32 * 16),
        ("vertex_array_offset_mask", ctypes.c_uint32),
        ("vertex_array_format_mask", ctypes.c_uint32),
        ("active_vertex_primitive", ctypes.c_uint32),
        ("active_vertex_max_index", ctypes.c_uint32),
        ("active_vertex_has_index", ctypes.c_uint32),
        ("frame_binding_count", ctypes.c_uint32),
        ("frame_binding_stages", ctypes.c_uint32 * 8192),
        ("frame_binding_addresses", ctypes.c_uint32 * 8192),
        ("frame_binding_formats", ctypes.c_uint32 * 8192),
        ("frame_binding_image_rects", ctypes.c_uint32 * 8192),
        ("frame_range_count", ctypes.c_uint32),
        ("frame_range_starts", ctypes.c_uint32 * 1024),
        ("frame_range_ends", ctypes.c_uint32 * 1024),
    ]


class NativeCooperativeSchedulerState(ctypes.Structure):
    _fields_ = [
        ("last_steps", ctypes.c_uint64),
        ("pending_steps", ctypes.c_uint64),
        ("last_serviced_flip", ctypes.c_uint64),
    ]


_ReadU32 = ctypes.CFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32)
_WriteU32 = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32)
_ReadU8 = ctypes.CFUNCTYPE(ctypes.c_uint8, ctypes.c_void_p, ctypes.c_uint32)
_WriteU8 = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint8)
_Call = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_Context))
_HostCall = ctypes.CFUNCTYPE(
    ctypes.c_uint32,
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.POINTER(_Context),
)
_Observe = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.POINTER(_Context))

NATIVE_HOST_SERVICE_RETURN_CONSTANT = 1
NATIVE_HOST_SERVICE_PERFORMANCE_COUNTER = 2
NATIVE_HOST_SERVICE_SYSTEM_TIME = 3
NATIVE_HOST_SERVICE_XGETDEVICES = 4
NATIVE_HOST_SERVICE_XINPUT_OPEN = 5
NATIVE_HOST_SERVICE_XINPUT_CAPABILITIES = 6
NATIVE_HOST_SERVICE_XINPUT_STATE = 7
NATIVE_HOST_SERVICE_COLD_CALLBACK = 8
NATIVE_HOST_SERVICE_IRQL = 9
NATIVE_HOST_SERVICE_SEMAPHORE = 10
NATIVE_HOST_SERVICE_RUNTIME = 11
NATIVE_HOST_SERVICE_MEMORY = 12
NATIVE_HOST_SERVICE_AUDIO = 13
NATIVE_HOST_SERVICE_TITLE = 14

NATIVE_HOST_SERVICE_IRQL_RAISE = 1
NATIVE_HOST_SERVICE_IRQL_LOWER = 2
NATIVE_HOST_SERVICE_IRQL_RAISE_DPC = 3
NATIVE_HOST_SERVICE_IRQL_RAISE_SYNCH = 4
NATIVE_HOST_SERVICE_IRQL_GET_CURRENT = 5

NATIVE_HOST_SERVICE_SEMAPHORE_RELEASE = 1
NATIVE_HOST_SERVICE_SEMAPHORE_WAIT = 2
NATIVE_HOST_SERVICE_SEMAPHORE_KERNEL_WAIT = 3

NATIVE_HOST_SERVICE_RUNTIME_INIT_ANSI_STRING = 1
NATIVE_HOST_SERVICE_RUNTIME_EQUAL_STRING = 2
NATIVE_HOST_SERVICE_RUNTIME_MISSING_NON_VOLATILE_SETTING = 3
NATIVE_HOST_SERVICE_RUNTIME_NT_STATUS_TO_DOS_ERROR = 4
NATIVE_HOST_SERVICE_RUNTIME_PRESERVE_RETURN = 5
NATIVE_HOST_SERVICE_RUNTIME_DELAY_THREAD = 6
NATIVE_HOST_SERVICE_RUNTIME_ANSI_STRING_TO_UNICODE_STRING = 7
NATIVE_HOST_SERVICE_RUNTIME_UNICODE_STRING_TO_ANSI_STRING = 8

NATIVE_HOST_SERVICE_AUDIO_DIRECTSOUND_EFFECT_IMAGE = 1
NATIVE_HOST_SERVICE_AUDIO_BUFFER_SET_DATA = 2
NATIVE_HOST_SERVICE_AUDIO_BUFFER_SET_FORMAT = 3
NATIVE_HOST_SERVICE_AUDIO_BUFFER_PLAY = 4
NATIVE_HOST_SERVICE_AUDIO_BUFFER_STOP = 5
NATIVE_HOST_SERVICE_AUDIO_BUFFER_STOP_EX = 6
NATIVE_HOST_SERVICE_AUDIO_STREAM_CREATE = 7
NATIVE_HOST_SERVICE_AUDIO_STREAM_PROCESS = 8
NATIVE_HOST_SERVICE_AUDIO_STREAM_FLUSH = 9
NATIVE_HOST_SERVICE_AUDIO_STREAM_SET_FORMAT = 10
NATIVE_HOST_SERVICE_AUDIO_BUFFER_CREATE = 11
NATIVE_HOST_SERVICE_AUDIO_PASSTHROUGH = 12
NATIVE_HOST_SERVICE_AUDIO_BUFFER_SET_VOLUME = 13
NATIVE_HOST_SERVICE_AUDIO_BUFFER_SET_FREQUENCY = 14
NATIVE_HOST_SERVICE_AUDIO_BUFFER_GET_POSITION = 15
NATIVE_HOST_SERVICE_AUDIO_BUFFER_SET_POSITION = 16

NATIVE_HOST_SERVICE_TITLE_HEAP_ALLOCATE = 1
NATIVE_HOST_SERVICE_TITLE_HEAP_FREE = 2
NATIVE_HOST_SERVICE_TITLE_ALLOCATION_LIST_COUNT = 3
NATIVE_HOST_SERVICE_TITLE_GLOBAL_LIST_REGISTER = 4
NATIVE_HOST_SERVICE_TITLE_TEXT_DRAW = 5
NATIVE_HOST_SERVICE_TITLE_ASSET_ACTIVATE = 6
NATIVE_HOST_SERVICE_TITLE_ASSET_STATUS = 7
NATIVE_HOST_SERVICE_TITLE_ASSET_READ = 8
NATIVE_HOST_SERVICE_TITLE_ASSET_SEEK = 9
NATIVE_HOST_SERVICE_TITLE_STATIC_DRIVE_SETUP = 10
NATIVE_HOST_SERVICE_TITLE_D3D_FLUSH = 11
NATIVE_HOST_SERVICE_TITLE_SPIN_DELAY = 12
NATIVE_HOST_SERVICE_TITLE_ASSET_OPEN = 13
NATIVE_HOST_SERVICE_TITLE_FRONTEND_SPECIAL_AUDIO_CREATE = 14
NATIVE_HOST_SERVICE_TITLE_MUSIC_MODE_SET = 15

NATIVE_HOST_SERVICE_BOOTSTRAP = 15
NATIVE_HOST_SERVICE_BOOTSTRAP_AV_SEND_TV_ENCODER_OPTION = 1
NATIVE_HOST_SERVICE_BOOTSTRAP_HAL_GET_INTERRUPT_VECTOR = 2
NATIVE_HOST_SERVICE_BOOTSTRAP_HAL_READ_WRITE_PCI_SPACE = 3
NATIVE_HOST_SERVICE_BOOTSTRAP_IO_CREATE_DEVICE = 4
NATIVE_HOST_SERVICE_BOOTSTRAP_KE_CONNECT_INTERRUPT = 5
NATIVE_HOST_SERVICE_BOOTSTRAP_KE_INITIALIZE_INTERRUPT = 6
NATIVE_HOST_SERVICE_BOOTSTRAP_KE_STALL_EXECUTION_PROCESSOR = 7
NATIVE_HOST_SERVICE_BOOTSTRAP_MM_CLAIM_GPU_INSTANCE_MEMORY = 8
NATIVE_HOST_SERVICE_BOOTSTRAP_NT_CLOSE = 9
NATIVE_HOST_SERVICE_BOOTSTRAP_KE_SET_EVENT = 10
NATIVE_HOST_SERVICE_BOOTSTRAP_NT_CREATE_FILE = 11
NATIVE_HOST_SERVICE_BOOTSTRAP_NT_OPEN_FILE = 12
NATIVE_HOST_SERVICE_BOOTSTRAP_NT_OPEN_SYMBOLIC_LINK_OBJECT = 13
NATIVE_HOST_SERVICE_BOOTSTRAP_NT_QUERY_INFORMATION_FILE = 14
NATIVE_HOST_SERVICE_BOOTSTRAP_NT_QUERY_SYMBOLIC_LINK_OBJECT = 15
NATIVE_HOST_SERVICE_BOOTSTRAP_NT_QUERY_VOLUME_INFORMATION_FILE = 16
NATIVE_HOST_SERVICE_BOOTSTRAP_AV_GET_SAVED_DATA_ADDRESS = 17
NATIVE_HOST_SERVICE_BOOTSTRAP_AV_SET_DISPLAY_MODE = 18
NATIVE_HOST_SERVICE_BOOTSTRAP_AV_SET_SAVED_DATA_ADDRESS = 19
NATIVE_HOST_SERVICE_BOOTSTRAP_NT_READ_FILE = 20
NATIVE_HOST_SERVICE_BOOTSTRAP_NT_SET_INFORMATION_FILE = 21
NATIVE_HOST_SERVICE_BOOTSTRAP_NT_WRITE_FILE = 22
NATIVE_HOST_SERVICE_BOOTSTRAP_NT_QUERY_DIRECTORY_FILE = 23
NATIVE_HOST_SERVICE_BOOTSTRAP_XC_SHA_INIT = 24
NATIVE_HOST_SERVICE_BOOTSTRAP_XC_SHA_UPDATE = 25
NATIVE_HOST_SERVICE_BOOTSTRAP_XC_SHA_FINAL = 26
NATIVE_HOST_SERVICE_BOOTSTRAP_XC_RC4_KEY = 27
NATIVE_HOST_SERVICE_BOOTSTRAP_XC_RC4_CRYPT = 28
NATIVE_HOST_SERVICE_BOOTSTRAP_XC_HMAC = 29
NATIVE_HOST_SERVICE_BOOTSTRAP_RTL_TIME_FIELDS_TO_TIME = 30
NATIVE_HOST_SERVICE_BOOTSTRAP_RTL_TIME_TO_TIME_FIELDS = 31
NATIVE_HOST_SERVICE_BOOTSTRAP_NT_DEVICE_IO_CONTROL_FILE = 32
NATIVE_HOST_SERVICE_BOOTSTRAP_NT_FS_CONTROL_FILE = 33

NATIVE_HOST_SERVICE_MEMORY_ALLOCATE_CONTIGUOUS_EX = 1
NATIVE_HOST_SERVICE_MEMORY_ALLOCATE_VIRTUAL = 2
NATIVE_HOST_SERVICE_MEMORY_ALLOCATE_CONTIGUOUS = 3
NATIVE_HOST_SERVICE_MEMORY_GET_PHYSICAL_ADDRESS = 4
NATIVE_HOST_SERVICE_MEMORY_QUERY_ALLOCATION_SIZE = 5
NATIVE_HOST_SERVICE_MEMORY_FREE = 6
NATIVE_HOST_SERVICE_MEMORY_VALIDATE_RANGE = 7
NATIVE_HOST_SERVICE_MEMORY_ALLOCATE_POOL = 8

NATIVE_HOST_SERVICE_LIFECYCLE_NONE = 0
NATIVE_HOST_SERVICE_LIFECYCLE_CREATE_WORKER = 1
NATIVE_HOST_SERVICE_LIFECYCLE_TERMINATE_WORKER = 2
NATIVE_HOST_SERVICE_LIFECYCLE_SUSPEND_WORKER = 3
NATIVE_HOST_SERVICE_LIFECYCLE_RESUME_WORKER = 4
NATIVE_HOST_SERVICE_LIFECYCLE_CREATE_SEMAPHORE = 5
NATIVE_HOST_SERVICE_LIFECYCLE_REFERENCE_WORKER = 6
NATIVE_HOST_SERVICE_LIFECYCLE_SET_BASE_PRIORITY = 7
NATIVE_HOST_SERVICE_LIFECYCLE_SET_PRIORITY = 8
NATIVE_HOST_SERVICE_LIFECYCLE_SET_DISABLE_BOOST = 9

NATIVE_WORKER_READY = 1
NATIVE_WORKER_RUNNING = 2
NATIVE_WORKER_WAITING = 3
NATIVE_WORKER_COMPLETED = 4
NATIVE_WORKER_BLOCKED = 5
NATIVE_WORKER_FAILED = 6
NATIVE_WORKER_SUSPENDED = 7
NATIVE_WORKER_LIFECYCLE_CAPACITY = 64


class NativeHostServiceEntry(ctypes.Structure):
    _fields_ = [
        ("target", ctypes.c_uint32),
        ("kind", ctypes.c_uint32),
        ("stack_cleanup_bytes", ctypes.c_uint32),
        ("value", ctypes.c_uint32),
    ]


class _NativeControllerState(ctypes.Structure):
    _fields_ = [
        ("buttons", ctypes.c_uint16),
        ("left_trigger", ctypes.c_uint8),
        ("right_trigger", ctypes.c_uint8),
        ("thumb_lx", ctypes.c_int16),
        ("thumb_ly", ctypes.c_int16),
        ("thumb_rx", ctypes.c_int16),
        ("thumb_ry", ctypes.c_int16),
        ("connected", ctypes.c_uint8),
        ("reserved", ctypes.c_uint8 * 3),
        ("generation", ctypes.c_uint32),
    ]


class _NativeSemaphoreEntry(ctypes.Structure):
    _fields_ = [
        ("handle", ctypes.c_uint32),
        ("count", ctypes.c_uint32),
        ("limit", ctypes.c_uint32),
    ]


class _NativeAllocationEntry(ctypes.Structure):
    _fields_ = [
        ("address", ctypes.c_uint32),
        ("size", ctypes.c_uint32),
        ("kind", ctypes.c_uint32),
        ("active", ctypes.c_uint32),
    ]


class _NativeFileEntry(ctypes.Structure):
    _fields_ = [
        ("handle", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("size", ctypes.c_uint64),
        ("position", ctypes.c_uint64),
        ("host_file", ctypes.c_uint64),
        ("active", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
        ("directory_index", ctypes.c_uint32),
        ("directory_initialized", ctypes.c_uint32),
        ("guest_path", ctypes.c_char * 256),
        ("host_path", ctypes.c_char * 512),
        ("directory_pattern", ctypes.c_char * 256),
    ]


class _NativeTitleAssetStreamEntry(ctypes.Structure):
    _fields_ = [
        ("object", ctypes.c_uint32),
        ("payload", ctypes.c_uint32),
        ("payload_size", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("image_base", ctypes.c_uint32),
    ]


class _NativeTrafficMeshSample(ctypes.Structure):
    _fields_ = [
        ("caller", ctypes.c_uint32),
        ("owner", ctypes.c_uint32),
        ("owner_mode", ctypes.c_uint32),
        ("mesh_entry", ctypes.c_uint32),
        ("index_data", ctypes.c_uint32),
        ("index_count", ctypes.c_uint32),
    ]


class _NativeWorkerExecutionSlot(ctypes.Structure):
    _fields_ = [
        ("handle", ctypes.c_uint32),
        ("return_sentinel", ctypes.c_uint32),
        ("context", ctypes.c_uint64),
        ("run_count", ctypes.c_uint64),
        ("step_count", ctypes.c_uint64),
        ("last_target", ctypes.c_uint32),
        ("last_reason", ctypes.c_uint32),
    ]


class _NativePageBufferMap(dict[int, ctypes.Array]):
    """Adopt pages allocated by the native runtime without a Python callback."""

    def __init__(self, page_table: Any, generations: dict[int, int]) -> None:
        super().__init__()
        self._page_table = page_table
        self._generations = generations

    def _adopt(self, page: int) -> ctypes.Array | None:
        existing = dict.get(self, page)
        if existing is not None:
            return existing
        if not 0 <= page < len(self._page_table):
            return None
        pointer = self._page_table[page]
        if not pointer:
            return None
        buffer = (ctypes.c_uint8 * 4096).from_address(int(pointer))
        dict.__setitem__(self, page, buffer)
        self._generations.setdefault(page, 0)
        return buffer

    def __contains__(self, page: object) -> bool:
        return isinstance(page, int) and self._adopt(page) is not None

    def __getitem__(self, page: int) -> ctypes.Array:
        buffer = self._adopt(page)
        if buffer is None:
            raise KeyError(page)
        return buffer

    def get(
        self,
        page: int,
        default: ctypes.Array | None = None,
    ) -> ctypes.Array | None:
        return self._adopt(page) or default


class NativeWorkerLifecycleEntry(ctypes.Structure):
    _fields_ = [
        ("handle", ctypes.c_uint32),
        ("start_address", ctypes.c_uint32),
        ("start_context1", ctypes.c_uint32),
        ("start_context2", ctypes.c_uint32),
        ("status", ctypes.c_uint32),
        ("wait_handle", ctypes.c_uint32),
        ("generation", ctypes.c_uint32),
        ("priority", ctypes.c_int32),
        ("base_priority", ctypes.c_int32),
        ("disable_boost", ctypes.c_uint32),
    ]


class NativeWorkerLifecycleState(ctypes.Structure):
    _fields_ = [
        (
            "entries",
            NativeWorkerLifecycleEntry * NATIVE_WORKER_LIFECYCLE_CAPACITY,
        ),
        ("entry_count", ctypes.c_uint32),
        ("selection_cursor", ctypes.c_uint32),
        ("transition_count", ctypes.c_uint64),
        ("created_count", ctypes.c_uint64),
        ("resumed_count", ctypes.c_uint64),
        ("suspended_count", ctypes.c_uint64),
        ("completed_count", ctypes.c_uint64),
        ("overflow_count", ctypes.c_uint64),
    ]

    def entry_for_handle(self, handle: int) -> NativeWorkerLifecycleEntry | None:
        for index in range(int(self.entry_count)):
            entry = self.entries[index]
            if int(entry.handle) == int(handle):
                return entry
        return None

    def summary(self) -> dict[str, Any]:
        return {
            "backend": "native_dispatcher",
            "entry_count": int(self.entry_count),
            "transition_count": int(self.transition_count),
            "created_count": int(self.created_count),
            "resumed_count": int(self.resumed_count),
            "suspended_count": int(self.suspended_count),
            "completed_count": int(self.completed_count),
            "overflow_count": int(self.overflow_count),
        }


class NativeHostServiceState(ctypes.Structure):
    _fields_ = [
        ("entries", ctypes.POINTER(NativeHostServiceEntry)),
        ("entry_count", ctypes.c_uint32),
        ("eax_offset", ctypes.c_uint32),
        ("ecx_offset", ctypes.c_uint32),
        ("esp_offset", ctypes.c_uint32),
        ("eip_offset", ctypes.c_uint32),
        ("timestamp_counter_offset", ctypes.c_uint32),
        ("flags_offset", ctypes.c_uint32),
        ("performance_counter", ctypes.c_uint64),
        ("system_time_filetime", ctypes.c_uint64),
        ("controllers", _NativeControllerState * 4),
        ("controller_packets", ctypes.c_uint32 * 4),
        ("controller_last_generations", ctypes.c_uint32 * 4),
        ("service_call_counts", ctypes.c_uint64 * 16),
        ("native_call_count", ctypes.c_uint64),
        ("native_observer_count", ctypes.c_uint64),
        ("native_frontend_record_table_repair_count", ctypes.c_uint64),
        ("native_frontend_record_table_last_address", ctypes.c_uint32),
        ("native_frontend_record_table_last_count", ctypes.c_uint32),
        ("observed_button_mask", ctypes.c_uint32),
        ("a_pressed_poll_count", ctypes.c_uint32),
        ("successful_get_state_count", ctypes.c_uint32),
        ("current_irql", ctypes.c_uint32),
        ("semaphores", _NativeSemaphoreEntry * 64),
        ("semaphore_count", ctypes.c_uint32),
        ("semaphore_overflow_count", ctypes.c_uint32),
        ("allocations", _NativeAllocationEntry * 1024),
        ("allocation_count", ctypes.c_uint32),
        ("allocation_overflow_count", ctypes.c_uint32),
        ("next_pool_address", ctypes.c_uint32),
        ("next_contiguous_address", ctypes.c_uint32),
        ("native_allocated_page_count", ctypes.c_uint64),
        ("native_page_allocation_failure_count", ctypes.c_uint64),
        ("title_heap_next_address", ctypes.c_uint32),
        ("title_heap_allocation_count", ctypes.c_uint64),
        ("title_heap_free_count", ctypes.c_uint64),
        ("title_heap_bytes_allocated", ctypes.c_uint64),
        ("native_memory_write_count", ctypes.c_uint64),
        ("native_gpu_command_kick_count", ctypes.c_uint64),
        ("native_gpu_completion_count", ctypes.c_uint64),
        ("native_gpu_get_pointer_sync_count", ctypes.c_uint64),
        ("native_gpu_observed_range_sync_count", ctypes.c_uint64),
        ("native_d3d_vblank_next_deadline_qpc", ctypes.c_int64),
        ("native_d3d_vblank_tick_count", ctypes.c_uint64),
        ("native_d3d_vblank_callback_schedule_count", ctypes.c_uint64),
        ("native_d3d_vblank_callback_run_count", ctypes.c_uint64),
        ("native_d3d_vblank_callback_step_count", ctypes.c_uint64),
        ("native_d3d_vblank_callback_completion_count", ctypes.c_uint64),
        ("native_d3d_vblank_callback_failure_count", ctypes.c_uint64),
        ("native_d3d_vblank_callback_context", ctypes.c_uint64),
        ("native_d3d_vblank_sequence", ctypes.c_uint32),
        ("native_d3d_vblank_callback_address", ctypes.c_uint32),
        ("native_d3d_vblank_callback_pending", ctypes.c_bool),
        ("native_memory_read_count", ctypes.c_uint64),
        ("native_gpu_master_interrupt_count", ctypes.c_uint64),
        ("native_gpu_master_interrupt_pending", ctypes.c_uint32),
        ("native_page_miss_count", ctypes.c_uint64),
        ("extracted_root", ctypes.c_char * 1024),
        ("save_data_root", ctypes.c_char * 1024),
        ("dashboard_data_root", ctypes.c_char * 1024),
        ("cache_data_root", ctypes.c_char * 1024),
        ("title_id", ctypes.c_uint32),
        ("last_file_guest_path", ctypes.c_char * 512),
        ("last_file_host_path", ctypes.c_char * 1536),
        ("title_asset_stream_count", ctypes.c_uint32),
        ("title_asset_payload_next", ctypes.c_uint32),
        ("title_asset_open_count", ctypes.c_uint64),
        ("title_asset_open_failure_count", ctypes.c_uint64),
        ("title_asset_payload_bytes", ctypes.c_uint64),
        ("title_asset_streams", _NativeTitleAssetStreamEntry * 128),
        ("title_track_pss_candidate_count", ctypes.c_uint64),
        ("title_track_pss_publication_count", ctypes.c_uint64),
        ("title_track_pss_validation_failure_count", ctypes.c_uint64),
        ("title_track_pss_published_bytes", ctypes.c_uint64),
        ("title_track_pss_descriptor_entry_count", ctypes.c_uint64),
        ("title_track_pss_scene_record_entry_count", ctypes.c_uint64),
        ("title_traffic_tra_candidate_count", ctypes.c_uint64),
        ("av_saved_data_address", ctypes.c_uint32),
        ("av_display_mode_set_count", ctypes.c_uint32),
        ("files", _NativeFileEntry * 128),
        ("file_count", ctypes.c_uint32),
        ("file_overflow_count", ctypes.c_uint32),
        ("next_object_handle", ctypes.c_uint32),
        ("service_trace_count", ctypes.c_uint32),
        ("service_trace_overflow_count", ctypes.c_uint32),
        ("service_trace_targets", ctypes.c_uint32 * 4096),
        ("service_trace_kinds", ctypes.c_uint32 * 4096),
        ("service_trace_values", ctypes.c_uint32 * 4096),
        ("service_trace_results", ctypes.c_uint32 * 4096),
        ("service_trace_return_addresses", ctypes.c_uint32 * 4096),
        ("last_service_target", ctypes.c_uint32),
        ("last_service_kind", ctypes.c_uint32),
        ("last_service_value", ctypes.c_uint32),
        ("last_service_result", ctypes.c_uint32),
        ("last_service_return_address", ctypes.c_uint32),
        ("last_service_argument0", ctypes.c_uint32),
        ("last_service_worker_handle", ctypes.c_uint32),
        ("callback_address_keys", ctypes.POINTER(ctypes.c_uint32)),
        ("callback_address_mask", ctypes.c_uint32),
        ("write_callback_address_keys", ctypes.POINTER(ctypes.c_uint32)),
        ("write_callback_address_mask", ctypes.c_uint32),
        ("worker_lifecycle", ctypes.POINTER(NativeWorkerLifecycleState)),
        ("current_worker_handle", ctypes.c_uint32),
        ("yield_requested_offset", ctypes.c_uint32),
        ("cold_call", ctypes.c_void_p),
        ("cold_user", ctypes.c_void_p),
        ("read_pages", ctypes.POINTER(ctypes.c_void_p)),
        ("memory_user", ctypes.c_void_p),
        ("read_u32", ctypes.c_void_p),
        ("write_u32", ctypes.c_void_p),
        ("read_u8", ctypes.c_void_p),
        ("write_u8", ctypes.c_void_p),
        ("cache_physical_aliases", ctypes.c_bool),
        ("dirty_pages", ctypes.POINTER(ctypes.c_uint8)),
        ("dirty_page_generations", ctypes.POINTER(ctypes.c_uint32)),
        ("dirty_page_indices", ctypes.POINTER(ctypes.c_uint32)),
        ("dirty_page_min_offsets", ctypes.POINTER(ctypes.c_uint16)),
        ("dirty_page_max_offsets", ctypes.POINTER(ctypes.c_uint16)),
        ("dirty_page_count", ctypes.POINTER(ctypes.c_uint32)),
        ("dirty_page_capacity", ctypes.c_uint32),
        ("normal_runtime_enabled", ctypes.c_bool),
        ("scheduler_quantum", ctypes.c_uint64),
        ("scheduler_last_main_steps", ctypes.c_uint64),
        ("scheduler_service_count", ctypes.c_uint64),
        ("scheduler_worker_run_count", ctypes.c_uint64),
        ("scheduler_worker_step_count", ctypes.c_uint64),
        ("scheduler_worker_completion_count", ctypes.c_uint64),
        ("scheduler_worker_wait_count", ctypes.c_uint64),
        ("scheduler_worker_failure_count", ctypes.c_uint64),
        (
            "worker_execution",
            _NativeWorkerExecutionSlot * NATIVE_WORKER_LIFECYCLE_CAPACITY,
        ),
        ("live_control_mapping", ctypes.POINTER(ctypes.c_uint8)),
        ("live_control_size", ctypes.c_uint64),
        ("live_command_mapping", ctypes.POINTER(ctypes.c_uint8)),
        ("live_command_size", ctypes.c_uint64),
        ("live_resource_mapping", ctypes.POINTER(ctypes.c_uint8)),
        ("live_resource_size", ctypes.c_uint64),
        ("command_stream_generation", ctypes.c_uint64),
        ("live_command_write_cursor", ctypes.c_uint64),
        ("live_published_record_count", ctypes.c_uint64),
        ("live_published_byte_count", ctypes.c_uint64),
        ("live_published_span_count", ctypes.c_uint64),
        ("live_flip_count", ctypes.c_uint64),
        ("live_manifest_publish_count", ctypes.c_uint64),
        ("live_resource_generation", ctypes.c_uint64),
        ("live_resource_publish_count", ctypes.c_uint64),
        ("live_controller_refresh_count", ctypes.c_uint64),
        ("live_presentation_wait_count", ctypes.c_uint64),
        ("live_presentation_wait_milliseconds", ctypes.c_uint64),
        ("live_video_frame_timer_handle", ctypes.c_uint64),
        ("live_next_video_frame_deadline_qpc", ctypes.c_int64),
        ("live_video_pacing_wait_count", ctypes.c_uint64),
        ("live_video_pacing_wait_microseconds", ctypes.c_uint64),
        ("live_resource_slot", ctypes.c_uint32),
        ("live_resource_payload_size", ctypes.c_uint32),
        ("live_controller_sequence", ctypes.c_uint32),
        ("live_frontend_text", ctypes.c_char * 129),
        ("live_frontend_text_x_bits", ctypes.c_uint32),
        ("live_frontend_text_y_bits", ctypes.c_uint32),
        ("live_frontend_text_size_bits", ctypes.c_uint32),
        ("live_frontend_text_color_argb", ctypes.c_uint32),
        ("live_frontend_text_pending", ctypes.c_bool),
        ("normal_runtime_failure_code", ctypes.c_uint32),
        ("normal_runtime_failure_target", ctypes.c_uint32),
        ("normal_runtime_stop_requested", ctypes.c_bool),
        ("presentation_event_handle", ctypes.c_uint64),
        ("publication_event_handle", ctypes.c_uint64),
        ("normal_runtime_opaque", ctypes.c_uint64),
        ("audio_library_path", ctypes.c_char * 1024),
        ("native_audio_decoded_special_clip_count", ctypes.c_uint64),
        ("native_audio_decoded_music_track_count", ctypes.c_uint64),
        ("native_audio_decode_failure_count", ctypes.c_uint64),
        ("native_audio_submitted_buffer_count", ctypes.c_uint64),
        ("native_audio_submitted_byte_count", ctypes.c_uint64),
        ("native_audio_mixed_chunk_count", ctypes.c_uint64),
        ("native_audio_dropped_buffer_count", ctypes.c_uint64),
        ("native_audio_output_error_count", ctypes.c_uint64),
        ("native_audio_queued_bytes", ctypes.c_uint64),
        ("native_audio_buffer_create_count", ctypes.c_uint64),
        ("native_audio_buffer_data_count", ctypes.c_uint64),
        ("native_audio_buffer_format_count", ctypes.c_uint64),
        ("native_audio_buffer_volume_count", ctypes.c_uint64),
        ("native_audio_buffer_frequency_count", ctypes.c_uint64),
        ("native_audio_buffer_play_count", ctypes.c_uint64),
        ("native_audio_buffer_repeated_play_count", ctypes.c_uint64),
        ("native_audio_buffer_get_position_count", ctypes.c_uint64),
        ("native_audio_buffer_set_position_count", ctypes.c_uint64),
        ("native_audio_buffer_refresh_count", ctypes.c_uint64),
        ("native_audio_buffer_stop_count", ctypes.c_uint64),
        ("native_audio_stream_create_count", ctypes.c_uint64),
        ("native_audio_stream_process_count", ctypes.c_uint64),
        ("native_audio_stream_flush_count", ctypes.c_uint64),
        ("native_audio_stream_packet_byte_count", ctypes.c_uint64),
        ("native_audio_decoded_buffer_count", ctypes.c_uint64),
        ("native_audio_decoded_stream_packet_count", ctypes.c_uint64),
        ("native_audio_active_buffer_count", ctypes.c_uint32),
        ("native_audio_active_stream_count", ctypes.c_uint32),
        ("native_audio_buffer_play_stage", ctypes.c_uint32),
        ("native_audio_last_buffer", ctypes.c_uint32),
        ("native_audio_last_data", ctypes.c_uint32),
        ("native_audio_last_size", ctypes.c_uint32),
        ("native_audio_last_sample_rate", ctypes.c_uint32),
        ("native_audio_last_format_tag", ctypes.c_uint32),
        ("native_audio_last_channels", ctypes.c_uint32),
        ("native_audio_last_bits_per_sample", ctypes.c_uint32),
        ("native_audio_last_block_align", ctypes.c_uint32),
        ("native_audio_last_samples_per_block", ctypes.c_uint32),
        ("native_audio_last_volume", ctypes.c_int32),
        ("native_audio_last_frequency", ctypes.c_uint32),
        ("native_audio_largest_loop_buffer", ctypes.c_uint32),
        ("native_audio_largest_loop_data", ctypes.c_uint32),
        ("native_audio_largest_loop_play_length", ctypes.c_uint32),
        ("native_audio_largest_loop_start", ctypes.c_uint32),
        ("native_audio_largest_loop_length", ctypes.c_uint32),
        ("native_audio_largest_loop_sample_rate", ctypes.c_uint32),
        ("native_audio_largest_loop_frequency", ctypes.c_uint32),
        ("native_audio_largest_loop_format_tag", ctypes.c_uint32),
        ("native_service_bypass_target", ctypes.c_uint32),
        ("native_audio_output_open", ctypes.c_bool),
        ("native_audio_looping", ctypes.c_bool),
        ("native_traffic_world_draw_count", ctypes.c_uint64),
        ("native_traffic_world_zero_index_count", ctypes.c_uint64),
        ("native_traffic_mesh_sample_count", ctypes.c_uint32),
        ("native_traffic_mesh_sample_cursor", ctypes.c_uint32),
        ("native_traffic_mesh_samples", _NativeTrafficMeshSample * 1024),
    ]

    def __init__(self, entries: Iterable[NativeHostServiceEntry] = ()) -> None:
        super().__init__()
        active_entries = tuple(entries)
        self._entry_storage = (NativeHostServiceEntry * len(active_entries))(
            *active_entries
        )
        self.entries = self._entry_storage if active_entries else None
        self.entry_count = len(active_entries)
        self.eax_offset = _Context.eax.offset
        self.ecx_offset = _Context.ecx.offset
        self.esp_offset = _Context.esp.offset
        self.eip_offset = _Context.eip.offset
        self.timestamp_counter_offset = _Context.timestamp_counter.offset
        self.flags_offset = _Context.flags.offset
        self.yield_requested_offset = _Context.yield_requested.offset
        self.next_pool_address = 0x10000000
        self.next_contiguous_address = 0x20000000
        self.title_heap_next_address = 0x18000000
        self.next_object_handle = 0x108
        self.title_asset_payload_next = 0x32000000
        self._controller_snapshots: list[tuple[int, ...] | None] = [None] * 4
        self._worker_lifecycle: NativeWorkerLifecycleState | None = None
        self._live_transports: tuple[Any, Any, Any] | None = None

    def enable_normal_runtime(self, *, scheduler_quantum: int = 100_000) -> None:
        if scheduler_quantum <= 0:
            raise NativeExecutorError(
                "native normal-runtime scheduler quantum must be positive"
            )
        self.normal_runtime_enabled = True
        self.scheduler_quantum = int(scheduler_quantum)

    def attach_live_transports(
        self,
        control: Any,
        command: Any,
        resource: Any,
        *,
        command_stream_generation: int,
        presentation_event_handle: int | None = None,
        publication_event_handle: int | None = None,
    ) -> None:
        """Expose fixed live mappings to the native normal-play coordinator."""

        transports = (control, command, resource)
        for label, transport in zip(
            ("control", "command", "resource"), transports, strict=True
        ):
            if transport is None:
                raise NativeExecutorError(
                    f"native normal runtime requires the live {label} transport"
                )
        self._live_transports = transports
        self.live_control_mapping = ctypes.cast(
            int(control.buffer_address()), ctypes.POINTER(ctypes.c_uint8)
        )
        self.live_control_size = int(control.buffer_size)
        self.live_command_mapping = ctypes.cast(
            int(command.buffer_address()), ctypes.POINTER(ctypes.c_uint8)
        )
        self.live_command_size = int(command.buffer_size)
        self.live_resource_mapping = ctypes.cast(
            int(resource.buffer_address()), ctypes.POINTER(ctypes.c_uint8)
        )
        self.live_resource_size = int(resource.buffer_size)
        self.command_stream_generation = int(command_stream_generation)
        self.presentation_event_handle = int(presentation_event_handle or 0)
        self.publication_event_handle = int(publication_event_handle or 0)

    def set_audio_library(self, library: Path | None) -> None:
        self.audio_library_path = self._encode_filesystem_root(
            library, "audio-library"
        )

    def set_extracted_root(self, root: Path | None) -> None:
        self.extracted_root = self._encode_filesystem_root(
            root, "extracted-disc"
        )

    @staticmethod
    def _encode_filesystem_root(root: Path | None, label: str) -> bytes:
        encoded = os.fsencode(str(root.resolve())) if root is not None else b""
        if b"\0" in encoded or len(encoded) >= 1024:
            raise NativeExecutorError(f"native {label} root is too long")
        return encoded

    def set_filesystem_roots(
        self,
        *,
        extracted_disc_root: Path | None,
        save_data_root: Path | None,
        dashboard_data_root: Path | None,
        cache_data_root: Path | None,
        title_id: int | None,
    ) -> None:
        self.extracted_root = self._encode_filesystem_root(
            extracted_disc_root, "extracted-disc"
        )
        self.save_data_root = self._encode_filesystem_root(
            save_data_root, "save-data"
        )
        self.dashboard_data_root = self._encode_filesystem_root(
            dashboard_data_root, "dashboard-data"
        )
        self.cache_data_root = self._encode_filesystem_root(
            cache_data_root, "cache-data"
        )
        self.title_id = 0 if title_id is None else title_id & 0xFFFFFFFF

    def attach_worker_lifecycle(
        self, state: NativeWorkerLifecycleState | None
    ) -> None:
        self._worker_lifecycle = state
        self.worker_lifecycle = ctypes.pointer(state) if state is not None else None

    def seed_allocations(self, snapshot: dict[str, Any]) -> None:
        allocations = snapshot.get("allocations", ())
        if not isinstance(allocations, (list, tuple)):
            raise NativeExecutorError("native allocation snapshot is malformed")
        if len(allocations) > len(self.allocations):
            raise NativeExecutorError(
                "native allocation snapshot exceeds the fixed allocation capacity"
            )
        self.allocation_count = 0
        for index, allocation in enumerate(allocations):
            if not isinstance(allocation, dict):
                raise NativeExecutorError("native allocation record is malformed")
            entry = self.allocations[index]
            entry.address = int(allocation["address"])
            entry.size = int(allocation["size"])
            entry.kind = 2 if allocation.get("kind") == "contiguous" else 1
            entry.active = 1
            self.allocation_count += 1
        self.next_pool_address = int(
            snapshot.get("next_pool_address", self.next_pool_address)
        )
        self.next_contiguous_address = int(
            snapshot.get(
                "next_contiguous_address", self.next_contiguous_address
            )
        )

    def seed_semaphores(self, snapshot: Iterable[dict[str, int]]) -> None:
        semaphores = tuple(snapshot)
        if len(semaphores) > len(self.semaphores):
            raise NativeExecutorError(
                "native semaphore snapshot exceeds the fixed semaphore capacity"
            )
        self.semaphore_count = 0
        for index, semaphore in enumerate(semaphores):
            entry = self.semaphores[index]
            entry.handle = int(semaphore["handle"])
            entry.count = int(semaphore["count"])
            entry.limit = int(semaphore["limit"])
            self.semaphore_count += 1
            self.next_object_handle = max(
                int(self.next_object_handle), entry.handle + 4
            )

    def update_clock(self, snapshot: dict[str, Any]) -> None:
        self.performance_counter = int(snapshot.get("performance_counter", 0))
        self.system_time_filetime = int(snapshot.get("system_time_filetime", 0))

    def update_controllers(self, states: dict[int, Any]) -> None:
        for port in range(4):
            controller = states.get(port)
            values = (
                int(getattr(controller, "buttons", 0)),
                int(getattr(controller, "left_trigger", 0)),
                int(getattr(controller, "right_trigger", 0)),
                int(getattr(controller, "thumb_lx", 0)),
                int(getattr(controller, "thumb_ly", 0)),
                int(getattr(controller, "thumb_rx", 0)),
                int(getattr(controller, "thumb_ry", 0)),
                int(bool(getattr(controller, "connected", False))),
            )
            target = self.controllers[port]
            (
                target.buttons,
                target.left_trigger,
                target.right_trigger,
                target.thumb_lx,
                target.thumb_ly,
                target.thumb_rx,
                target.thumb_ry,
                target.connected,
            ) = values
            if values != self._controller_snapshots[port]:
                target.generation = (target.generation + 1) & 0xFFFFFFFF or 1
                self._controller_snapshots[port] = values

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
    ("module_exit_reason", ctypes.c_uint32),
    ("callback_bypass_target", ctypes.c_uint32),
    ("steps", ctypes.c_uint64),
    ("step_budget", ctypes.c_uint64),
    ("yield_requested", ctypes.c_bool),
    ("direct_observed_write_transport", ctypes.c_bool),
    ("user", ctypes.c_void_p),
    ("read_u32", _ReadU32),
    ("write_u32", _WriteU32),
    ("read_u8", _ReadU8),
    ("write_u8", _WriteU8),
    ("native_memory_user", ctypes.c_void_p),
    ("native_read_u32", ctypes.c_void_p),
    ("native_write_u32", ctypes.c_void_p),
    ("native_read_u8", ctypes.c_void_p),
    ("native_write_u8", ctypes.c_void_p),
    ("call", _Call),
    ("observe", _Observe),
    ("read_pages", ctypes.POINTER(ctypes.c_void_p)),
    ("callback_pages", ctypes.POINTER(ctypes.c_uint8)),
    ("callback_address_keys", ctypes.POINTER(ctypes.c_uint32)),
    ("callback_address_mask", ctypes.c_uint32),
    ("write_callback_pages", ctypes.POINTER(ctypes.c_uint8)),
    ("write_callback_address_keys", ctypes.POINTER(ctypes.c_uint32)),
    ("write_callback_address_mask", ctypes.c_uint32),
    ("zero_read_callback_pages", ctypes.POINTER(ctypes.c_uint8)),
    ("zero_read_callback_address_keys", ctypes.POINTER(ctypes.c_uint32)),
    ("zero_read_callback_address_mask", ctypes.c_uint32),
    ("cache_physical_aliases", ctypes.c_bool),
    ("dirty_pages", ctypes.POINTER(ctypes.c_uint8)),
    ("dirty_page_generations", ctypes.POINTER(ctypes.c_uint32)),
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
    ("observed_write_values", ctypes.POINTER(ctypes.c_uint64)),
    ("observed_write_steps", ctypes.POINTER(ctypes.c_uint64)),
    ("observed_write_sizes", ctypes.POINTER(ctypes.c_uint8)),
    ("observed_write_count", ctypes.c_uint32),
    ("observed_write_capacity", ctypes.c_uint32),
    ("observed_write_packet_header", ctypes.c_uint32),
    ("observed_write_packet_next_address", ctypes.c_uint32),
    ("observed_write_packet_yield_count", ctypes.c_uint64),
    ("zero_read_callback_bypass_count", ctypes.c_uint64),
    ("direct_observed_write_count", ctypes.c_uint64),
    ("direct_observed_write_byte_count", ctypes.c_uint64),
    ("direct_observed_payload", ctypes.POINTER(ctypes.c_uint8)),
    ("direct_observed_payload_size", ctypes.c_uint32),
    ("direct_observed_payload_capacity", ctypes.c_uint32),
    ("direct_observed_span_addresses", ctypes.POINTER(ctypes.c_uint32)),
    ("direct_observed_span_payload_offsets", ctypes.POINTER(ctypes.c_uint32)),
    ("direct_observed_span_payload_sizes", ctypes.POINTER(ctypes.c_uint32)),
    ("direct_observed_span_write_counts", ctypes.POINTER(ctypes.c_uint32)),
    ("direct_observed_span_flags", ctypes.POINTER(ctypes.c_uint8)),
    ("direct_observed_span_count", ctypes.c_uint32),
    ("direct_observed_span_capacity", ctypes.c_uint32),
    ("direct_observed_span_sealed", ctypes.c_bool),
    ("native_fast_path_address_keys", ctypes.POINTER(ctypes.c_uint32)),
    ("native_fast_path_call_counts", ctypes.POINTER(ctypes.c_uint64)),
    ("native_fast_path_address_mask", ctypes.c_uint32),
]


class NativeResumableExecutor:
    """Compile a lifted frame once, then resume it across host-service boundaries."""

    SYMBOL = "b2r_native_loop"
    BASE_INSTRUCTIONS_PER_MODULE = 3000
    MAX_INSTRUCTIONS_PER_MODULE = 12000
    INCREMENTAL_INSTRUCTIONS_PER_MODULE = 256

    def __init__(
        self,
        function: LiftedFunction,
        *,
        build_dir: Path,
        observer_addresses: Iterable[int] = (),
        callback_addresses: Iterable[int] = (),
        forwarded_callback_addresses: Iterable[int] = (),
        native_passthrough_callback_addresses: Iterable[int] = (),
        memory_callback_addresses: Iterable[int] = (),
        memory_read_callback_addresses: Iterable[int] = (),
        memory_write_callback_addresses: Iterable[int] = (),
        memory_zero_read_callback_addresses: Iterable[int] = (),
        native_fast_paths: dict[int, NativeFastPath] | None = None,
        synchronize_eip_for_callbacks: bool = False,
        module_functions: Iterable[LiftedFunction] | None = None,
        incremental_module_functions: Iterable[LiftedFunction] | None = None,
        compiler: str | None = None,
        compile_worker_limit: int | None = None,
        low_priority_compilation: bool = False,
    ) -> None:
        compile_profile = "clang-cl-o2-call-fused-v11-native-frame-resource-state"
        build_dir.mkdir(parents=True, exist_ok=True)
        compiler_path = compiler or shutil.which("clang-cl")
        if compiler_path is None:
            raise NativeExecutorError("clang-cl is required to build the native guest loop")
        observer_addresses = {int(address) for address in observer_addresses}
        callback_addresses = {int(address) for address in callback_addresses}
        forwarded_callback_addresses = {
            int(address) for address in forwarded_callback_addresses
        }
        if not forwarded_callback_addresses <= callback_addresses:
            raise ValueError(
                "forwarded callback addresses must also be callback addresses"
            )
        native_passthrough_callback_addresses = {
            int(address) for address in native_passthrough_callback_addresses
        }
        if not native_passthrough_callback_addresses <= callback_addresses:
            raise ValueError(
                "native passthrough callback addresses must also be callback addresses"
            )
        native_fast_paths = {
            int(address) & 0xFFFFFFFF: fast_path
            for address, fast_path in (native_fast_paths or {}).items()
        }
        common_memory_callback_addresses = {
            int(address) for address in memory_callback_addresses
        }
        memory_read_callback_addresses = {
            *common_memory_callback_addresses,
            *(int(address) for address in memory_read_callback_addresses),
        }
        memory_write_callback_addresses = {
            *common_memory_callback_addresses,
            *(int(address) for address in memory_write_callback_addresses),
        }
        memory_zero_read_callback_addresses = {
            int(address) for address in memory_zero_read_callback_addresses
        }
        if not memory_zero_read_callback_addresses <= memory_read_callback_addresses:
            raise ValueError(
                "zero-guarded memory reads must also be read callback addresses"
            )
        base_module_sources = (
            tuple(module_functions) if module_functions is not None else (function,)
        )
        incremental_module_sources = tuple(incremental_module_functions or ())
        seen_addresses: set[int] = set()
        base_instructions: list[Any] = []
        for module_source in base_module_sources:
            new_instructions = [
                instruction
                for instruction in module_source.instructions
                if instruction.address not in seen_addresses
            ]
            seen_addresses.update(
                instruction.address for instruction in new_instructions
            )
            base_instructions.extend(new_instructions)
        base_chunks = _partition_instructions_for_call_fusion(
            base_instructions,
            base_maximum_count=self.BASE_INSTRUCTIONS_PER_MODULE,
            maximum_count=self.MAX_INSTRUCTIONS_PER_MODULE,
            isolated_addresses=native_fast_paths,
        )
        chunk_specs: list[tuple[list[Any], str, str]] = [
            (chunk, "base", "/O2") for chunk in base_chunks
        ]
        incremental_instruction_count = 0
        for module_source in incremental_module_sources:
            new_instructions = [
                instruction
                for instruction in module_source.instructions
                if instruction.address not in seen_addresses
            ]
            seen_addresses.update(
                instruction.address for instruction in new_instructions
            )
            incremental_instruction_count += len(new_instructions)
            if not new_instructions:
                continue
            chunk_specs.extend(
                (chunk, "incremental", "/Od")
                for chunk in _partition_instructions_by_address(
                    new_instructions,
                    maximum_count=self.INCREMENTAL_INSTRUCTIONS_PER_MODULE,
                )
            )
        if not chunk_specs:
            chunk_specs = [([], "base", "/O2")]

        compiler_executable = shutil.which(compiler_path) or compiler_path
        compile_worker_limit = (
            6 if compile_worker_limit is None else max(1, int(compile_worker_limit))
        )
        compile_creation_flags = (
            int(getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0))
            if low_priority_compilation
            else 0
        )
        compiler_file = Path(compiler_executable).resolve()
        try:
            compiler_stat = compiler_file.stat()
            compiler_identity = {
                "path": str(compiler_file),
                "size": compiler_stat.st_size,
                "mtime_ns": compiler_stat.st_mtime_ns,
            }
        except OSError:
            compiler_identity = {"path": str(compiler_executable)}
        emitter_file = NATIVE_EMITTER_SOURCE_FILE
        try:
            emitter_stat = emitter_file.stat()
            emitter_identity = {
                "path": str(emitter_file),
                "size": emitter_stat.st_size,
                "mtime_ns": emitter_stat.st_mtime_ns,
            }
        except OSError:
            emitter_identity = {"path": str(emitter_file)}
        base_configuration = {
            "schema": "native-partition-manifest-v1",
            "compile_profile": compile_profile,
            "compiler": compiler_identity,
            "emitter": emitter_identity,
            "maximum_instructions_per_module": self.MAX_INSTRUCTIONS_PER_MODULE,
            "base_instructions_per_module": self.BASE_INSTRUCTIONS_PER_MODULE,
            "partition_strategy": "weighted-direct-call-and-tail-call-fusion-v1",
            "observer_addresses": sorted(observer_addresses),
            "callback_addresses": sorted(callback_addresses),
            "forwarded_callback_addresses": sorted(forwarded_callback_addresses),
            "synchronize_eip_for_callbacks": bool(synchronize_eip_for_callbacks),
        }
        base_configuration_key = hashlib.sha256(
            json.dumps(
                base_configuration,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        manifest = NativeModuleManifest(build_dir)
        modules: list[dict[str, Any]] = []
        for chunk, module_kind, optimization in chunk_specs:
            partition_start = chunk[0].address if chunk else function.base_address
            partition_end = chunk[-1].next_address if chunk else function.base_address
            module_identity = f"{partition_start:08X}_{partition_end:08X}"
            module = LiftedFunction(
                symbol=f"b2r_partition_{module_identity}",
                base_address=partition_start,
                code_size=partition_end - partition_start,
                instructions=tuple(chunk),
            )
            module_addresses = {instruction.address for instruction in chunk}
            module_fast_paths = {
                f"0x{address:08X}": native_fast_paths[address].to_dict()
                for address in sorted(native_fast_paths)
                if address in module_addresses
            }
            module_configuration = {
                "base_configuration_key": base_configuration_key,
                "native_fast_paths": module_fast_paths,
            }
            if module_kind == "incremental":
                module_configuration["incremental_module_profile"] = (
                    "isolated-256-instruction-od-v1"
                )
            configuration_key = hashlib.sha256(
                json.dumps(
                    module_configuration,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            content_digest = hashlib.sha256(
                json.dumps(
                    module.to_dict(include_bytes=True),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            cached_paths = manifest.resolve(
                configuration_key=configuration_key,
                partition_start=partition_start,
                partition_end=partition_end,
                content_digest=content_digest,
            )
            cache_hit = cached_paths is not None
            if cached_paths is None:
                artifact_digest = hashlib.sha256(
                    (
                        configuration_key
                        + f":{partition_start:08X}:{partition_end:08X}:"
                        + content_digest
                    ).encode("ascii")
                ).hexdigest()[:24]
                source_path = build_dir / f"native-loop-{artifact_digest}.cpp"
                dll_path = build_dir / f"native-loop-{artifact_digest}.dll"
            else:
                source_path, dll_path = cached_paths
            modules.append(
                {
                    "module": module,
                    "partition_start": partition_start,
                    "partition_end": partition_end,
                    "configuration_key": configuration_key,
                    "content_digest": content_digest,
                    "source_path": source_path,
                    "dll_path": dll_path,
                    "cache_hit": cache_hit,
                    "module_kind": module_kind,
                    "optimization": optimization,
                    "equivalent_source_candidates": (
                        []
                        if cache_hit
                        else manifest.equivalent_source_candidates(
                            partition_start=partition_start,
                            partition_end=partition_end,
                            content_digest=content_digest,
                        )
                    ),
                    "artifact_adopted": False,
                    "equivalent_source_reused": False,
                    "compiled": False,
                    "source_emit_us": 0,
                    "compile_us": 0,
                }
            )

        modules_to_compile = [item for item in modules if not item["cache_hit"]]

        def compile_module(item: dict[str, Any]) -> None:
            module = item["module"]
            source_path = item["source_path"]
            dll_path = item["dll_path"]
            if _is_native_dll_artifact(dll_path):
                item["artifact_adopted"] = True
                return
            emit_started_ns = time.perf_counter_ns()
            source = emit_cpp(
                module,
                exported_symbol=self.SYMBOL,
                resumable=True,
                observer_addresses=observer_addresses,
                callback_addresses=callback_addresses,
                forwarded_callback_addresses=forwarded_callback_addresses,
                native_fast_paths=native_fast_paths,
                synchronize_eip_for_callbacks=synchronize_eip_for_callbacks,
            )
            source_path.write_text(source, encoding="utf-8", newline="\n")
            item["source_emit_us"] = (
                time.perf_counter_ns() - emit_started_ns
            ) // 1_000
            temporary_dll_path = dll_path.with_name(
                f"{dll_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            source_bytes = source.encode("utf-8")
            for candidate_source, candidate_dll in item[
                "equivalent_source_candidates"
            ]:
                try:
                    if candidate_source.read_bytes() != source_bytes:
                        continue
                    shutil.copy2(candidate_dll, temporary_dll_path)
                except OSError:
                    temporary_dll_path.unlink(missing_ok=True)
                    continue
                if not _is_native_dll_artifact(temporary_dll_path):
                    temporary_dll_path.unlink(missing_ok=True)
                    continue
                temporary_dll_path.replace(dll_path)
                item["artifact_adopted"] = True
                item["equivalent_source_reused"] = True
                return
            compile_started_ns = time.perf_counter_ns()
            completed = subprocess.run(
                [
                    compiler_executable,
                    "/nologo",
                    "/std:c++17",
                    item["optimization"],
                    "/LD",
                    str(source_path),
                    f"/Fe:{temporary_dll_path}",
                    "/link",
                    "/NOIMPLIB",
                ],
                capture_output=True,
                text=True,
                check=False,
                creationflags=compile_creation_flags,
            )
            item["compile_us"] = (
                time.perf_counter_ns() - compile_started_ns
            ) // 1_000
            if completed.returncode != 0:
                temporary_dll_path.unlink(missing_ok=True)
                raise NativeExecutorError(
                    "native guest-loop compilation failed:\n"
                    + (completed.stdout + completed.stderr).strip()
                )
            if not _is_native_dll_artifact(temporary_dll_path):
                temporary_dll_path.unlink(missing_ok=True)
                raise NativeExecutorError(
                    f"native guest-loop compiler produced an invalid DLL: {dll_path}"
                )
            temporary_dll_path.replace(dll_path)
            item["compiled"] = True

        compile_workers = 0
        compile_wall_us = 0
        if modules_to_compile:
            compile_workers = min(
                compile_worker_limit,
                len(modules_to_compile),
                max(1, os.cpu_count() or 1),
            )
            compile_wall_started_ns = time.perf_counter_ns()
            with ThreadPoolExecutor(
                max_workers=compile_workers
            ) as pool:
                list(pool.map(compile_module, modules_to_compile))
            compile_wall_us = (
                time.perf_counter_ns() - compile_wall_started_ns
            ) // 1_000
            for item in modules_to_compile:
                manifest.record(
                    configuration_key=item["configuration_key"],
                    partition_start=item["partition_start"],
                    partition_end=item["partition_end"],
                    content_digest=item["content_digest"],
                    source_path=item["source_path"],
                    artifact_path=item["dll_path"],
                )
        manifest.prune_if_needed(
            protected_artifacts=(item["dll_path"] for item in modules)
        )
        self.cache_summary = {
            **manifest.summary(),
            "configuration_key": base_configuration_key,
            "partition_configuration_count": len(
                {item["configuration_key"] for item in modules}
            ),
            "localized_fast_path_configuration": True,
            "known_reachable_partition_count": len(modules),
            "base_partition_count": sum(
                item["module_kind"] == "base" for item in modules
            ),
            "incremental_partition_count": sum(
                item["module_kind"] == "incremental" for item in modules
            ),
            "incremental_instruction_count": incremental_instruction_count,
            "incremental_partition_instruction_limit": (
                self.INCREMENTAL_INSTRUCTIONS_PER_MODULE
            ),
            "incremental_max_partition_instruction_count": max(
                (
                    len(item["module"].instructions)
                    for item in modules
                    if item["module_kind"] == "incremental"
                ),
                default=0,
            ),
            "incremental_compile_optimization": "Od",
            "warm_hit_count": sum(bool(item["cache_hit"]) for item in modules),
            "recovered_artifact_count": sum(
                bool(item["artifact_adopted"]) for item in modules
            ),
            "equivalent_source_reuse_count": sum(
                bool(item["equivalent_source_reused"]) for item in modules
            ),
            "ahead_compiled_count": sum(bool(item["compiled"]) for item in modules),
            "base_compiled_count": sum(
                bool(item["compiled"]) and item["module_kind"] == "base"
                for item in modules
            ),
            "incremental_compiled_count": sum(
                bool(item["compiled"])
                and item["module_kind"] == "incremental"
                for item in modules
            ),
            "parallel_compile_workers": compile_workers,
            "compile_worker_limit": compile_worker_limit,
            "low_priority_compilation": bool(low_priority_compilation),
            "executor_instance_id": (
                f"{os.getpid()}-{time.time_ns()}-{id(self):X}"
            ),
            "compile_wall_us": compile_wall_us,
            "source_emit_us": sum(int(item["source_emit_us"]) for item in modules),
            "compiler_process_us": sum(int(item["compile_us"]) for item in modules),
        }
        manifest.close()

        dispatcher_digest = hashlib.sha256(
            ("clang-cl-o2-native-module-dispatch-v5\n" + _NATIVE_DISPATCH_SOURCE).encode(
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
                    compiler_executable,
                    "/nologo",
                    "/std:c++17",
                    "/O2",
                    "/LD",
                    str(dispatcher_source_path),
                    f"/Fe:{dispatcher_dll_path}",
                    "/link",
                    "/NOIMPLIB",
                ],
                capture_output=True,
                text=True,
                check=False,
                creationflags=compile_creation_flags,
            )
            if completed.returncode != 0:
                raise NativeExecutorError(
                    "native module dispatcher compilation failed:\n"
                    + (completed.stdout + completed.stderr).strip()
                )

        self.dll_path = modules[0]["dll_path"]
        self._base_address = function.base_address
        self._native_fast_paths = native_fast_paths
        self._host_call_addresses = frozenset(callback_addresses)
        self._valid_addresses = {instruction.address for instruction in function.instructions}
        # Execution callback targets yield through generated control flow; they
        # do not make every data access sharing the target's 4 KiB code page
        # volatile. Only addresses whose memory semantics change on reads must
        # bypass the native page cache.
        self._read_callback_byte_addresses = {
            (int(address) + offset) & 0xFFFFFFFF
            for address in memory_read_callback_addresses
            for offset in range(4)
        }
        self._write_callback_byte_addresses = {
            (int(address) + offset) & 0xFFFFFFFF
            for address in memory_write_callback_addresses
            for offset in range(4)
        }
        self._zero_read_callback_addresses = {
            int(address) & 0xFFFFFFFF
            for address in memory_zero_read_callback_addresses
        }
        self._callback_pages = {
            address >> 12 for address in self._read_callback_byte_addresses
        }
        self._libraries: list[ctypes.CDLL] = []
        self._entries_by_address: dict[int, Callable] = {}
        self.last_run_summary: dict[str, Any] | None = None
        self.current_run_metrics: dict[str, Any] | None = None
        for item in modules:
            module = item["module"]
            dll_path = item["dll_path"]
            library = ctypes.CDLL(str(dll_path))
            entry = getattr(library, self.SYMBOL)
            entry.argtypes = [ctypes.POINTER(_Context)]
            entry.restype = ctypes.c_uint32
            self._libraries.append(library)
            for instruction in module.instructions:
                self._entries_by_address[instruction.address] = entry
        self._dispatcher_library = ctypes.CDLL(str(dispatcher_dll_path))
        native_context_size = getattr(
            self._dispatcher_library,
            "b2r_native_context_size",
        )
        native_context_size.argtypes = []
        native_context_size.restype = ctypes.c_uint32
        if int(native_context_size()) != ctypes.sizeof(_Context):
            raise NativeExecutorError(
                "native dispatcher context ABI does not match generated modules"
            )
        self._dispatcher = getattr(self._dispatcher_library, "b2r_native_dispatch")
        self._native_memory_write_u32 = getattr(
            self._dispatcher_library,
            "b2r_native_memory_write_u32",
        )
        self._native_memory_write_u32.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        self._native_memory_write_u32.restype = ctypes.c_bool
        self._native_memory_read_u32 = getattr(
            self._dispatcher_library,
            "b2r_native_memory_read_u32",
        )
        self._native_memory_read_u32.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        self._native_memory_read_u32.restype = ctypes.c_bool
        self._native_memory_read_u8 = getattr(
            self._dispatcher_library,
            "b2r_native_memory_read_u8",
        )
        self._native_memory_read_u8.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint8),
        ]
        self._native_memory_read_u8.restype = ctypes.c_bool
        self._native_memory_write_u8 = getattr(
            self._dispatcher_library,
            "b2r_native_memory_write_u8",
        )
        self._native_memory_write_u8.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint8,
        ]
        self._native_memory_write_u8.restype = ctypes.c_bool
        self._native_observe_callback = _Observe(
            ("b2r_native_observe", self._dispatcher_library)
        )
        self._dispatcher.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_uint32,
            ctypes.POINTER(NativeHostServiceState),
            _HostCall,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_bool),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_bool,
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
        ]
        self._dispatcher.restype = ctypes.c_uint32
        self._cooperative_scheduler = getattr(
            self._dispatcher_library,
            "b2r_update_cooperative_scheduler",
        )
        self._cooperative_scheduler.argtypes = [
            ctypes.POINTER(NativeCooperativeSchedulerState),
            ctypes.c_uint64,
            ctypes.c_uint64,
            ctypes.c_uint64,
            ctypes.c_bool,
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
        ]
        self._cooperative_scheduler.restype = None
        self._worker_lifecycle_sync = getattr(
            self._dispatcher_library,
            "b2r_sync_worker_lifecycle",
        )
        self._worker_lifecycle_sync.argtypes = [
            ctypes.POINTER(NativeWorkerLifecycleState),
            ctypes.POINTER(NativeWorkerLifecycleEntry),
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        self._worker_lifecycle_sync.restype = None
        self._worker_lifecycle_set_status = getattr(
            self._dispatcher_library,
            "b2r_set_worker_lifecycle_status",
        )
        self._worker_lifecycle_set_status.argtypes = [
            ctypes.POINTER(NativeWorkerLifecycleState),
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        self._worker_lifecycle_set_status.restype = ctypes.c_bool
        self._worker_lifecycle_select = getattr(
            self._dispatcher_library,
            "b2r_select_runnable_workers",
        )
        self._worker_lifecycle_select.argtypes = [
            ctypes.POINTER(NativeWorkerLifecycleState),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_uint32,
        ]
        self._worker_lifecycle_select.restype = ctypes.c_uint32
        self._runtime_shutdown = getattr(
            self._dispatcher_library,
            "b2r_native_runtime_shutdown",
        )
        self._runtime_shutdown.argtypes = [
            ctypes.POINTER(NativeHostServiceState),
        ]
        self._runtime_shutdown.restype = None
        self._observed_write_packer = getattr(
            self._dispatcher_library,
            "b2r_pack_observed_write_records",
        )
        self._observed_write_packer.argtypes = [
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        self._observed_write_packer.restype = ctypes.c_uint32
        self._resource_method_scanner = getattr(
            self._dispatcher_library,
            "b2r_scan_resource_methods",
        )
        self._resource_method_scanner.argtypes = [
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
        ]
        self._resource_method_scanner.restype = ctypes.c_bool
        self._resource_span_method_scanner = getattr(
            self._dispatcher_library,
            "b2r_scan_resource_method_spans",
        )
        self._resource_span_method_scanner.argtypes = [
            ctypes.POINTER(_ResourceSpanScanState),
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_uint32,
            ctypes.c_bool,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
        ]
        self._resource_span_method_scanner.restype = ctypes.c_bool
        table_capacity = 2
        while table_capacity < max(1, len(self._entries_by_address)) * 2:
            table_capacity <<= 1
        self._dispatch_keys = (ctypes.c_uint32 * table_capacity)()
        self._dispatch_entries = (ctypes.c_void_p * table_capacity)()
        self._dispatch_addresses_by_slot = [0] * table_capacity
        self._dispatch_target_call_counts = (ctypes.c_uint64 * table_capacity)()
        self._dispatch_target_step_counts = (ctypes.c_uint64 * table_capacity)()
        self._dispatch_exit_reason_counts = (
            ctypes.c_uint64 * _NATIVE_MODULE_EXIT_REASON_COUNT
        )()
        self._dispatch_target_exit_reason_counts = (
            ctypes.c_uint64
            * (table_capacity * _NATIVE_MODULE_EXIT_REASON_COUNT)
        )()
        self._dispatch_touched_slots = (ctypes.c_uint32 * table_capacity)()
        self._dispatch_touched_count = ctypes.c_uint32()
        self._dispatch_mask = table_capacity - 1
        # Exact entry/exit/reason edges are much higher-cardinality than the
        # per-entry counters above. Allocate their lossless profiling table
        # lazily so representative non-profiled runs pay no memory cost.
        self._dispatch_edge_capacity = min(
            table_capacity * 2,
            NATIVE_DISPATCH_EDGE_CAPACITY_LIMIT,
        )
        self._dispatch_edge_keys: Any | None = None
        self._dispatch_edge_reasons: Any | None = None
        self._dispatch_edge_counts: Any | None = None
        self._dispatch_edge_touched_slots: Any | None = None
        self._dispatch_edge_touched_count = ctypes.c_uint32()
        self._dispatch_edge_overflow_count = ctypes.c_uint64()
        for address, entry in self._entries_by_address.items():
            if (
                address in callback_addresses
                and address not in forwarded_callback_addresses
                and address not in native_passthrough_callback_addresses
            ):
                continue
            slot = (address * 2654435761) & self._dispatch_mask
            while self._dispatch_entries[slot]:
                slot = (slot + 1) & self._dispatch_mask
            self._dispatch_keys[slot] = address
            self._dispatch_entries[slot] = ctypes.cast(entry, ctypes.c_void_p).value
            self._dispatch_addresses_by_slot[slot] = address

        host_call_capacity = 2
        while host_call_capacity < max(1, len(self._host_call_addresses)) * 2:
            host_call_capacity <<= 1
        self._host_call_keys = (ctypes.c_uint32 * host_call_capacity)(
            *([0xFFFFFFFF] * host_call_capacity)
        )
        self._host_call_mask = host_call_capacity - 1
        for address in self._host_call_addresses:
            slot = (address * 2654435761) & self._host_call_mask
            while self._host_call_keys[slot] != 0xFFFFFFFF:
                slot = (slot + 1) & self._host_call_mask
            self._host_call_keys[slot] = address

        native_fast_path_capacity = 2
        while native_fast_path_capacity < max(1, len(native_fast_paths)) * 2:
            native_fast_path_capacity <<= 1
        self._native_fast_path_address_keys = (
            ctypes.c_uint32 * native_fast_path_capacity
        )(*([0xFFFFFFFF] * native_fast_path_capacity))
        self._native_fast_path_call_counts = (
            ctypes.c_uint64 * native_fast_path_capacity
        )()
        self._native_fast_path_addresses_by_slot = [0] * native_fast_path_capacity
        self._native_fast_path_mask = native_fast_path_capacity - 1
        for address in native_fast_paths:
            slot = (address * 2654435761) & self._native_fast_path_mask
            while self._native_fast_path_address_keys[slot] != 0xFFFFFFFF:
                slot = (slot + 1) & self._native_fast_path_mask
            self._native_fast_path_address_keys[slot] = address
            self._native_fast_path_addresses_by_slot[slot] = address

        page_capacity = 1 << 20
        self._page_table = (ctypes.c_void_p * page_capacity)()
        def build_callback_table(addresses: set[int]):
            pages = (ctypes.c_uint8 * page_capacity)()
            capacity = 2
            while capacity < max(1, len(addresses)) * 2:
                capacity <<= 1
            keys = (ctypes.c_uint32 * capacity)(*([0xFFFFFFFF] * capacity))
            mask = capacity - 1
            for address in addresses:
                pages[address >> 12] = 1
                slot = (address * 2654435761) & mask
                while keys[slot] != 0xFFFFFFFF:
                    slot = (slot + 1) & mask
                keys[slot] = address
            return pages, keys, mask

        (
            self._callback_pages_table,
            self._callback_address_keys,
            self._callback_address_mask,
        ) = build_callback_table(self._read_callback_byte_addresses)
        (
            self._write_callback_pages_table,
            self._write_callback_address_keys,
            self._write_callback_address_mask,
        ) = build_callback_table(self._write_callback_byte_addresses)
        (
            self._zero_read_callback_pages_table,
            self._zero_read_callback_address_keys,
            self._zero_read_callback_address_mask,
        ) = build_callback_table(self._zero_read_callback_addresses)
        self._dirty_pages = (ctypes.c_uint8 * page_capacity)()
        self._dirty_page_generations = (ctypes.c_uint32 * page_capacity)()
        self._dirty_page_indices = (ctypes.c_uint32 * page_capacity)()
        self._dirty_page_min_offsets = (ctypes.c_uint16 * page_capacity)()
        self._dirty_page_max_offsets = (ctypes.c_uint16 * page_capacity)()
        self._page_generations: dict[int, int] = {}
        self._page_buffers: dict[int, ctypes.Array] = _NativePageBufferMap(
            self._page_table,
            self._page_generations,
        )
        self._cache_memory_identity: SparseMemory | None = None
        self._observed_write_capacity = 0
        self._observed_write_eips = (ctypes.c_uint32 * 1)()
        self._observed_write_source_addresses = (ctypes.c_uint32 * 1)()
        self._observed_write_addresses = (ctypes.c_uint32 * 1)()
        self._observed_write_values = (ctypes.c_uint64 * 1)()
        self._observed_write_steps = (ctypes.c_uint64 * 1)()
        self._observed_write_sizes = (ctypes.c_uint8 * 1)()
        self._direct_observed_payload = (ctypes.c_uint8 * 1)()
        self._direct_observed_span_addresses = (ctypes.c_uint32 * 1)()
        self._direct_observed_span_payload_offsets = (ctypes.c_uint32 * 1)()
        self._direct_observed_span_payload_sizes = (ctypes.c_uint32 * 1)()
        self._direct_observed_span_write_counts = (ctypes.c_uint32 * 1)()
        self._direct_observed_span_flags = (ctypes.c_uint8 * 1)()
        self._packed_observed_write_records = (ctypes.c_uint8 * 16)()
        self._packed_observed_write_texture_payload = (ctypes.c_uint8 * 8)()
        self._packed_observed_write_texture_runs = (ctypes.c_uint32 * 4)()
        self._packed_observed_write_texture_run_count = ctypes.c_uint32()
        self._resource_scan_capacity = 1
        self._resource_scan_methods = (ctypes.c_uint32 * 1)()
        self._resource_scan_values = (ctypes.c_uint32 * 1)()
        self._resource_scan_output_count = ctypes.c_uint32()
        self._resource_scan_data_word_count = ctypes.c_uint64()
        self._resource_scan_tracked_word_count = ctypes.c_uint64()
        self._resource_scan_skipped_word_count = ctypes.c_uint64()
        self._resource_scan_aggregated_index_word_count = ctypes.c_uint64()
        self._resource_span_scan_state = _ResourceSpanScanState()
        self._resource_span_scan_capacity = 1
        self._resource_span_scan_methods = (ctypes.c_uint32 * 1)()
        self._resource_span_scan_values = (ctypes.c_uint32 * 1)()
        self._resource_span_scan_positions = (ctypes.c_uint32 * 1)()
        self._resource_span_scan_output_count = ctypes.c_uint32()
        self._resource_span_scan_data_word_count = ctypes.c_uint64()
        self._resource_span_scan_tracked_word_count = ctypes.c_uint64()
        self._resource_span_scan_skipped_word_count = ctypes.c_uint64()
        self._resource_span_scan_aggregated_index_word_count = ctypes.c_uint64()

    def scan_resource_methods(
        self,
        payload: bytearray,
        byte_size: int,
    ) -> tuple[Any, Any, int, int, int, int, int]:
        """Compact push-buffer resource state for Python consumption."""
        if byte_size < 0 or byte_size > len(payload) or byte_size % 4:
            raise ValueError("resource method payload must be aligned packed words")
        word_count = byte_size // 4
        if word_count == 0:
            return (
                self._resource_scan_methods,
                self._resource_scan_values,
                0,
                0,
                0,
                0,
                0,
            )
        if word_count > self._resource_scan_capacity:
            self._resource_scan_methods = (ctypes.c_uint32 * word_count)()
            self._resource_scan_values = (ctypes.c_uint32 * word_count)()
            self._resource_scan_capacity = word_count
        first_byte = ctypes.c_uint8.from_buffer(payload)
        if not self._resource_method_scanner(
            ctypes.byref(first_byte),
            byte_size,
            self._resource_scan_methods,
            self._resource_scan_values,
            self._resource_scan_capacity,
            ctypes.byref(self._resource_scan_output_count),
            ctypes.byref(self._resource_scan_data_word_count),
            ctypes.byref(self._resource_scan_tracked_word_count),
            ctypes.byref(self._resource_scan_skipped_word_count),
            ctypes.byref(self._resource_scan_aggregated_index_word_count),
        ):
            raise NativeExecutorError("native resource method scan failed")
        return (
            self._resource_scan_methods,
            self._resource_scan_values,
            int(self._resource_scan_output_count.value),
            int(self._resource_scan_data_word_count.value),
            int(self._resource_scan_tracked_word_count.value),
            int(self._resource_scan_skipped_word_count.value),
            int(self._resource_scan_aggregated_index_word_count.value),
        )

    def scan_resource_method_spans(
        self,
        payload: Any,
        payload_size: int,
        addresses: Any,
        payload_offsets: Any,
        payload_sizes: Any,
        flags: Any,
        span_count: int,
    ) -> tuple[Any, Any, Any, int, int, int, int, int]:
        """Scan a batch of native push-buffer spans without Python copies."""
        payload_size = int(payload_size)
        span_count = int(span_count)
        if payload_size < 0 or span_count < 0:
            raise ValueError("resource method span dimensions must be non-negative")
        # A small final flip span can publish semantic state accumulated across
        # the whole frame, so capacity cannot depend only on this batch's bytes.
        output_capacity = max(1, payload_size // 4 + 1, 8192 * 3 + 1024 * 2)
        if output_capacity > self._resource_span_scan_capacity:
            self._resource_span_scan_methods = (
                ctypes.c_uint32 * output_capacity
            )()
            self._resource_span_scan_values = (
                ctypes.c_uint32 * output_capacity
            )()
            self._resource_span_scan_positions = (
                ctypes.c_uint32 * output_capacity
            )()
            self._resource_span_scan_capacity = output_capacity
        payload_pointer = ctypes.cast(payload, ctypes.POINTER(ctypes.c_uint8))
        if not self._resource_span_method_scanner(
            ctypes.byref(self._resource_span_scan_state),
            payload_pointer,
            payload_size,
            addresses,
            payload_offsets,
            payload_sizes,
            flags,
            span_count,
            False,
            self._resource_span_scan_methods,
            self._resource_span_scan_values,
            self._resource_span_scan_positions,
            self._resource_span_scan_capacity,
            ctypes.byref(self._resource_span_scan_output_count),
            ctypes.byref(self._resource_span_scan_data_word_count),
            ctypes.byref(self._resource_span_scan_tracked_word_count),
            ctypes.byref(self._resource_span_scan_skipped_word_count),
            ctypes.byref(
                self._resource_span_scan_aggregated_index_word_count
            ),
        ):
            raise NativeExecutorError("native resource span method scan failed")
        return self._resource_span_scan_result()

    def finish_resource_method_spans(
        self,
    ) -> tuple[Any, Any, Any, int, int, int, int, int]:
        """Flush and reset state retained by the native span scanner."""
        if not self._resource_span_method_scanner(
            ctypes.byref(self._resource_span_scan_state),
            None,
            0,
            None,
            None,
            None,
            None,
            0,
            True,
            self._resource_span_scan_methods,
            self._resource_span_scan_values,
            self._resource_span_scan_positions,
            self._resource_span_scan_capacity,
            ctypes.byref(self._resource_span_scan_output_count),
            ctypes.byref(self._resource_span_scan_data_word_count),
            ctypes.byref(self._resource_span_scan_tracked_word_count),
            ctypes.byref(self._resource_span_scan_skipped_word_count),
            ctypes.byref(
                self._resource_span_scan_aggregated_index_word_count
            ),
        ):
            raise NativeExecutorError("native resource span method finish failed")
        return self._resource_span_scan_result()

    def export_resource_method_span_state(self) -> bytes:
        """Copy persistent parser state for a replacement native executor."""
        return bytes(self._resource_span_scan_state)

    def import_resource_method_span_state(self, state: bytes) -> None:
        """Restore parser state retained by a previous native executor."""
        expected_size = ctypes.sizeof(_ResourceSpanScanState)
        if len(state) != expected_size:
            raise ValueError("native resource span state has an invalid size")
        ctypes.memmove(
            ctypes.byref(self._resource_span_scan_state),
            state,
            expected_size,
        )

    def _resource_span_scan_result(
        self,
    ) -> tuple[Any, Any, Any, int, int, int, int, int]:
        return (
            self._resource_span_scan_methods,
            self._resource_span_scan_values,
            self._resource_span_scan_positions,
            int(self._resource_span_scan_output_count.value),
            int(self._resource_span_scan_data_word_count.value),
            int(self._resource_span_scan_tracked_word_count.value),
            int(self._resource_span_scan_skipped_word_count.value),
            int(
                self._resource_span_scan_aggregated_index_word_count.value
            ),
        )

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

    def update_cooperative_scheduler(
        self,
        state: NativeCooperativeSchedulerState,
        *,
        steps: int,
        completed_flips: int,
        instruction_quantum: int,
        force_instruction_tick: bool = False,
    ) -> tuple[int, int]:
        instruction_ticks = ctypes.c_uint64()
        video_ticks = ctypes.c_uint64()
        self._cooperative_scheduler(
            ctypes.byref(state),
            max(0, int(steps)),
            max(0, int(completed_flips)),
            max(1, int(instruction_quantum)),
            bool(force_instruction_tick),
            ctypes.byref(instruction_ticks),
            ctypes.byref(video_ticks),
        )
        return int(instruction_ticks.value), int(video_ticks.value)

    def synchronize_worker_lifecycle(
        self,
        state: NativeWorkerLifecycleState,
        workers: Iterable[dict[str, Any]],
        *,
        primary_handle: int = 0,
    ) -> None:
        descriptors = []
        for worker in workers:
            handle = worker.get("handle")
            start_address = worker.get("start_address")
            if not isinstance(handle, int) or not isinstance(start_address, int):
                continue
            descriptors.append(
                NativeWorkerLifecycleEntry(
                    handle,
                    start_address,
                    int(worker.get("start_context1") or 0),
                    int(worker.get("start_context2") or 0),
                    (
                        NATIVE_WORKER_SUSPENDED
                        if worker.get("suspended")
                        else NATIVE_WORKER_READY
                    ),
                    0,
                )
            )
        storage = (NativeWorkerLifecycleEntry * len(descriptors))(*descriptors)
        self._worker_lifecycle_sync(
            ctypes.byref(state),
            storage if descriptors else None,
            len(descriptors),
            int(primary_handle),
        )

    def set_worker_lifecycle_status(
        self,
        state: NativeWorkerLifecycleState,
        handle: int,
        status: int,
    ) -> bool:
        return bool(
            self._worker_lifecycle_set_status(
                ctypes.byref(state), int(handle), int(status)
            )
        )

    def select_runnable_workers(
        self,
        state: NativeWorkerLifecycleState,
    ) -> tuple[int, ...]:
        handles = (ctypes.c_uint32 * NATIVE_WORKER_LIFECYCLE_CAPACITY)()
        count = int(
            self._worker_lifecycle_select(
                ctypes.byref(state), handles, len(handles)
            )
        )
        return tuple(int(handles[index]) for index in range(count))

    def shutdown_normal_runtime(self, state: NativeHostServiceState) -> None:
        self._runtime_shutdown(ctypes.byref(state))

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
            [
                Any,
                Any,
                Any,
                Any,
                Any,
                Any,
                Any,
                int,
                int,
                bool,
                int,
                Any,
                Any,
                int,
            ],
            None,
        ]
        | None = None,
        memory_write_span_observer: Callable[..., None] | None = None,
        memory_write_batch_address_base: int = 0,
        memory_write_observer_yield_header: int = 0,
        memory_write_observer_ranges_provider: Callable[
            [], Iterable[tuple[int, int]]
        ]
        | None = None,
        max_steps: int = 0,
        max_memory_pages: int = 65536,
        slice_steps: int = 0,
        slice_steps_provider: Callable[[int], int] | None = None,
        yield_handler: Callable[[CpuState, SparseMemory, int], bool | None] | None = None,
        yield_predicate: Callable[[], bool] | None = None,
        call_handler_yield_predicate: Callable[[int], bool] | None = None,
        shared_memory_handler_predicate: Callable[[int], bool] | None = None,
        profile_hot_paths: bool = True,
        capture_observed_write_provenance: bool = True,
        direct_observed_write_transport: bool = False,
        use_shared_memory_view: bool = True,
        defer_dirty_sync_at_yield: bool = False,
        dispatch_host_calls_in_native: bool = False,
        native_host_services: NativeHostServiceState | None = None,
        dispatch_observers_in_native: bool = False,
    ) -> int:
        """Run until the step budget, an unhandled target, or callback exception."""
        run_started_ns = time.perf_counter_ns()
        if profile_hot_paths and self._dispatch_edge_keys is None:
            edge_capacity = self._dispatch_edge_capacity
            self._dispatch_edge_keys = (ctypes.c_uint64 * edge_capacity)()
            self._dispatch_edge_reasons = (ctypes.c_uint8 * edge_capacity)()
            self._dispatch_edge_counts = (ctypes.c_uint64 * edge_capacity)()
            self._dispatch_edge_touched_slots = (
                ctypes.c_uint32 * edge_capacity
            )()
        if self._dispatch_edge_counts is not None:
            for index in range(int(self._dispatch_edge_touched_count.value)):
                slot = int(self._dispatch_edge_touched_slots[index])
                self._dispatch_edge_counts[slot] = 0
        self._dispatch_edge_touched_count.value = 0
        self._dispatch_edge_overflow_count.value = 0
        for index in range(int(self._dispatch_touched_count.value)):
            slot = int(self._dispatch_touched_slots[index])
            self._dispatch_target_call_counts[slot] = 0
            self._dispatch_target_step_counts[slot] = 0
            reason_base = slot * _NATIVE_MODULE_EXIT_REASON_COUNT
            for reason_index in range(_NATIVE_MODULE_EXIT_REASON_COUNT):
                self._dispatch_target_exit_reason_counts[
                    reason_base + reason_index
                ] = 0
        self._dispatch_touched_count.value = 0
        for reason_index in range(_NATIVE_MODULE_EXIT_REASON_COUNT):
            self._dispatch_exit_reason_counts[reason_index] = 0
        for index in range(len(self._native_fast_path_call_counts)):
            self._native_fast_path_call_counts[index] = 0
        performance_counts = {
            "native_module_count": len(self._libraries),
            "native_dispatch_count": 0,
            "native_module_call_count": 0,
            "native_host_call_dispatch_count": 0,
            "native_host_service_call_count": 0,
            "native_cold_host_call_count": 0,
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
            "native_observed_write_span_count": 0,
            "native_observed_write_span_batch_count": 0,
            "native_observed_write_span_byte_count": 0,
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
            "observer_drain_deferred_count": 0,
            "empty_dependency_sync_bypass_count": 0,
            "memory_callback_policy_cache_hit_count": 0,
            "memory_callback_policy_cache_miss_count": 0,
            "observed_write_packet_yield_count": 0,
            "zero_read_callback_bypass_count": 0,
            "direct_observed_write_transport": bool(
                direct_observed_write_transport
            ),
            "direct_observed_write_count": 0,
            "direct_observed_write_byte_count": 0,
            "observed_write_provenance_enabled": bool(
                capture_observed_write_provenance
            ),
            "shared_memory_view_enabled": False,
            "shared_memory_handler_sync_bypass_count": 0,
            "shared_memory_handler_sync_fallback_count": 0,
            "shared_memory_observer_sync_bypass_count": 0,
            "shared_memory_yield_sync_bypass_count": 0,
            "slice_quantum_change_count": 0,
        }
        observer_address_counts: Counter[int] = Counter()
        self.current_run_metrics = performance_counts
        performance_timings: dict[str, dict[str, int]] = {}
        handler_timings: dict[int, dict[str, Any]] = {}
        memory_callback_samples: dict[tuple[str, int], dict[str, int]] = {}
        memory_callback_sample_count = 0
        memory_callback_dropped_sample_count = 0
        memory_callback_dropped_read_sample_count = 0

        def record_performance_duration(name: str, elapsed_ns: int) -> None:
            elapsed_us = max(0, int(elapsed_ns) // 1_000)
            metric = performance_timings.setdefault(
                name,
                {"count": 0, "total_us": 0, "max_us": 0},
            )
            metric["count"] += 1
            metric["total_us"] += elapsed_us
            metric["max_us"] = max(metric["max_us"], elapsed_us)

        def record_performance(name: str, started_ns: int) -> None:
            record_performance_duration(
                name,
                time.perf_counter_ns() - started_ns,
            )

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

        def begin_memory_callback_sample(
            kind: str,
            address: int,
            callback_count: int,
            *,
            exact: bool,
        ) -> tuple[tuple[str, int], int] | None:
            if (
                (callback_count - 1) % NATIVE_MEMORY_CALLBACK_SAMPLE_INTERVAL
                != 0
            ):
                return None
            sampled_address = address if exact else address & ~0xFFF
            key = (kind, sampled_address & 0xFFFFFFFF)
            return key, time.perf_counter_ns()

        def finish_memory_callback_sample(
            sample: tuple[tuple[str, int], int] | None,
        ) -> None:
            nonlocal memory_callback_sample_count
            nonlocal memory_callback_dropped_sample_count
            nonlocal memory_callback_dropped_read_sample_count
            if sample is None:
                return
            key, started_ns = sample
            elapsed_ns = max(0, time.perf_counter_ns() - started_ns)
            memory_callback_sample_count += 1
            if (
                key not in memory_callback_samples
                and len(memory_callback_samples)
                    >= NATIVE_MEMORY_CALLBACK_SAMPLE_KEY_LIMIT
            ):
                memory_callback_dropped_sample_count += 1
                if key[0].startswith(("exact_", "page_miss_")):
                    memory_callback_dropped_read_sample_count += 1
                return
            metric = memory_callback_samples.setdefault(
                key,
                {"sample_count": 0, "total_ns": 0, "max_ns": 0},
            )
            metric["sample_count"] += 1
            metric["total_ns"] += elapsed_ns
            metric["max_ns"] = max(metric["max_ns"], elapsed_ns)

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
        write_callback_pages = self._write_callback_pages_table
        zero_read_callback_pages = self._zero_read_callback_pages_table
        dirty_pages = self._dirty_pages
        dirty_page_generations = self._dirty_page_generations
        dirty_page_indices = self._dirty_page_indices
        dirty_page_min_offsets = self._dirty_page_min_offsets
        dirty_page_max_offsets = self._dirty_page_max_offsets
        page_buffers = self._page_buffers
        page_generations = self._page_generations
        native_direct_observed_consumer = bool(
            direct_observed_write_transport
            and native_host_services is not None
            and native_host_services.normal_runtime_enabled
        )
        observed_write_capacity = (
            262_144
            if (
                memory_write_observer is not None
                or memory_write_batch_observer is not None
                or memory_write_span_observer is not None
                or native_direct_observed_consumer
            )
            and memory_write_observer_ranges_provider is not None
            else 0
        )
        memory_write_observer_yield_header = int(
            memory_write_observer_yield_header
        )
        if not 0 <= memory_write_observer_yield_header <= 0xFFFFFFFF:
            raise NativeExecutorError(
                "memory_write_observer_yield_header must fit in uint32"
            )
        if memory_write_observer_yield_header and not observed_write_capacity:
            raise NativeExecutorError(
                "memory_write_observer_yield_header requires an observed-write "
                "callback and range provider"
            )
        if direct_observed_write_transport and (
            (
                memory_write_span_observer is None
                and not native_direct_observed_consumer
            )
            or not observed_write_capacity
        ):
            raise NativeExecutorError(
                "direct observed-write transport requires a span observer and "
                "range provider"
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
                ctypes.c_uint64 * observed_write_capacity
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
            self._packed_observed_write_texture_payload = (
                ctypes.c_uint8 * (observed_write_capacity * 8)
            )()
            self._packed_observed_write_texture_runs = (
                ctypes.c_uint32 * (observed_write_capacity * 4)
            )()
            self._direct_observed_payload = (
                ctypes.c_uint8 * (observed_write_capacity * 8)
            )()
            self._direct_observed_span_addresses = (
                ctypes.c_uint32 * observed_write_capacity
            )()
            self._direct_observed_span_payload_offsets = (
                ctypes.c_uint32 * observed_write_capacity
            )()
            self._direct_observed_span_payload_sizes = (
                ctypes.c_uint32 * observed_write_capacity
            )()
            self._direct_observed_span_write_counts = (
                ctypes.c_uint32 * observed_write_capacity
            )()
            self._direct_observed_span_flags = (
                ctypes.c_uint8 * observed_write_capacity
            )()
            self._observed_write_capacity = observed_write_capacity
        observed_write_eips = self._observed_write_eips
        observed_write_source_addresses = self._observed_write_source_addresses
        observed_write_addresses = self._observed_write_addresses
        observed_write_values = self._observed_write_values
        observed_write_steps = self._observed_write_steps
        observed_write_sizes = self._observed_write_sizes
        packed_observed_write_records = self._packed_observed_write_records
        packed_observed_write_texture_payload = (
            self._packed_observed_write_texture_payload
        )
        packed_observed_write_texture_runs = self._packed_observed_write_texture_runs
        direct_observed_payload = self._direct_observed_payload
        direct_observed_span_addresses = self._direct_observed_span_addresses
        direct_observed_span_payload_offsets = (
            self._direct_observed_span_payload_offsets
        )
        direct_observed_span_payload_sizes = self._direct_observed_span_payload_sizes
        direct_observed_span_write_counts = self._direct_observed_span_write_counts
        direct_observed_span_flags = self._direct_observed_span_flags
        packed_observed_write_texture_run_count = (
            self._packed_observed_write_texture_run_count
        )
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
        native_callback_requires_observer_drain = getattr(
            memory,
            "native_memory_callback_requires_observer_drain",
            None,
        )
        native_callback_policy_static = bool(
            getattr(memory, "native_memory_callback_policy_static", False)
        )
        callback_dependency_page_cache: dict[
            tuple[int, int, bool], set[int] | None
        ] = {}
        callback_observer_drain_cache: dict[tuple[int, int, bool], bool] = {}
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

        resident_page_indices = getattr(memory, "native_resident_page_indices", None)
        if not cache_context_reused and callable(resident_page_indices):
            for page in resident_page_indices():
                cache_page(int(page) << 12)

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
                    pending_address = int(
                        context.observed_write_packet_next_address
                    )
                    if pending_address and not start <= pending_address < end:
                        context.observed_write_packet_next_address = 0
                    return
            context.observed_write_packet_next_address = 0

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
            cache_key = (address & 0xFFFFFFFF, int(size), bool(is_write))
            if native_callback_policy_static and cache_key in callback_dependency_page_cache:
                performance_counts["memory_callback_policy_cache_hit_count"] += 1
                return callback_dependency_page_cache[cache_key]
            performance_counts["memory_callback_policy_cache_miss_count"] += 1
            dependencies = native_callback_dependency_addresses(
                address,
                size,
                is_write=is_write,
            )
            if dependencies is None:
                result = None
            else:
                result = {
                    cache_address(dependency) >> 12
                    for dependency in dependencies
                    if cacheable_address(dependency)
                }
            if native_callback_policy_static:
                callback_dependency_page_cache[cache_key] = result
            return result

        def callback_requires_observer_drain(
            address: int,
            size: int,
            *,
            is_write: bool,
        ) -> bool:
            if not callable(native_callback_requires_observer_drain):
                return True
            cache_key = (address & 0xFFFFFFFF, int(size), bool(is_write))
            if native_callback_policy_static and cache_key in callback_observer_drain_cache:
                performance_counts["memory_callback_policy_cache_hit_count"] += 1
                return callback_observer_drain_cache[cache_key]
            performance_counts["memory_callback_policy_cache_miss_count"] += 1
            result = bool(
                native_callback_requires_observer_drain(
                    address,
                    size,
                    is_write=is_write,
                )
            )
            if native_callback_policy_static:
                callback_observer_drain_cache[cache_key] = result
            return result

        def sync_dirty_pages(
            required_pages: set[int] | None = None,
            *,
            drain_observed_writes: bool = True,
        ) -> None:
            performance_counts["dirty_sync_call_count"] += 1
            selective = required_pages is not None
            if selective:
                performance_counts["selective_dirty_sync_call_count"] += 1
            observed_write_work = bool(
                drain_observed_writes
                and (
                    context.observed_write_count
                    or context.direct_observed_span_count
                )
            )
            if (
                context.observed_write_count
                or context.direct_observed_span_count
            ) and not drain_observed_writes:
                performance_counts["observer_drain_deferred_count"] += 1
            if not context.dirty_page_count and not observed_write_work:
                performance_counts["dirty_sync_no_work_count"] += 1
                return
            if selective and not required_pages and not observed_write_work:
                performance_counts["selective_dirty_sync_no_match_count"] += 1
                return
            started_ns = time.perf_counter_ns()
            if drain_observed_writes:
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
            writeback_loop_started_ns = (
                time.perf_counter_ns() if profile_hot_paths else 0
            )
            payload_materialize_ns = 0
            memory_commit_ns = 0
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
                phase_started_ns = (
                    time.perf_counter_ns() if profile_hot_paths else 0
                )
                # A ctypes-array slice materializes a Python list before bytes(),
                # making large dirty-range writeback scale per element in Python.
                # The buffer protocol performs the identical copy in native code.
                payload = memoryview(buffer).cast("B")[start:end].tobytes()
                if profile_hot_paths:
                    payload_materialize_ns += (
                        time.perf_counter_ns() - phase_started_ns
                    )
                    phase_started_ns = time.perf_counter_ns()
                write_native_page_range(
                    (page << 12) + start,
                    payload,
                )
                if profile_hot_paths:
                    memory_commit_ns += time.perf_counter_ns() - phase_started_ns
                dirty_pages[page] = 0
                dirty_page_generations[page] = 0
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
            if profile_hot_paths:
                writeback_loop_elapsed_ns = (
                    time.perf_counter_ns() - writeback_loop_started_ns
                )
                record_performance_duration(
                    "dirty_page_writeback_loop",
                    writeback_loop_elapsed_ns,
                )
                record_performance_duration(
                    "dirty_page_payload_materialize",
                    payload_materialize_ns,
                )
                record_performance_duration(
                    "dirty_page_memory_commit",
                    memory_commit_ns,
                )
            record_performance("dirty_page_sync", started_ns)

        def sync_callback_boundary(
            required_pages: set[int] | None,
            *,
            drain_observed_writes: bool = True,
        ) -> None:
            if required_pages is not None and not required_pages:
                observed_write_work = bool(
                    drain_observed_writes
                    and (
                        context.observed_write_count
                        or context.direct_observed_span_count
                    )
                )
                if not observed_write_work:
                    performance_counts["empty_dependency_sync_bypass_count"] += 1
                    if (
                        context.observed_write_count
                        or context.direct_observed_span_count
                    ) and not drain_observed_writes:
                        performance_counts["observer_drain_deferred_count"] += 1
                    return
            sync_dirty_pages(
                required_pages,
                drain_observed_writes=drain_observed_writes,
            )

        def drain_native_write_log() -> None:
            span_count = int(context.direct_observed_span_count)
            count = int(context.observed_write_count)
            if count == 0 and span_count == 0:
                return
            performance_counts["native_observed_write_drain_count"] += 1
            if span_count:
                if memory_write_span_observer is None:
                    raise NativeExecutorError(
                        "native direct-write spans have no configured observer"
                    )
                span_started_ns = time.perf_counter_ns()
                logical_write_count = sum(
                    int(direct_observed_span_write_counts[index])
                    for index in range(span_count)
                )
                payload_size = int(context.direct_observed_payload_size)
                memory_write_span_observer(
                    direct_observed_payload,
                    payload_size,
                    direct_observed_span_addresses,
                    direct_observed_span_payload_offsets,
                    direct_observed_span_payload_sizes,
                    direct_observed_span_write_counts,
                    direct_observed_span_flags,
                    span_count,
                    int(context.observed_write_range_start),
                    int(context.observed_write_range_end),
                    int(memory_write_batch_address_base) & 0xFFFFFFFF,
                )
                performance_counts["native_observed_write_count"] += (
                    logical_write_count
                )
                performance_counts["native_observed_write_batch_count"] += 1
                performance_counts["native_observed_write_span_count"] += span_count
                performance_counts["native_observed_write_span_batch_count"] += 1
                performance_counts["native_observed_write_span_byte_count"] += (
                    payload_size
                )
                record_performance(
                    "memory_write_observer_span_batch",
                    span_started_ns,
                )
                context.direct_observed_payload_size = 0
                context.direct_observed_span_count = 0
                context.direct_observed_span_sealed = False
            if count == 0:
                return
            performance_counts["native_observed_write_count"] += count
            if memory_write_batch_observer is not None:
                batch_started_ns = time.perf_counter_ns()
                packed_flags = int(
                    self._observed_write_packer(
                        packed_observed_write_records,
                        observed_write_capacity,
                        packed_observed_write_texture_payload,
                        packed_observed_write_texture_runs,
                        ctypes.byref(packed_observed_write_texture_run_count),
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
                    int(context.observed_write_range_end),
                    packed_observed_write_texture_payload,
                    packed_observed_write_texture_runs,
                    int(packed_observed_write_texture_run_count.value),
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
            dirty_page_generations[page] = (
                int(dirty_page_generations[page]) + 1
            ) & 0xFFFFFFFF or 1
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

        def requires_read_memory_callback(address: int, size: int) -> bool:
            return any(
                cache_address(address + offset)
                in self._read_callback_byte_addresses
                for offset in range(size)
            )

        def requires_write_memory_callback(address: int, size: int) -> bool:
            return any(
                cache_address(address + offset)
                in self._write_callback_byte_addresses
                for offset in range(size)
            )

        def is_zero_guarded_read_callback(address: int, size: int) -> bool:
            return (
                size == 4
                and cache_address(address) in self._zero_read_callback_addresses
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
            exact_callback = requires_read_memory_callback(address, 4)
            if exact_callback:
                performance_counts["exact_read_u32_callback_count"] += 1
                sample = begin_memory_callback_sample(
                    "exact_u32",
                    memory_address,
                    performance_counts["exact_read_u32_callback_count"],
                    exact=True,
                )
                drain_observed_writes = callback_requires_observer_drain(
                    memory_address,
                    4,
                    is_write=False,
                )
                sync_callback_boundary(
                    callback_dependency_pages(
                        memory_address,
                        4,
                        is_write=False,
                    ),
                    drain_observed_writes=drain_observed_writes,
                )
            else:
                performance_counts["page_miss_read_u32_callback_count"] += 1
                sample = begin_memory_callback_sample(
                    "page_miss_u32",
                    memory_address,
                    performance_counts["page_miss_read_u32_callback_count"],
                    exact=False,
                )
            value = memory.read_u32(memory_address)
            if exact_callback:
                refresh_cached_changes(memory_address)
                if is_zero_guarded_read_callback(address, 4):
                    cache_page(memory_address)
            else:
                cache_page(memory_address)
            finish_memory_callback_sample(sample)
            return value

        def read8(_user, address: int) -> int:
            performance_counts["read_u8_callback_count"] += 1
            memory_address = cache_address(address)
            exact_callback = requires_read_memory_callback(address, 1)
            if exact_callback:
                performance_counts["exact_read_u8_callback_count"] += 1
                sample = begin_memory_callback_sample(
                    "exact_u8",
                    memory_address,
                    performance_counts["exact_read_u8_callback_count"],
                    exact=True,
                )
                drain_observed_writes = callback_requires_observer_drain(
                    memory_address,
                    1,
                    is_write=False,
                )
                sync_callback_boundary(
                    callback_dependency_pages(
                        memory_address,
                        1,
                        is_write=False,
                    ),
                    drain_observed_writes=drain_observed_writes,
                )
            else:
                performance_counts["page_miss_read_u8_callback_count"] += 1
                sample = begin_memory_callback_sample(
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
            finish_memory_callback_sample(sample)
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
            sample = begin_memory_callback_sample(
                "write_u32",
                memory_address,
                performance_counts["write_u32_callback_count"],
                exact=True,
            )
            check_write(memory_address)
            # Preserve ordering between cached native writes and a subsequent
            # callback/MMIO write.
            drain_native_write_log()
            if memory_write_observer is not None:
                memory_write_observer(
                    int(context.eip), memory_address, 4, value, int(context.steps)
                )
            if requires_write_memory_callback(address, 4):
                sync_callback_boundary(
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
                finish_memory_callback_sample(sample)
                return
            page = memory_address >> 12
            if cacheable_address(memory_address) and (memory_address & 0xFFF) <= 0xFFC:
                cache_page(memory_address)
            if page in page_buffers and (memory_address & 0xFFF) <= 0xFFC:
                ctypes.c_uint32.from_address(
                    int(page_table[page]) + (memory_address & 0xFFF)
                ).value = value
                mark_dirty_page(page, memory_address & 0xFFF, 4)
                finish_memory_callback_sample(sample)
                return
            memory.write_u32(memory_address, value)
            if yield_predicate is not None and yield_predicate():
                context.yield_requested = True
            finish_memory_callback_sample(sample)

        def write8(_user, address: int, value: int) -> None:
            performance_counts["write_u8_callback_count"] += 1
            memory_address = cache_address(address)
            sample = begin_memory_callback_sample(
                "write_u8",
                memory_address,
                performance_counts["write_u8_callback_count"],
                exact=True,
            )
            check_write(memory_address)
            drain_native_write_log()
            if memory_write_observer is not None:
                memory_write_observer(
                    int(context.eip), memory_address, 1, value, int(context.steps)
                )
            if requires_write_memory_callback(address, 1):
                sync_callback_boundary(
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
                finish_memory_callback_sample(sample)
                return
            page = memory_address >> 12
            if cacheable_address(memory_address):
                cache_page(memory_address)
            if page in page_buffers:
                ctypes.c_uint8.from_address(
                    int(page_table[page]) + (memory_address & 0xFFF)
                ).value = value
                mark_dirty_page(page, memory_address & 0xFFF, 1)
                finish_memory_callback_sample(sample)
                return
            memory.write(memory_address, bytes((value,)))
            if yield_predicate is not None and yield_predicate():
                context.yield_requested = True
            finish_memory_callback_sample(sample)

        write_u32 = _WriteU32(guard(write32))
        read_u8 = _ReadU8(guard(read8, 0))
        write_u8 = _WriteU8(guard(write8))
        unused_call = _Call(lambda _user, _target, _context: None)

        def observe(_user, context_pointer) -> None:
            started_ns = time.perf_counter_ns()
            performance_counts["observer_callback_count"] += 1
            observer_address_counts[int(context_pointer.contents.eip)] += 1
            if step_observer is None:
                record_performance("observer_callback", started_ns)
                return
            try:
                if shared_memory_view_enabled:
                    drain_native_write_log()
                    performance_counts[
                        "shared_memory_observer_sync_bypass_count"
                    ] += 1
                else:
                    sync_dirty_pages()
                _state_from_context(state, context_pointer.contents)
                step_observer(state, memory, trace, int(context_pointer.contents.steps))
                bind_shared_memory_view()
                _update_context(context_pointer.contents, state)
                invalidate_changed_pages()
            finally:
                record_performance("observer_callback", started_ns)

        observe_callback = _Observe(guard(observe))
        set_native_page_cache_view = getattr(
            memory,
            "set_native_page_cache_view",
            None,
        )
        clear_native_page_cache_view = getattr(
            memory,
            "clear_native_page_cache_view",
            None,
        )
        has_native_page_cache_view = getattr(
            memory,
            "has_native_page_cache_view",
            None,
        )
        shared_memory_view_enabled = bool(
            use_shared_memory_view and callable(set_native_page_cache_view)
        )

        def commit_shared_page(page: int) -> None:
            page = int(page)
            if 0 <= page < len(dirty_pages) and dirty_pages[page]:
                sync_dirty_pages(
                    {page},
                    drain_observed_writes=False,
                )

        def bind_shared_memory_view() -> None:
            if not shared_memory_view_enabled:
                return
            if callable(has_native_page_cache_view) and has_native_page_cache_view(
                page_buffers
            ):
                return
            set_native_page_cache_view(
                page_buffers,
                page_generations,
                commit_shared_page,
                dirty_pages,
                dirty_page_generations,
            )

        deferred_yield_sync_enabled = bool(
            defer_dirty_sync_at_yield and shared_memory_view_enabled
        )
        performance_counts["deferred_dirty_sync_at_yield"] = (
            deferred_yield_sync_enabled
        )

        def sync_yield_boundary() -> None:
            if deferred_yield_sync_enabled:
                drain_native_write_log()
                performance_counts[
                    "shared_memory_yield_sync_bypass_count"
                ] += 1
                return
            sync_dirty_pages()

        def invoke_call_handler(
            target: int,
            active_context: _Context,
        ) -> bool:
            handler = handlers[target]
            use_shared_handler_view = bool(
                shared_memory_view_enabled
                and (
                    shared_memory_handler_predicate is None
                    or shared_memory_handler_predicate(target)
                )
            )
            if use_shared_handler_view:
                drain_native_write_log()
                performance_counts[
                    "shared_memory_handler_sync_bypass_count"
                ] += 1
            else:
                if shared_memory_view_enabled:
                    performance_counts[
                        "shared_memory_handler_sync_fallback_count"
                    ] += 1
                sync_dirty_pages()
            _state_from_context(state, active_context)
            performance_counts["handler_call_count"] += 1
            handler_started_ns = time.perf_counter_ns()
            try:
                handler(state, memory, target, trace)
            except BaseException:
                bind_shared_memory_view()
                sync_dirty_pages()
                raise
            finally:
                record_handler_performance(target, handler, handler_started_ns)
                record_performance("call_handler", handler_started_ns)
                bind_shared_memory_view()
            state.eip = memory.read_u32(state.get_register("esp"))
            state.set_register("esp", state.get_register("esp") + 4)
            _update_context(active_context, state)
            invalidate_changed_pages()
            return bool(
                call_handler_yield_predicate is not None
                and call_handler_yield_predicate(target)
            )

        native_host_call_yield_requested = [False]

        def dispatch_host_call(
            _user: int,
            target: int,
            context_pointer: ctypes.POINTER(_Context),
        ) -> int:
            active_context = context_pointer.contents
            try:
                if invoke_call_handler(int(target), active_context):
                    performance_counts["call_handler_yield_count"] += 1
                    native_host_call_yield_requested[0] = True
                    active_context.yield_requested = True
                return int(active_context.eip)
            except BaseException as exc:
                callback_error.append(exc)
                active_context.yield_requested = True
                return int(active_context.eip)

        host_call_callback = _HostCall(dispatch_host_call)
        null_host_call_callback = ctypes.cast(None, _HostCall)

        def dispatch_cold_host_call(
            _user: int,
            target: int,
            context_pointer: ctypes.POINTER(_Context),
        ) -> int:
            active_context = context_pointer.contents
            original_esp = int(active_context.esp)
            try:
                if invoke_call_handler(int(target), active_context):
                    performance_counts["call_handler_yield_count"] += 1
                    native_host_call_yield_requested[0] = True
                    active_context.yield_requested = True
                performance_counts["native_cold_host_call_count"] += 1
            except BaseException as exc:
                callback_error.append(exc)
                active_context.yield_requested = True
            # RuntimeAbiBridge performs the stdcall argument adjustment and
            # the generic handler path pops the return address. Restore the
            # entry stack here so the native service table owns the complete
            # ABI continuation for cold calls as it does for hot services.
            active_context.esp = original_esp
            active_context.eip = int(target)
            return int(target)

        cold_host_call_callback = _HostCall(dispatch_cold_host_call)
        native_host_call_dispatch_enabled = bool(
            dispatch_host_calls_in_native
            and handlers
            and self._host_call_addresses
            and not (
                native_host_services is not None
                and native_host_services.normal_runtime_enabled
            )
        )
        performance_counts["native_host_call_dispatch_enabled"] = (
            native_host_call_dispatch_enabled
        )

        if shared_memory_view_enabled:
            bind_shared_memory_view()
        elif callable(clear_native_page_cache_view):
            clear_native_page_cache_view()
        performance_counts["shared_memory_view_enabled"] = (
            shared_memory_view_enabled
        )
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
        context.write_callback_pages = ctypes.cast(
            write_callback_pages, ctypes.POINTER(ctypes.c_uint8)
        )
        context.write_callback_address_keys = ctypes.cast(
            self._write_callback_address_keys,
            ctypes.POINTER(ctypes.c_uint32),
        )
        context.write_callback_address_mask = self._write_callback_address_mask
        context.zero_read_callback_pages = ctypes.cast(
            zero_read_callback_pages, ctypes.POINTER(ctypes.c_uint8)
        )
        context.zero_read_callback_address_keys = ctypes.cast(
            self._zero_read_callback_address_keys,
            ctypes.POINTER(ctypes.c_uint32),
        )
        context.zero_read_callback_address_mask = (
            self._zero_read_callback_address_mask
        )
        context.cache_physical_aliases = physical_alias_cache_enabled
        context.direct_observed_write_transport = bool(
            direct_observed_write_transport
        )
        context.dirty_pages = ctypes.cast(dirty_pages, ctypes.POINTER(ctypes.c_uint8))
        context.dirty_page_generations = ctypes.cast(
            dirty_page_generations,
            ctypes.POINTER(ctypes.c_uint32),
        )
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
        native_host_services_enabled = bool(
            dispatch_host_calls_in_native
            and native_host_services is not None
            and (
                native_host_services.entry_count
                or native_host_services.normal_runtime_enabled
            )
        )
        native_host_service_start_count = (
            int(native_host_services.native_call_count)
            if native_host_services_enabled and native_host_services is not None
            else 0
        )
        if native_host_services_enabled and native_host_services is not None:
            context.native_memory_user = ctypes.cast(
                ctypes.pointer(native_host_services),
                ctypes.c_void_p,
            )
            context.native_read_u32 = ctypes.cast(
                self._native_memory_read_u32,
                ctypes.c_void_p,
            ).value
            context.native_write_u32 = ctypes.cast(
                self._native_memory_write_u32,
                ctypes.c_void_p,
            ).value
            context.native_read_u8 = ctypes.cast(
                self._native_memory_read_u8,
                ctypes.c_void_p,
            ).value
            context.native_write_u8 = ctypes.cast(
                self._native_memory_write_u8,
                ctypes.c_void_p,
            ).value
            if dispatch_observers_in_native:
                context.user = ctypes.cast(
                    ctypes.pointer(native_host_services),
                    ctypes.c_void_p,
                )
                context.observe = self._native_observe_callback
        else:
            context.native_memory_user = None
            context.native_read_u32 = None
            context.native_write_u32 = None
            context.native_read_u8 = None
            context.native_write_u8 = None
        def bind_native_host_service_view() -> None:
            if not native_host_services_enabled or native_host_services is None:
                return
            native_host_services.cold_call = (
                None
                if native_host_services.normal_runtime_enabled
                else ctypes.cast(cold_host_call_callback, ctypes.c_void_p).value
            )
            native_host_services.cold_user = None
            native_host_services.read_pages = context.read_pages
            native_host_services.callback_address_keys = (
                context.callback_address_keys
            )
            native_host_services.callback_address_mask = (
                context.callback_address_mask
            )
            native_host_services.write_callback_address_keys = (
                context.write_callback_address_keys
            )
            native_host_services.write_callback_address_mask = (
                context.write_callback_address_mask
            )
            native_host_services.memory_user = context.user
            native_host_services.read_u32 = ctypes.cast(
                read_u32, ctypes.c_void_p
            ).value
            native_host_services.write_u32 = ctypes.cast(
                write_u32, ctypes.c_void_p
            ).value
            native_host_services.read_u8 = ctypes.cast(
                read_u8, ctypes.c_void_p
            ).value
            native_host_services.write_u8 = ctypes.cast(
                write_u8, ctypes.c_void_p
            ).value
            native_host_services.cache_physical_aliases = (
                physical_alias_cache_enabled
            )
            native_host_services.dirty_pages = context.dirty_pages
            native_host_services.dirty_page_generations = (
                context.dirty_page_generations
            )
            native_host_services.dirty_page_indices = context.dirty_page_indices
            native_host_services.dirty_page_min_offsets = (
                context.dirty_page_min_offsets
            )
            native_host_services.dirty_page_max_offsets = (
                context.dirty_page_max_offsets
            )
            native_host_services.dirty_page_count = ctypes.cast(
                ctypes.byref(context, _Context.dirty_page_count.offset),
                ctypes.POINTER(ctypes.c_uint32),
            )
            native_host_services.dirty_page_capacity = (
                context.dirty_page_capacity
            )
        bind_native_host_service_view()
        context.observed_write_eips = (
            ctypes.cast(observed_write_eips, ctypes.POINTER(ctypes.c_uint32))
            if observed_write_capacity and capture_observed_write_provenance
            else None
        )
        context.observed_write_source_addresses = (
            ctypes.cast(
                observed_write_source_addresses,
                ctypes.POINTER(ctypes.c_uint32),
            )
            if observed_write_capacity and capture_observed_write_provenance
            else None
        )
        context.observed_write_addresses = (
            ctypes.cast(observed_write_addresses, ctypes.POINTER(ctypes.c_uint32))
            if observed_write_capacity
            else None
        )
        context.observed_write_values = (
            ctypes.cast(observed_write_values, ctypes.POINTER(ctypes.c_uint64))
            if observed_write_capacity
            else None
        )
        context.observed_write_steps = (
            ctypes.cast(observed_write_steps, ctypes.POINTER(ctypes.c_uint64))
            if observed_write_capacity and capture_observed_write_provenance
            else None
        )
        context.observed_write_sizes = (
            ctypes.cast(observed_write_sizes, ctypes.POINTER(ctypes.c_uint8))
            if observed_write_capacity
            else None
        )
        context.observed_write_count = 0
        context.observed_write_capacity = observed_write_capacity
        context.observed_write_packet_header = memory_write_observer_yield_header
        context.observed_write_packet_next_address = 0
        context.observed_write_packet_yield_count = 0
        context.direct_observed_write_count = 0
        context.direct_observed_write_byte_count = 0
        context.direct_observed_payload = (
            ctypes.cast(direct_observed_payload, ctypes.POINTER(ctypes.c_uint8))
            if direct_observed_write_transport
            else None
        )
        context.direct_observed_payload_size = 0
        context.direct_observed_payload_capacity = (
            observed_write_capacity * 8 if direct_observed_write_transport else 0
        )
        context.direct_observed_span_addresses = (
            ctypes.cast(
                direct_observed_span_addresses,
                ctypes.POINTER(ctypes.c_uint32),
            )
            if direct_observed_write_transport
            else None
        )
        context.direct_observed_span_payload_offsets = (
            ctypes.cast(
                direct_observed_span_payload_offsets,
                ctypes.POINTER(ctypes.c_uint32),
            )
            if direct_observed_write_transport
            else None
        )
        context.direct_observed_span_payload_sizes = (
            ctypes.cast(
                direct_observed_span_payload_sizes,
                ctypes.POINTER(ctypes.c_uint32),
            )
            if direct_observed_write_transport
            else None
        )
        context.direct_observed_span_write_counts = (
            ctypes.cast(
                direct_observed_span_write_counts,
                ctypes.POINTER(ctypes.c_uint32),
            )
            if direct_observed_write_transport
            else None
        )
        context.direct_observed_span_flags = (
            ctypes.cast(
                direct_observed_span_flags,
                ctypes.POINTER(ctypes.c_uint8),
            )
            if direct_observed_write_transport
            else None
        )
        context.direct_observed_span_count = 0
        context.direct_observed_span_capacity = (
            observed_write_capacity if direct_observed_write_transport else 0
        )
        context.direct_observed_span_sealed = False
        context.native_fast_path_address_keys = ctypes.cast(
            self._native_fast_path_address_keys,
            ctypes.POINTER(ctypes.c_uint32),
        )
        context.native_fast_path_call_counts = ctypes.cast(
            self._native_fast_path_call_counts,
            ctypes.POINTER(ctypes.c_uint64),
        )
        context.native_fast_path_address_mask = self._native_fast_path_mask
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

        def requested_slice_steps() -> int:
            requested = (
                int(slice_steps_provider(int(context.steps)))
                if slice_steps_provider is not None
                else int(slice_steps)
            )
            if requested < 0:
                raise NativeExecutorError(
                    "slice_steps_provider must not return a negative value"
                )
            return requested

        active_slice_steps = requested_slice_steps()
        performance_counts["initial_slice_steps"] = active_slice_steps
        next_slice = active_slice_steps if active_slice_steps else 0
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
        module_exit_reason_pointer = ctypes.cast(
            ctypes.byref(context, _Context.module_exit_reason.offset),
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
        host_call_count = ctypes.c_uint64()
        dispatch_self_time_ns = ctypes.c_uint64()
        transitions: deque[dict[str, int]] = deque(maxlen=32)

        def finish(reason: str, target: int, dispatch_eip: int) -> int:
            performance_counts["final_slice_steps"] = active_slice_steps
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
            module_exit_reason_counts = {
                name: int(self._dispatch_exit_reason_counts[index])
                for index, name in enumerate(_NATIVE_MODULE_EXIT_REASON_NAMES)
            }

            def target_exit_reason_counts(slot: int) -> dict[str, int]:
                reason_base = slot * _NATIVE_MODULE_EXIT_REASON_COUNT
                return {
                    name: int(
                        self._dispatch_target_exit_reason_counts[
                            reason_base + index
                        ]
                    )
                    for index, name in enumerate(
                        _NATIVE_MODULE_EXIT_REASON_NAMES
                    )
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
                        "module_exit_reasons": target_exit_reason_counts(slot),
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
            dispatch_edges_by_pair: dict[tuple[int, int], dict[str, Any]] = {}
            if profile_hot_paths and self._dispatch_edge_counts is not None:
                for index in range(
                    int(self._dispatch_edge_touched_count.value)
                ):
                    slot = int(self._dispatch_edge_touched_slots[index])
                    key = int(self._dispatch_edge_keys[slot])
                    entry_target = (key >> 32) & 0xFFFFFFFF
                    exit_target = key & 0xFFFFFFFF
                    edge = dispatch_edges_by_pair.setdefault(
                        (entry_target, exit_target),
                        {
                            "entry_target": entry_target,
                            "entry_target_hex": f"0x{entry_target:08X}",
                            "exit_target": exit_target,
                            "exit_target_hex": f"0x{exit_target:08X}",
                            "module_calls": 0,
                            "module_exit_reasons": {
                                name: 0
                                for name in _NATIVE_MODULE_EXIT_REASON_NAMES
                            },
                        },
                    )
                    edge_count = int(self._dispatch_edge_counts[slot])
                    edge["module_calls"] += edge_count
                    reason_index = int(self._dispatch_edge_reasons[slot])
                    edge["module_exit_reasons"][
                        _NATIVE_MODULE_EXIT_REASON_NAMES[reason_index]
                    ] += edge_count
            dispatch_edges = sorted(
                dispatch_edges_by_pair.values(),
                key=lambda item: (
                    -item["module_calls"],
                    item["entry_target"],
                    item["exit_target"],
                ),
            )
            edge_profiled_module_calls = sum(
                item["module_calls"] for item in dispatch_edges
            )
            reported_dispatch_edges = dispatch_edges[
                :NATIVE_DISPATCH_EDGE_REPORT_LIMIT
            ]
            dropped_dispatch_edge_count = max(
                0,
                len(dispatch_edges) - len(reported_dispatch_edges),
            )
            edge_unclassified_module_calls = max(
                0,
                int(performance_counts["native_module_call_count"])
                - edge_profiled_module_calls,
            )
            ordered_callback_samples = sorted(
                memory_callback_samples.items(),
                key=lambda item: (
                    -item[1]["total_ns"],
                    item[0][0],
                    item[0][1],
                ),
            )

            def callback_sample_records(
                *,
                reads_only: bool,
            ) -> list[dict[str, Any]]:
                records = []
                for (kind, address), metric in ordered_callback_samples:
                    if reads_only and not kind.startswith(("exact_", "page_miss_")):
                        continue
                    records.append(
                        {
                            "kind": kind,
                            "address": address,
                            "address_hex": f"0x{address:08X}",
                            "sample_count": metric["sample_count"],
                            "sampled_total_us": metric["total_ns"] // 1_000,
                            "sampled_average_us": round(
                                metric["total_ns"]
                                / max(1, metric["sample_count"])
                                / 1_000.0,
                                3,
                            ),
                            "sampled_max_us": metric["max_ns"] // 1_000,
                        }
                    )
                    if len(records) >= NATIVE_MEMORY_CALLBACK_HOT_ADDRESS_LIMIT:
                        break
                return records

            read_sample_metrics = [
                (address, metric)
                for (kind, address), metric in ordered_callback_samples
                if kind.startswith(("exact_", "page_miss_"))
            ]
            native_fast_path_counts = [
                {
                    "address": address,
                    "address_hex": f"0x{address:08X}",
                    "name": self._native_fast_paths[address].name,
                    "invocation_count": int(
                        self._native_fast_path_call_counts[slot]
                    ),
                }
                for slot, address in enumerate(
                    self._native_fast_path_addresses_by_slot
                )
                if address
            ]
            native_fast_path_counts.sort(key=lambda item: item["address"])
            self.last_run_summary = {
                "reason": reason,
                "target": target,
                "target_hex": f"0x{target:08X}",
                "dispatch_eip": dispatch_eip,
                "dispatch_eip_hex": f"0x{dispatch_eip:08X}",
                "native_module_cache": self.cache_summary,
                "steps": int(context.steps),
                "step_budget": int(context.step_budget),
                "performance": {
                    "elapsed_us": elapsed_ns // 1_000,
                    "steps_per_second": round(
                        int(context.steps) / elapsed_seconds,
                        3,
                    ),
                    **performance_counts,
                    "observed_write_packet_yield_count": int(
                        context.observed_write_packet_yield_count
                    ),
                    "zero_read_callback_bypass_count": int(
                        context.zero_read_callback_bypass_count
                    ),
                    "direct_observed_write_count": int(
                        context.direct_observed_write_count
                    ),
                    "direct_observed_write_byte_count": int(
                        context.direct_observed_write_byte_count
                    ),
                    "native_fast_path_invocation_count": sum(
                        item["invocation_count"]
                        for item in native_fast_path_counts
                    ),
                    "native_fast_paths": native_fast_path_counts,
                    "observer_callback_addresses": [
                        {
                            "address": address,
                            "address_hex": f"0x{address:08X}",
                            "count": count,
                        }
                        for address, count in observer_address_counts.most_common(32)
                    ],
                    "hot_path_profiling_enabled": bool(profile_hot_paths),
                    "native_module_exit_profile": {
                        "enabled": bool(profile_hot_paths),
                        "reason_counts": module_exit_reason_counts,
                        "classified_module_calls": sum(
                            module_exit_reason_counts.values()
                        ),
                        "unclassified_module_calls": (
                            max(
                                0,
                                int(performance_counts["native_module_call_count"])
                                - sum(module_exit_reason_counts.values()),
                            )
                            if profile_hot_paths
                            else None
                        ),
                    },
                    "native_module_edge_profile": {
                        "enabled": bool(profile_hot_paths),
                        "exact": (
                            bool(profile_hot_paths)
                            and int(self._dispatch_edge_overflow_count.value) == 0
                            and edge_unclassified_module_calls == 0
                            and dropped_dispatch_edge_count == 0
                        ),
                        "table_capacity": (
                            self._dispatch_edge_capacity
                            if profile_hot_paths
                            else 0
                        ),
                        "stored_edge_reason_count": (
                            int(self._dispatch_edge_touched_count.value)
                            if profile_hot_paths
                            else 0
                        ),
                        "unique_edge_count": len(dispatch_edges),
                        "reported_edge_count": len(reported_dispatch_edges),
                        "report_limit": NATIVE_DISPATCH_EDGE_REPORT_LIMIT,
                        "dropped_edge_count": dropped_dispatch_edge_count,
                        "classified_module_calls": edge_profiled_module_calls,
                        "unclassified_module_calls": (
                            edge_unclassified_module_calls
                            if profile_hot_paths
                            else None
                        ),
                        "overflow_module_calls": (
                            int(self._dispatch_edge_overflow_count.value)
                            if profile_hot_paths
                            else 0
                        ),
                        "edges": reported_dispatch_edges,
                    },
                    "read_callback_sampling": {
                        "interval": NATIVE_MEMORY_CALLBACK_SAMPLE_INTERVAL,
                        "address_capacity": NATIVE_MEMORY_CALLBACK_SAMPLE_KEY_LIMIT,
                        "tracked_address_count": len(memory_callback_samples),
                        "sample_count": sum(
                            metric["sample_count"]
                            for _address, metric in read_sample_metrics
                        ),
                        "dropped_new_address_sample_count": (
                            memory_callback_dropped_read_sample_count
                        ),
                        "high_address_sample_count": sum(
                            metric["sample_count"]
                            for address, metric in read_sample_metrics
                            if address >= 0x80000000
                        ),
                        "hot_addresses": callback_sample_records(reads_only=True),
                    },
                    "memory_callback_sampling": {
                        "interval": NATIVE_MEMORY_CALLBACK_SAMPLE_INTERVAL,
                        "address_capacity": NATIVE_MEMORY_CALLBACK_SAMPLE_KEY_LIMIT,
                        "tracked_address_count": len(memory_callback_samples),
                        "sample_count": memory_callback_sample_count,
                        "dropped_new_address_sample_count": (
                            memory_callback_dropped_sample_count
                        ),
                        "hot_addresses": callback_sample_records(reads_only=False),
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
                reason_base = slot * _NATIVE_MODULE_EXIT_REASON_COUNT
                for reason_index in range(_NATIVE_MODULE_EXIT_REASON_COUNT):
                    self._dispatch_target_exit_reason_counts[
                        reason_base + reason_index
                    ] = 0
            self._dispatch_touched_count.value = 0
            for reason_index in range(_NATIVE_MODULE_EXIT_REASON_COUNT):
                self._dispatch_exit_reason_counts[reason_index] = 0
            if self._dispatch_edge_counts is not None:
                for index in range(
                    int(self._dispatch_edge_touched_count.value)
                ):
                    slot = int(self._dispatch_edge_touched_slots[index])
                    self._dispatch_edge_counts[slot] = 0
            self._dispatch_edge_touched_count.value = 0
            self._dispatch_edge_overflow_count.value = 0
            if shared_memory_view_enabled and callable(clear_native_page_cache_view):
                clear_native_page_cache_view()
            return target

        while True:
            performance_counts["native_dispatch_count"] += 1
            dispatch_eip = int(context.eip)
            dispatch_started_ns = time.perf_counter_ns()
            module_call_count.value = 0
            host_call_count.value = 0
            dispatch_self_time_ns.value = 0
            # Cooperative worker execution temporarily shares this service
            # state with a nested executor. Restore this run's callback and
            # page-cache view before every dispatcher residency.
            bind_native_host_service_view()
            target = int(
                self._dispatcher(
                    ctypes.byref(context),
                    dispatch_eip,
                    self._dispatch_keys,
                    self._dispatch_entries,
                    self._dispatch_mask,
                    (
                        self._host_call_keys
                        if (
                            native_host_call_dispatch_enabled
                            or native_host_services_enabled
                        )
                        else None
                    ),
                    self._host_call_mask,
                    (
                        ctypes.byref(native_host_services)
                        if native_host_services_enabled
                        else None
                    ),
                    (
                        host_call_callback
                        if native_host_call_dispatch_enabled
                        else null_host_call_callback
                    ),
                    None,
                    ctypes.byref(host_call_count),
                    yield_requested_pointer,
                    fault_code_pointer,
                    steps_pointer,
                    step_budget_pointer,
                    ctypes.byref(module_call_count),
                    module_exit_reason_pointer,
                    bool(profile_hot_paths),
                    self._dispatch_exit_reason_counts,
                    self._dispatch_target_exit_reason_counts,
                    self._dispatch_target_call_counts,
                    self._dispatch_target_step_counts,
                    self._dispatch_touched_slots,
                    ctypes.byref(self._dispatch_touched_count),
                    self._dispatch_edge_keys,
                    self._dispatch_edge_reasons,
                    self._dispatch_edge_counts,
                    self._dispatch_edge_capacity - 1,
                    self._dispatch_edge_touched_slots,
                    ctypes.byref(self._dispatch_edge_touched_count),
                    ctypes.byref(self._dispatch_edge_overflow_count),
                    ctypes.byref(dispatch_self_time_ns),
                )
            )
            performance_counts["native_module_call_count"] += module_call_count.value
            performance_counts["native_host_call_dispatch_count"] += (
                host_call_count.value
            )
            if native_host_services_enabled and native_host_services is not None:
                performance_counts["native_host_service_call_count"] = (
                    int(native_host_services.native_call_count)
                    - native_host_service_start_count
                )
            record_performance("native_dispatch", dispatch_started_ns)
            if profile_hot_paths:
                record_performance_duration(
                    "native_dispatch_self",
                    dispatch_self_time_ns.value,
                )
            if (
                native_host_services_enabled
                and native_host_services is not None
                and native_host_services.normal_runtime_stop_requested
            ):
                sync_dirty_pages()
                return finish("native_runtime_stop", target, dispatch_eip)
            if (
                native_host_services_enabled
                and native_host_services is not None
                and native_host_services.normal_runtime_failure_code
            ):
                failure_code = int(
                    native_host_services.normal_runtime_failure_code
                )
                failure_target = (
                    int(native_host_services.normal_runtime_failure_target)
                    or target
                )
                # A terminal native-runtime failure can leave a partially
                # accumulated direct span. Python is not a normal-runtime
                # render consumer, so preserve the real transport/coverage
                # failure instead of replacing it with an observer error.
                sync_dirty_pages(drain_observed_writes=False)
                finish(
                    "native_runtime_failure",
                    failure_target,
                    dispatch_eip,
                )
                # A native-runtime failure is terminal for this state.  Release
                # its audio worker and loaded host library before surfacing the
                # coverage/transport error; otherwise the native worker keeps
                # an embedded live-test process resident after Python returns.
                self.shutdown_normal_runtime(native_host_services)
                raise NativeExecutorError(
                    "native normal runtime failed with transport code "
                    f"{failure_code} at 0x{failure_target:08X}"
                )
            if context.fault_code:
                fault_code = int(context.fault_code)
                fault_eip = int(context.fault_eip)
                finish("guest_arithmetic_fault", fault_eip, dispatch_eip)
                fault_label = {
                    1: "division by zero",
                    2: "division overflow",
                }.get(fault_code, f"arithmetic fault {fault_code}")
                frame_context = ""
                frame_pointer = int(context.ebp)
                if frame_pointer >= 4:
                    try:
                        def fault_read(address: int, size: int) -> bytes:
                            payload = bytearray()
                            for offset in range(size):
                                active_address = (address + offset) & 0xFFFFFFFF
                                page = active_address >> 12
                                page_buffer = self._page_buffers.get(page)
                                if page_buffer is None:
                                    payload.extend(memory.read(active_address, 1))
                                else:
                                    payload.append(
                                        int(page_buffer[active_address & 0xFFF])
                                    )
                            return bytes(payload)

                        frame_this = int.from_bytes(
                            fault_read(frame_pointer - 4, 4), "little"
                        )
                        frame_return = int.from_bytes(
                            fault_read(frame_pointer + 4, 4), "little"
                        )
                        frame_argument = int.from_bytes(
                            fault_read(frame_pointer + 8, 4), "little"
                        )
                        frame_format = (
                            int.from_bytes(fault_read(frame_this + 4, 4), "little")
                            if frame_this
                            else 0
                        )
                        frame_divisor = (
                            int.from_bytes(
                                fault_read(frame_format + 0x1A, 2),
                                "little",
                            )
                            if frame_format
                            else 0
                        )
                        frame_chain = []
                        active_frame = frame_pointer
                        for _ in range(6):
                            previous_frame = int.from_bytes(
                                fault_read(active_frame, 4), "little"
                            )
                            active_return = int.from_bytes(
                                fault_read(active_frame + 4, 4), "little"
                            )
                            frame_chain.append(
                                f"0x{active_frame:08X}->0x{active_return:08X}"
                            )
                            if previous_frame <= active_frame:
                                break
                            active_frame = previous_frame
                        object_words = (
                            ",".join(
                                f"0x{int.from_bytes(fault_read(frame_this + offset, 4), 'little'):08X}"
                                for offset in range(0, 0x40, 4)
                            )
                            if frame_this
                            else ""
                        )
                        frame_context = (
                            f", frame_this=0x{frame_this:08X}, "
                            f"frame_return=0x{frame_return:08X}, "
                            f"frame_argument=0x{frame_argument:08X}, "
                            f"frame_format=0x{frame_format:08X}, "
                            f"frame_divisor=0x{frame_divisor:04X}, "
                            f"frame_chain=[{','.join(frame_chain)}], "
                            f"frame_object=[{object_words}]"
                        )
                    except (KeyError, ValueError):
                        pass
                service_context = ""
                if (
                    native_host_services_enabled
                    and native_host_services is not None
                ):
                    service_context = (
                        f", last_service=0x{int(native_host_services.last_service_target):08X}"
                        f"/kind{int(native_host_services.last_service_kind)}"
                        f"/value{int(native_host_services.last_service_value)}"
                        f"/result0x{int(native_host_services.last_service_result):08X}"
                        f"/return0x{int(native_host_services.last_service_return_address):08X}"
                    )
                    trace_count = int(native_host_services.service_trace_count)
                    recent_services = ",".join(
                        (
                            f"0x{int(native_host_services.service_trace_targets[index]):08X}"
                            f"@0x{int(native_host_services.service_trace_return_addresses[index]):08X}"
                            f"/k{int(native_host_services.service_trace_kinds[index])}"
                            f"/v{int(native_host_services.service_trace_values[index])}"
                            f"/r0x{int(native_host_services.service_trace_results[index]):08X}"
                        )
                        for index in range(max(0, trace_count - 8), trace_count)
                    )
                    if recent_services:
                        service_context += f", recent_services=[{recent_services}]"
                    last_guest_path = os.fsdecode(
                        native_host_services.last_file_guest_path
                    )
                    last_host_path = os.fsdecode(
                        native_host_services.last_file_host_path
                    )
                    if last_guest_path or last_host_path:
                        service_context += (
                            f", last_file_guest={last_guest_path!r}, "
                            f"last_file_host={last_host_path!r}"
                        )
                edge_context = ""
                edge_profile = self.last_run_summary["performance"][
                    "native_module_edge_profile"
                ]
                if edge_profile["enabled"]:
                    fault_entries = {
                        int(edge["entry_target"])
                        for edge in edge_profile["edges"]
                        if int(edge["exit_target"]) == fault_eip
                    }
                    relevant_edges = [
                        edge
                        for edge in edge_profile["edges"]
                        if int(edge["exit_target"]) == fault_eip
                        or int(edge["exit_target"]) in fault_entries
                    ]
                    if relevant_edges:
                        edge_context = ", fault_edges=[" + ",".join(
                            (
                                f"0x{int(edge['entry_target']):08X}"
                                f"->0x{int(edge['exit_target']):08X}"
                            )
                            for edge in relevant_edges[-8:]
                        ) + "]"
                raise NativeExecutorError(
                    f"native guest {fault_label} at 0x{fault_eip:08X} "
                    f"from dispatch 0x{dispatch_eip:08X} "
                    f"(eax=0x{int(context.eax):08X}, "
                    f"ecx=0x{int(context.ecx):08X}, "
                    f"edx=0x{int(context.edx):08X}, "
                    f"ebp=0x{frame_pointer:08X}{frame_context}"
                    f"{service_context}{edge_context})"
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
            if native_host_call_yield_requested[0]:
                context.yield_requested = False
                sync_dirty_pages()
                return finish(
                    "call_handler_yield",
                    int(context.eip),
                    dispatch_eip,
                )
            handled_requested_yield = False
            if context.yield_requested:
                performance_counts["predicate_yield_count"] += 1
                sync_yield_boundary()
                _state_from_context(state, context)
                context.yield_requested = False
                if yield_handler is not None:
                    yield_started_ns = time.perf_counter_ns()
                    try:
                        stop_requested = yield_handler(
                            state, memory, int(context.steps)
                        ) is False
                    except BaseException:
                        sync_dirty_pages()
                        raise
                    finally:
                        record_performance("yield_handler", yield_started_ns)
                        bind_shared_memory_view()
                    if stop_requested:
                        sync_dirty_pages()
                        return finish("yield_handler_stop", int(context.eip), dispatch_eip)
                performance_counts["slice_yield_count"] += 1
                _update_context(context, state)
                invalidate_changed_pages()
                handled_requested_yield = True
                requested = requested_slice_steps()
                if requested != active_slice_steps:
                    performance_counts["slice_quantum_change_count"] += 1
                    active_slice_steps = requested
                    next_slice = (
                        int(context.steps) + active_slice_steps
                        if active_slice_steps
                        else 0
                    )
                    context.step_budget = (
                        min(max_steps, next_slice)
                        if max_steps and next_slice
                        else max_steps or next_slice
                    )
            if next_slice and context.steps >= next_slice:
                if not handled_requested_yield:
                    sync_yield_boundary()
                    _state_from_context(state, context)
                    if yield_handler is not None:
                        yield_started_ns = time.perf_counter_ns()
                        try:
                            stop_requested = yield_handler(
                                state, memory, int(context.steps)
                            ) is False
                        except BaseException:
                            sync_dirty_pages()
                            raise
                        finally:
                            record_performance("yield_handler", yield_started_ns)
                            bind_shared_memory_view()
                        if stop_requested:
                            sync_dirty_pages()
                            return finish("yield_handler_stop", int(context.eip), dispatch_eip)
                    performance_counts["slice_yield_count"] += 1
                    _update_context(context, state)
                    invalidate_changed_pages()
                if max_steps and context.steps >= max_steps:
                    sync_dirty_pages()
                    return finish("step_budget", target, dispatch_eip)
                requested = requested_slice_steps()
                if requested != active_slice_steps:
                    performance_counts["slice_quantum_change_count"] += 1
                active_slice_steps = requested
                next_slice = (
                    int(context.steps) + active_slice_steps
                    if active_slice_steps
                    else 0
                )
                context.step_budget = (
                    min(max_steps, next_slice)
                    if max_steps and next_slice
                    else max_steps or next_slice
                )
            if max_steps and context.steps >= max_steps:
                sync_dirty_pages()
                return finish("step_budget", target, dispatch_eip)
            handler = handlers.get(target)
            if handler is None:
                if target in self._valid_addresses:
                    continue
                sync_dirty_pages()
                return finish("unhandled_target", target, dispatch_eip)
            if (
                native_host_call_dispatch_enabled
                and target in self._host_call_addresses
            ):
                continue
            if invoke_call_handler(target, context):
                performance_counts["call_handler_yield_count"] += 1
                sync_dirty_pages()
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
