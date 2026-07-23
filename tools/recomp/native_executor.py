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
#include <chrono>
#include <cstdint>
#include <cstring>

using B2RNativeEntry = uint32_t (__cdecl *)(void*);
using B2RDispatchClock = std::chrono::steady_clock;
static constexpr uint32_t B2R_MODULE_EXIT_REASON_COUNT = 9u;

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
        uint32_t slot = (target * 2654435761u) & mask;
        while (entries[slot] != nullptr && keys[slot] != target) {
            slot = (slot + 1u) & mask;
        }
        const B2RNativeEntry entry = entries[slot];
        if (entry == nullptr) {
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
            stage_register == 0x18u;
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
            } else if (stage_register == 0x18u) {
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
    ("module_exit_reason", ctypes.c_uint32),
    ("steps", ctypes.c_uint64),
    ("step_budget", ctypes.c_uint64),
    ("yield_requested", ctypes.c_bool),
    ("direct_observed_write_transport", ctypes.c_bool),
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
    ("write_callback_pages", ctypes.POINTER(ctypes.c_uint8)),
    ("write_callback_address_keys", ctypes.POINTER(ctypes.c_uint32)),
    ("write_callback_address_mask", ctypes.c_uint32),
    ("zero_read_callback_pages", ctypes.POINTER(ctypes.c_uint8)),
    ("zero_read_callback_address_keys", ctypes.POINTER(ctypes.c_uint32)),
    ("zero_read_callback_address_mask", ctypes.c_uint32),
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
            ("clang-cl-o2-native-module-dispatch-v4\n" + _NATIVE_DISPATCH_SOURCE).encode(
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
        self._dispatch_edge_capacity = table_capacity * 2
        self._dispatch_edge_keys: Any | None = None
        self._dispatch_edge_reasons: Any | None = None
        self._dispatch_edge_counts: Any | None = None
        self._dispatch_edge_touched_slots: Any | None = None
        self._dispatch_edge_touched_count = ctypes.c_uint32()
        self._dispatch_edge_overflow_count = ctypes.c_uint64()
        for address, entry in self._entries_by_address.items():
            if address in callback_addresses:
                continue
            slot = (address * 2654435761) & self._dispatch_mask
            while self._dispatch_entries[slot]:
                slot = (slot + 1) & self._dispatch_mask
            self._dispatch_keys[slot] = address
            self._dispatch_entries[slot] = ctypes.cast(entry, ctypes.c_void_p).value
            self._dispatch_addresses_by_slot[slot] = address

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
        yield_handler: Callable[[CpuState, SparseMemory, int], bool | None] | None = None,
        yield_predicate: Callable[[], bool] | None = None,
        call_handler_yield_predicate: Callable[[int], bool] | None = None,
        shared_memory_handler_predicate: Callable[[int], bool] | None = None,
        profile_hot_paths: bool = True,
        capture_observed_write_provenance: bool = True,
        direct_observed_write_transport: bool = False,
        use_shared_memory_view: bool = True,
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
        }
        self.current_run_metrics = performance_counts
        performance_timings: dict[str, dict[str, int]] = {}
        handler_timings: dict[int, dict[str, Any]] = {}
        memory_callback_samples: dict[tuple[str, int], dict[str, int]] = {}

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
            if sample is None:
                return
            key, started_ns = sample
            elapsed_ns = max(0, time.perf_counter_ns() - started_ns)
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
                or memory_write_span_observer is not None
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
            memory_write_span_observer is None or not observed_write_capacity
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
        dispatch_self_time_ns = ctypes.c_uint64()
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
                        "edges": dispatch_edges,
                    },
                    "read_callback_sampling": {
                        "interval": NATIVE_MEMORY_CALLBACK_SAMPLE_INTERVAL,
                        "sample_count": sum(
                            metric["sample_count"]
                            for _address, metric in read_sample_metrics
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
                        "sample_count": sum(
                            metric["sample_count"]
                            for metric in memory_callback_samples.values()
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
            dispatch_self_time_ns.value = 0
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
            record_performance("native_dispatch", dispatch_started_ns)
            if profile_hot_paths:
                record_performance_duration(
                    "native_dispatch_self",
                    dispatch_self_time_ns.value,
                )
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
                        bind_shared_memory_view()
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
                            bind_shared_memory_view()
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
            _state_from_context(state, context)
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
            _update_context(context, state)
            invalidate_changed_pages()
            if (
                call_handler_yield_predicate is not None
                and call_handler_yield_predicate(target)
            ):
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
