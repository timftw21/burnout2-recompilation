#!/usr/bin/env python3
"""Replay a capsule twice and isolate the first divergent guest transition."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.playability.replay_capsule import (
    ReplayCapsule,
    ReplayCapsuleError,
    capture_replay_capsule,
    clone_capsule_state,
    cpu_state_record,
    load_replay_capsule,
    require_local_capsule_output,
)
from tools.recomp.x86_lifter import (
    CpuState,
    SparseMemory,
    X86ExecutionError,
    execute_lifted_function,
)


EVENT_STREAMS = ("service_sequence", "render_events", "audio_events")


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _events_at_prefix(records: Sequence[Any], prefix_steps: int) -> list[Any]:
    bounded = []
    for record in records:
        if isinstance(record, dict) and isinstance(record.get("step"), int):
            if int(record["step"]) <= prefix_steps:
                bounded.append(record)
        else:
            bounded.append(record)
    return bounded


@dataclass(frozen=True)
class ReplayObservation:
    backend: str
    prefix_steps: int
    steps_executed: int
    exit_reason: str
    state: CpuState
    memory: SparseMemory
    events: dict[str, list[Any]]
    status: str = "ok"
    error: str | None = None
    native_symbol: str | None = None

    def page_hashes(self) -> dict[int, str]:
        return {page_index: _sha256(payload) for page_index, payload in self.memory.export_pages()}

    def event_hashes(self) -> dict[str, str]:
        return {name: _sha256(_canonical_json(self.events.get(name, []))) for name in EVENT_STREAMS}

    def architectural_hash(self) -> str:
        record = {
            "cpu": cpu_state_record(self.state),
            "memory_pages": {
                str(page): digest for page, digest in sorted(self.page_hashes().items())
            },
            "events": self.event_hashes(),
            "status": self.status,
        }
        return _sha256(_canonical_json(record))

    def summary(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "prefix_steps": self.prefix_steps,
            "steps_executed": self.steps_executed,
            "exit_reason": self.exit_reason,
            "status": self.status,
            "error": self.error,
            "architectural_hash": self.architectural_hash(),
            "guest_eip": self.state.eip,
            "guest_eip_hex": f"0x{self.state.eip:08X}",
            "native_symbol": self.native_symbol,
            "page_hashes": {
                f"0x{page * 4096:08X}": digest
                for page, digest in sorted(self.page_hashes().items())
            },
            "event_hashes": self.event_hashes(),
        }


class ReplayBackend(Protocol):
    name: str

    def run_prefix(self, prefix_steps: int) -> ReplayObservation:
        """Run from the capsule checkpoint for exactly the bounded prefix."""


class InterpreterReplayBackend:
    name = "accepted-interpreter"

    def __init__(self, capsule: ReplayCapsule) -> None:
        self._capsule = capsule
        self._function = capsule.combined_function()

    def run_prefix(self, prefix_steps: int) -> ReplayObservation:
        state, memory = clone_capsule_state(self._capsule)
        if prefix_steps == 0:
            return _initial_observation(self._capsule, self.name, state, memory)
        try:
            result = execute_lifted_function(
                self._function,
                state=state,
                memory=memory,
                max_steps=prefix_steps,
                preserve_initial_eip=True,
                record_instruction_trace=False,
                record_trace=False,
                return_on_missing_instruction=True,
            )
            steps_executed = result.steps
            status = "ok"
            error = None
        except X86ExecutionError as exc:
            steps_executed = int(exc.steps)
            status = "fault"
            error = str(exc)
        return ReplayObservation(
            backend=self.name,
            prefix_steps=prefix_steps,
            steps_executed=steps_executed,
            exit_reason="bounded-interpreter",
            state=state,
            memory=memory,
            events=_capsule_events(self._capsule, prefix_steps),
            status=status,
            error=error,
        )


class NativeReplayBackend:
    name = "experimental-native"

    def __init__(
        self,
        capsule: ReplayCapsule,
        *,
        build_dir: Path,
        compiler_cache_mode: str = "auto",
    ) -> None:
        from tools.recomp.native_executor import NativeExecutorError, NativeResumableExecutor

        self._capsule = capsule
        self._error_type = NativeExecutorError
        self._executor = NativeResumableExecutor(
            capsule.combined_function(),
            build_dir=build_dir,
            module_functions=capsule.functions,
            compiler_cache_mode=compiler_cache_mode,
            preserve_debug_symbols=True,
        )

    def run_prefix(self, prefix_steps: int) -> ReplayObservation:
        state, memory = clone_capsule_state(self._capsule)
        if prefix_steps == 0:
            return _initial_observation(self._capsule, self.name, state, memory)
        error = None
        try:
            self._executor.run(
                state,
                memory,
                max_steps=prefix_steps,
                profile_hot_paths=False,
                capture_observed_write_provenance=False,
            )
            status = "ok"
        except self._error_type as exc:
            status = "fault"
            error = str(exc)
        summary = self._executor.last_run_summary or {}
        diagnostics = summary.get("diagnostic_context", {})
        native_symbol = (
            str(diagnostics["native_symbol"])
            if isinstance(diagnostics, dict) and diagnostics.get("native_symbol")
            else None
        )
        return ReplayObservation(
            backend=self.name,
            prefix_steps=prefix_steps,
            steps_executed=int(summary.get("steps", prefix_steps)),
            exit_reason=str(summary.get("reason", "bounded-native")),
            state=state,
            memory=memory,
            events=_capsule_events(self._capsule, prefix_steps),
            status=status,
            error=error,
            native_symbol=native_symbol,
        )


def _capsule_events(capsule: ReplayCapsule, prefix_steps: int) -> dict[str, list[Any]]:
    return {
        name: _events_at_prefix(capsule.events.get(name, []), prefix_steps)
        for name in EVENT_STREAMS
    }


def _initial_observation(
    capsule: ReplayCapsule,
    backend: str,
    state: CpuState,
    memory: SparseMemory,
) -> ReplayObservation:
    return ReplayObservation(
        backend=backend,
        prefix_steps=0,
        steps_executed=0,
        exit_reason="checkpoint",
        state=state,
        memory=memory,
        events=_capsule_events(capsule, 0),
    )


def compare_event_streams(accepted: Sequence[Any], experimental: Sequence[Any]) -> dict[str, Any]:
    first_mismatch = None
    for index, (left, right) in enumerate(zip(accepted, experimental)):
        if _canonical_json(left) != _canonical_json(right):
            first_mismatch = index
            break
    if first_mismatch is None and len(accepted) != len(experimental):
        first_mismatch = min(len(accepted), len(experimental))
    return {
        "matches": first_mismatch is None,
        "first_mismatch": first_mismatch,
        "accepted_count": len(accepted),
        "experimental_count": len(experimental),
        "accepted_hash": _sha256(_canonical_json(list(accepted))),
        "experimental_hash": _sha256(_canonical_json(list(experimental))),
        "accepted_event": (
            accepted[first_mismatch]
            if first_mismatch is not None and first_mismatch < len(accepted)
            else None
        ),
        "experimental_event": (
            experimental[first_mismatch]
            if first_mismatch is not None and first_mismatch < len(experimental)
            else None
        ),
    }


def _changed_registers(
    accepted: ReplayObservation, experimental: ReplayObservation
) -> list[dict[str, Any]]:
    left = cpu_state_record(accepted.state)
    right = cpu_state_record(experimental.state)
    changes = []
    register_names = sorted(set(left["registers"]) | set(right["registers"]))
    for name in register_names:
        accepted_value = left["registers"].get(name)
        experimental_value = right["registers"].get(name)
        if accepted_value != experimental_value:
            changes.append(
                {
                    "register": name,
                    "accepted": accepted_value,
                    "experimental": experimental_value,
                }
            )
    for name in ("eip", "flags", "timestamp_counter", "mxcsr"):
        if left[name] != right[name]:
            changes.append(
                {
                    "register": name,
                    "accepted": left[name],
                    "experimental": right[name],
                }
            )
    for name in ("fpu_stack_bits", "xmm_register_bits", "mmx_registers"):
        if left[name] != right[name]:
            changes.append(
                {
                    "register": name,
                    "accepted_sha256": _sha256(_canonical_json(left[name])),
                    "experimental_sha256": _sha256(_canonical_json(right[name])),
                }
            )
    return changes


def _page_diffs(
    accepted: ReplayObservation, experimental: ReplayObservation
) -> list[dict[str, Any]]:
    left = dict(accepted.memory.export_pages())
    right = dict(experimental.memory.export_pages())
    changes = []
    zero_page = bytes(4096)
    for page_index in sorted(set(left) | set(right)):
        left_payload = left.get(page_index, zero_page)
        right_payload = right.get(page_index, zero_page)
        left_present = page_index in left
        right_present = page_index in right
        if left_payload == right_payload and left_present == right_present:
            continue
        offsets = [
            offset
            for offset, (left_byte, right_byte) in enumerate(zip(left_payload, right_payload))
            if left_byte != right_byte
        ]
        changes.append(
            {
                "page_address": page_index * 4096,
                "page_address_hex": f"0x{page_index * 4096:08X}",
                "accepted_sha256": _sha256(left_payload),
                "experimental_sha256": _sha256(right_payload),
                "accepted_present": left_present,
                "experimental_present": right_present,
                "changed_byte_count": len(offsets),
                "first_changed_offsets": offsets[:32],
            }
        )
    return changes


def _diagnose_divergence(
    accepted: ReplayObservation,
    experimental: ReplayObservation,
) -> dict[str, Any]:
    return {
        "accepted": accepted.summary(),
        "experimental": experimental.summary(),
        "changed_registers": _changed_registers(accepted, experimental),
        "page_diffs": _page_diffs(accepted, experimental),
        "event_streams": {
            name: compare_event_streams(
                accepted.events.get(name, []), experimental.events.get(name, [])
            )
            for name in EVENT_STREAMS
        },
    }


def bisect_first_divergence(
    accepted_backend: ReplayBackend,
    experimental_backend: ReplayBackend,
    *,
    max_steps: int,
) -> tuple[int | None, ReplayObservation, ReplayObservation, int]:
    if max_steps < 1:
        raise ValueError("max_steps must be positive")
    comparisons = 0

    def compare(prefix: int) -> tuple[bool, ReplayObservation, ReplayObservation]:
        nonlocal comparisons
        left = accepted_backend.run_prefix(prefix)
        right = experimental_backend.run_prefix(prefix)
        comparisons += 1
        return left.architectural_hash() == right.architectural_hash(), left, right

    initial_matches, initial_left, initial_right = compare(0)
    if not initial_matches:
        return 0, initial_left, initial_right, comparisons
    final_matches, final_left, final_right = compare(max_steps)
    if final_matches:
        return None, final_left, final_right, comparisons
    low = 0
    high = max_steps
    while high - low > 1:
        middle = low + (high - low) // 2
        matches, _left, _right = compare(middle)
        if matches:
            low = middle
        else:
            high = middle
    _matches, divergent_left, divergent_right = compare(high)
    return high, divergent_left, divergent_right, comparisons


def run_differential_replay(
    capsule: ReplayCapsule,
    accepted_backend: ReplayBackend,
    experimental_backend: ReplayBackend,
    *,
    max_steps: int,
    failure_capsule: Path | None = None,
) -> dict[str, Any]:
    first_bad, accepted, experimental, comparisons = bisect_first_divergence(
        accepted_backend,
        experimental_backend,
        max_steps=max_steps,
    )
    report: dict[str, Any] = {
        "format": "b2-recomp-differential-replay",
        "version": 1,
        "capsule_id": capsule.capsule_id,
        "accepted_backend": accepted_backend.name,
        "experimental_backend": experimental_backend.name,
        "max_steps": max_steps,
        "matches": first_bad is None,
        "comparison_count": comparisons,
        "first_divergent_edge": first_bad,
        "last_matching_edge": (
            None if first_bad is None or first_bad == 0 else first_bad - 1
        ),
    }
    if first_bad is None:
        report["final"] = accepted.summary()
        return report
    report["divergence"] = _diagnose_divergence(accepted, experimental)
    report["guest_eip"] = experimental.state.eip
    report["guest_eip_hex"] = f"0x{experimental.state.eip:08X}"
    report["native_symbol"] = experimental.native_symbol
    if failure_capsule is not None:
        last_match_steps = max(0, first_bad - 1)
        last_matching = accepted_backend.run_prefix(last_match_steps)
        diagnostic_payload = _canonical_json(report)
        resources = dict(capsule.resources)
        resources["diagnostics/divergence.json"] = diagnostic_payload
        capture_replay_capsule(
            failure_capsule,
            state=last_matching.state,
            memory=last_matching.memory,
            functions=capsule.functions,
            scheduler_state=capsule.scheduler_state,
            service_state=capsule.service_state,
            provenance={
                **capsule.manifest.get("provenance", {}),
                "source_capsule_id": capsule.capsule_id,
                "last_matching_edge": last_match_steps,
                "first_divergent_edge": first_bad,
            },
            resources=resources,
            events=last_matching.events,
            capture_kind="divergence",
        )
        report["failure_capsule"] = str(failure_capsule.resolve())
    return report


def _load_event_document(path: Path) -> Mapping[str, Sequence[Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ReplayCapsuleError(f"event document is not an object: {path}")
    nested_events = value.get("events")
    if isinstance(nested_events, dict):
        value = nested_events
    for name, records in value.items():
        if not isinstance(name, str) or not isinstance(records, list):
            raise ReplayCapsuleError(f"event stream is invalid in {path}")
    return value


def _write_report(path: Path | None, report: Mapping[str, Any]) -> None:
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if path is None:
        print(payload, end="")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    print(path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    execute = commands.add_parser("execute", help="Bisect two capsule executors.")
    execute.add_argument("capsule", type=Path)
    execute.add_argument("--accepted", choices=("interpreter",), default="interpreter")
    execute.add_argument("--experimental", choices=("interpreter", "native"), default="native")
    execute.add_argument("--max-steps", type=int, required=True)
    execute.add_argument("--build-dir", type=Path, default=REPO_ROOT / "build" / "local" / "replay")
    execute.add_argument("--compiler-cache", choices=("auto", "off", "required"), default="auto")
    execute.add_argument("--failure-capsule", type=Path)
    execute.add_argument("--report", type=Path)
    compare = commands.add_parser(
        "compare-events", help="Compare service, typed draw, or audio boundaries."
    )
    compare.add_argument("accepted", type=Path)
    compare.add_argument("experimental", type=Path)
    compare.add_argument("--stream", choices=EVENT_STREAMS, required=True)
    compare.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "compare-events":
            left = _load_event_document(args.accepted)
            right = _load_event_document(args.experimental)
            report = compare_event_streams(left.get(args.stream, []), right.get(args.stream, []))
        else:
            capsule = load_replay_capsule(args.capsule)
            if args.failure_capsule is not None:
                require_local_capsule_output(
                    args.failure_capsule,
                    synthetic=capsule.manifest.get("capture_kind") == "synthetic",
                )
            accepted_backend: ReplayBackend = InterpreterReplayBackend(capsule)
            experimental_backend: ReplayBackend
            if args.experimental == "interpreter":
                experimental_backend = InterpreterReplayBackend(capsule)
            else:
                experimental_backend = NativeReplayBackend(
                    capsule,
                    build_dir=args.build_dir,
                    compiler_cache_mode=args.compiler_cache,
                )
            report = run_differential_replay(
                capsule,
                accepted_backend,
                experimental_backend,
                max_steps=args.max_steps,
                failure_capsule=args.failure_capsule,
            )
        _write_report(args.report, report)
        return 0 if report.get("matches", False) else 1
    except (OSError, ValueError, ReplayCapsuleError, json.JSONDecodeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    sys.exit(main())
