#!/usr/bin/env python3
"""Content-keyed validation graph execution shared by local development gates."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping, Sequence


VALIDATION_CACHE_SCHEMA = 3
VALIDATION_TIMING_SCHEMA = 1
TIMING_SAMPLE_LIMIT = 64
FAILURE_CAPSULE_LIMIT = 8
FAILURE_CAPSULE_TOTAL_BYTES = 64 * 1024 * 1024
FAILURE_ARTIFACT_BYTES = 8 * 1024 * 1024
FAILURE_OUTPUT_BYTES = 256 * 1024
REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ValidationNode:
    """One reproducible command in the validation dependency graph."""

    name: str
    command: tuple[str, ...]
    description: str
    inputs: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    environment_keys: tuple[str, ...] = ()
    package_identities: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    cacheable: bool = True
    exclusive: bool = False
    priority: int = 100
    budget_seconds: float | None = None
    coverage_key: str | None = None


@dataclass(frozen=True)
class CacheDecision:
    hit: bool
    reason: str
    key: str
    components: Mapping[str, str]


@dataclass(frozen=True)
class ValidationResult:
    name: str
    status: str
    returncode: int
    duration_seconds: float
    cache_key: str
    cache_reason: str
    output: str = ""


@dataclass(frozen=True)
class ValidationSummary:
    results: Mapping[str, ValidationResult]
    elapsed_seconds: float
    timing_path: Path | None = None
    failure_capsule: Path | None = None

    @property
    def passed(self) -> bool:
        return all(result.returncode == 0 for result in self.results.values())


@dataclass(frozen=True)
class TimingStatistics:
    runs: int
    p50_seconds: float
    p95_seconds: float


def _percentile(samples: Sequence[float], percentile: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    rank = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[rank]


def _timing_statistics(record: object) -> TimingStatistics | None:
    if not isinstance(record, dict):
        return None
    samples = record.get("samples_seconds")
    if not isinstance(samples, list):
        return None
    numeric = [float(value) for value in samples if isinstance(value, (int, float)) and value >= 0]
    if not numeric:
        return None
    return TimingStatistics(
        runs=len(numeric),
        p50_seconds=_percentile(numeric, 0.50),
        p95_seconds=_percentile(numeric, 0.95),
    )


def load_timing_history(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema": VALIDATION_TIMING_SCHEMA, "nodes": {}, "runs": []}
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != VALIDATION_TIMING_SCHEMA
        or not isinstance(payload.get("nodes"), dict)
        or not isinstance(payload.get("runs"), list)
    ):
        return {"schema": VALIDATION_TIMING_SCHEMA, "nodes": {}, "runs": []}
    return payload


def _save_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


@dataclass
class ValidationCache:
    path: Path
    entries: dict[str, dict[str, object]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> ValidationCache:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls(path)
        if not isinstance(payload, dict):
            return cls(path)
        if payload.get("schema") != VALIDATION_CACHE_SCHEMA:
            return cls(path)
        entries = payload.get("entries")
        if not isinstance(entries, dict):
            return cls(path)
        return cls(
            path,
            entries={str(key): value for key, value in entries.items() if isinstance(value, dict)},
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + f".{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(
                {"schema": VALIDATION_CACHE_SCHEMA, "entries": self.entries},
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def exact_entry(self, node: ValidationNode, key: str) -> dict[str, object] | None:
        return self.entries.get(f"{node.name}:{key}")

    def latest_entry(self, node: ValidationNode) -> dict[str, object] | None:
        candidates = [entry for entry in self.entries.values() if entry.get("node") == node.name]
        if not candidates:
            return None

        return max(candidates, key=_entry_completed_ns)

    def decide(
        self,
        node: ValidationNode,
        *,
        key: str,
        components: Mapping[str, str],
        root: Path,
        no_cache: bool,
    ) -> CacheDecision:
        if no_cache:
            return CacheDecision(False, "cache disabled", key, components)
        if not node.cacheable:
            return CacheDecision(False, "node is intentionally uncached", key, components)
        prior = self.exact_entry(node, key)
        if prior is None:
            latest = self.latest_entry(node)
            if latest is None:
                return CacheDecision(False, "no prior result", key, components)
            prior_components = latest.get("components")
            changed = _changed_components(
                prior_components if isinstance(prior_components, dict) else {}, components
            )
            return CacheDecision(False, _format_changed_components(changed), key, components)
        expected_outputs = prior.get("outputs")
        current_outputs = _output_identities(root, node.outputs)
        if any(name.startswith("missing:") for name in current_outputs):
            return CacheDecision(False, "required outputs are missing", key, components)
        if expected_outputs != current_outputs:
            return CacheDecision(False, "required outputs changed or are missing", key, components)
        return CacheDecision(True, "exact input and output key match", key, components)

    def record_success(
        self,
        node: ValidationNode,
        *,
        key: str,
        components: Mapping[str, str],
        duration_seconds: float,
        root: Path,
    ) -> None:
        if not node.cacheable:
            return
        self.entries[f"{node.name}:{key}"] = {
            "node": node.name,
            "key": key,
            "components": dict(components),
            "outputs": _output_identities(root, node.outputs),
            "duration_seconds": round(duration_seconds, 6),
            "completed_ns": time.time_ns(),
        }
        node_entries = sorted(
            (
                (entry_key, entry)
                for entry_key, entry in self.entries.items()
                if entry.get("node") == node.name
            ),
            key=lambda item: _entry_completed_ns(item[1]),
            reverse=True,
        )
        for entry_key, _entry in node_entries[8:]:
            self.entries.pop(entry_key, None)


def _entry_completed_ns(entry: Mapping[str, object]) -> int:
    completed = entry.get("completed_ns")
    return completed if isinstance(completed, int) else 0


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path.resolve())


def _expand_patterns(root: Path, patterns: Sequence[str]) -> list[Path]:
    paths: dict[str, Path] = {}
    for pattern in patterns:
        candidate = Path(pattern)
        if candidate.is_absolute():
            matches = [candidate] if candidate.is_file() else []
        elif any(character in pattern for character in "*?["):
            matches = [path for path in root.glob(pattern) if path.is_file()]
        else:
            resolved = root / candidate
            matches = [resolved] if resolved.is_file() else []
        for match in matches:
            paths[_normalized_relative(match, root)] = match
    return [paths[key] for key in sorted(paths)]


def _output_identities(root: Path, patterns: Sequence[str]) -> dict[str, str]:
    if not patterns:
        return {}
    identities: dict[str, str] = {}
    for path in _expand_patterns(root, patterns):
        identities[_normalized_relative(path, root)] = _file_digest(path)
    for pattern in patterns:
        if not _expand_patterns(root, (pattern,)):
            identities[f"missing:{pattern}"] = "missing"
    return identities


def node_key_components(
    node: ValidationNode,
    *,
    dependency_keys: Mapping[str, str],
    root: Path = REPO_ROOT,
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return every independently explainable component of a node cache key."""

    current_environment = os.environ if environment is None else environment
    components = {
        "contract:schema": str(VALIDATION_CACHE_SCHEMA),
        "contract:command": _sha256_bytes(
            json.dumps(node.command, separators=(",", ":")).encode("utf-8")
        ),
        "contract:budget-seconds": (
            "none" if node.budget_seconds is None else f"{node.budget_seconds:.6f}"
        ),
        "contract:coverage-key": node.coverage_key or "none",
        "identity:python-version": _sha256_bytes(sys.version.encode("utf-8")),
        "identity:python-executable": (
            _file_digest(Path(sys.executable))
            if Path(sys.executable).is_file()
            else _sha256_bytes(sys.executable.encode("utf-8"))
        ),
    }
    for key in sorted(node.environment_keys):
        components[f"environment:{key}"] = _sha256_bytes(
            current_environment.get(key, "<unset>").encode("utf-8")
        )
    for package in sorted(node.package_identities):
        try:
            version = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            version = "<missing>"
        components[f"package:{package}"] = _sha256_bytes(version.encode("utf-8"))
    for dependency in sorted(dependency_keys):
        components[f"dependency:{dependency}"] = dependency_keys[dependency]
    matched = _expand_patterns(root, node.inputs)
    for path in matched:
        components[f"input:{_normalized_relative(path, root)}"] = _file_digest(path)
    for pattern in sorted(node.inputs):
        if not _expand_patterns(root, (pattern,)):
            components[f"input-pattern:{pattern}"] = "no-matches"
    return components


def node_cache_key(components: Mapping[str, str]) -> str:
    return _sha256_bytes(
        json.dumps(components, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _changed_components(prior: Mapping[str, object], current: Mapping[str, str]) -> list[str]:
    changed = []
    for key in sorted(set(prior) | set(current)):
        if prior.get(key) != current.get(key):
            changed.append(key)
    return changed


def _format_changed_components(changed: Sequence[str]) -> str:
    if not changed:
        return "cache key changed"
    shown = list(changed[:4])
    suffix = f" and {len(changed) - len(shown)} more" if len(changed) > len(shown) else ""
    return "changed " + ", ".join(shown) + suffix


def _safe_name(value: str) -> str:
    normalized = "".join(character if character.isalnum() or character in "-_" else "-" for character in value)
    return normalized.strip("-")[:80] or "validation"


def _tree_size(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    try:
        files = path.rglob("*")
        for candidate in files:
            if candidate.is_file():
                try:
                    total += candidate.stat().st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


def _remove_owned_tree(path: Path, *, owner_root: Path) -> None:
    try:
        resolved = path.resolve()
        resolved.relative_to(owner_root.resolve())
    except (OSError, ValueError):
        return
    if resolved.is_dir():
        shutil.rmtree(resolved, ignore_errors=True)
    elif resolved.exists():
        try:
            resolved.unlink()
        except OSError:
            pass


def _copy_bounded_artifacts(source: Path, destination: Path) -> list[str]:
    omitted: list[str] = []
    remaining = FAILURE_ARTIFACT_BYTES
    if not source.is_dir():
        return omitted
    for candidate in sorted(path for path in source.rglob("*") if path.is_file()):
        try:
            size = candidate.stat().st_size
            relative = candidate.relative_to(source)
        except (OSError, ValueError):
            continue
        if size > remaining:
            omitted.append(relative.as_posix())
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(candidate, target)
        except OSError:
            omitted.append(relative.as_posix())
            continue
        remaining -= size
    return omitted


def _prune_failure_capsules(root: Path) -> None:
    try:
        capsules = sorted(
            (path for path in root.iterdir() if path.is_dir()),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
    except OSError:
        return
    total = 0
    for index, capsule in enumerate(capsules):
        size = _tree_size(capsule)
        total += size
        if index >= FAILURE_CAPSULE_LIMIT or total > FAILURE_CAPSULE_TOTAL_BYTES:
            _remove_owned_tree(capsule, owner_root=root)


def _first_structured_failure(output: str) -> str | None:
    prefix = "B2R_TEST_FAILURE="
    for line in output.splitlines():
        if line.startswith(prefix):
            return line.removeprefix(prefix).strip()
    return None


def _structured_failure_payload(output: str) -> tuple[str | None, dict[str, object] | None]:
    location = _first_structured_failure(output)
    if location is None:
        return None, None
    try:
        payload = json.loads(Path(location).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return location, None
    return location, payload if isinstance(payload, dict) else None


def _write_failure_capsule(
    node: ValidationNode,
    result: ValidationResult,
    *,
    components: Mapping[str, str],
    dependency_keys: Mapping[str, str],
    successful_order: Sequence[str],
    environment: Mapping[str, str],
    artifact_directory: Path,
    failure_root: Path,
) -> tuple[Path, str]:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    capsule = failure_root / f"{timestamp}-{_safe_name(node.name)}-{result.cache_key[:12]}"
    capsule.mkdir(parents=True, exist_ok=False)
    artifacts = capsule / "artifacts"
    omitted = _copy_bounded_artifacts(artifact_directory, artifacts)
    output = result.output
    if len(output.encode("utf-8", errors="replace")) > FAILURE_OUTPUT_BYTES:
        output = output[-FAILURE_OUTPUT_BYTES:]
        output = "[earlier output omitted by bounded failure capsule]\n" + output
    (capsule / "output.txt").write_text(output, encoding="utf-8")
    node_rerun = subprocess.list2cmdline(node.command)
    seed_environment = {
        key: value
        for key, value in sorted(environment.items())
        if "SEED" in key.upper() or key in {"TEMP", "TMP", "TMPDIR"}
    }
    structured_failure, structured_payload = _structured_failure_payload(result.output)
    focused_rerun = (
        structured_payload.get("rerun_command") if structured_payload is not None else None
    )
    rerun = focused_rerun if isinstance(focused_rerun, str) else node_rerun
    payload = {
        "schema": 1,
        "node": node.name,
        "description": node.description,
        "returncode": result.returncode,
        "duration_seconds": round(result.duration_seconds, 6),
        "budget_seconds": node.budget_seconds,
        "rerun_command": rerun,
        "node_rerun_command": node_rerun,
        "seed_environment": seed_environment,
        "temporary_artifact_location": str(artifacts),
        "source_artifact_location": str(artifact_directory),
        "structured_test_failure": structured_failure,
        "structured_test_failure_payload": structured_payload,
        "cache_key": result.cache_key,
        "cache_reason": result.cache_reason,
        "dependency_keys": dict(sorted(dependency_keys.items())),
        "input_hashes": {
            key: value
            for key, value in sorted(components.items())
            if key.startswith(("input:", "input-pattern:"))
        },
        "contract_hashes": {
            key: value
            for key, value in sorted(components.items())
            if key.startswith(("contract:", "identity:", "package:", "environment:"))
        },
        "last_successful_dependency": next(
            (name for name in reversed(successful_order) if name in node.dependencies),
            None,
        ),
        "omitted_artifacts": omitted,
        "created_utc": datetime.now(UTC).isoformat(),
    }
    _save_json_atomic(capsule / "failure.json", payload)
    (capsule / "rerun.ps1").write_text(
        "$ErrorActionPreference = 'Stop'\n" + rerun + "\n",
        encoding="utf-8",
    )
    _prune_failure_capsules(failure_root)
    return capsule, rerun


def _record_timing_history(
    path: Path,
    results: Mapping[str, ValidationResult],
    *,
    elapsed_seconds: float,
) -> None:
    payload = load_timing_history(path)
    nodes = payload["nodes"]
    runs = payload["runs"]
    assert isinstance(nodes, dict)
    assert isinstance(runs, list)
    for name, result in results.items():
        record = nodes.get(name)
        if not isinstance(record, dict):
            record = {"samples_seconds": [], "executions": 0, "cache_hits": 0}
        samples = record.get("samples_seconds")
        if not isinstance(samples, list):
            samples = []
        if result.status in {"passed", "failed"}:
            samples.append(round(result.duration_seconds, 6))
            samples = samples[-TIMING_SAMPLE_LIMIT:]
            record["executions"] = int(record.get("executions", 0)) + 1
        elif result.status == "cached":
            record["cache_hits"] = int(record.get("cache_hits", 0)) + 1
        statistics = _timing_statistics({"samples_seconds": samples})
        record.update(
            {
                "samples_seconds": samples,
                "p50_seconds": (
                    round(statistics.p50_seconds, 6) if statistics is not None else None
                ),
                "p95_seconds": (
                    round(statistics.p95_seconds, 6) if statistics is not None else None
                ),
                "last_status": result.status,
                "last_seconds": round(result.duration_seconds, 6),
            }
        )
        nodes[name] = record
    runs.append(
        {
            "completed_utc": datetime.now(UTC).isoformat(),
            "elapsed_seconds": round(elapsed_seconds, 6),
            "node_count": len(results),
            "cache_hits": sum(result.status == "cached" for result in results.values()),
            "passed": all(result.returncode == 0 for result in results.values()),
        }
    )
    payload["runs"] = runs[-TIMING_SAMPLE_LIMIT:]
    _save_json_atomic(path, payload)


def _run_subprocess(
    node: ValidationNode,
    *,
    root: Path,
    environment: Mapping[str, str],
    artifact_directory: Path,
    cache_key: str,
    cache_reason: str,
) -> ValidationResult:
    artifact_directory.mkdir(parents=True, exist_ok=True)
    temporary_directory = artifact_directory / "tmp"
    temporary_directory.mkdir(parents=True, exist_ok=True)
    node_environment = dict(environment)
    node_environment.update(
        {
            "B2R_VALIDATION_ARTIFACT_DIR": str(artifact_directory),
            "TEMP": str(temporary_directory),
            "TMP": str(temporary_directory),
            "TMPDIR": str(temporary_directory),
        }
    )
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            node.command,
            cwd=root,
            env=node_environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        return ValidationResult(
            name=node.name,
            status="failed",
            returncode=127,
            duration_seconds=time.perf_counter() - started,
            cache_key=cache_key,
            cache_reason=cache_reason,
            output=f"Unable to launch validation command: {exc}",
        )
    returncode = completed.returncode
    output = completed.stdout
    duration_seconds = time.perf_counter() - started
    if returncode == 0 and node.outputs:
        identities = _output_identities(root, node.outputs)
        missing = sorted(
            name.removeprefix("missing:") for name in identities if name.startswith("missing:")
        )
        if missing:
            returncode = 1
            output += "\nRequired validation outputs were not produced: " + ", ".join(missing)
    if (
        returncode == 0
        and node.budget_seconds is not None
        and duration_seconds > node.budget_seconds
    ):
        returncode = 124
        output += (
            f"\nRuntime budget exceeded: {duration_seconds:.3f}s > "
            f"{node.budget_seconds:.3f}s for {node.name}."
        )
    return ValidationResult(
        name=node.name,
        status="passed" if returncode == 0 else "failed",
        returncode=returncode,
        duration_seconds=duration_seconds,
        cache_key=cache_key,
        cache_reason=cache_reason,
        output=output,
    )


def _dependency_closure(
    nodes: Mapping[str, ValidationNode], selected: Sequence[str]
) -> dict[str, ValidationNode]:
    closure: dict[str, ValidationNode] = {}

    def add(name: str) -> None:
        if name in closure:
            return
        try:
            node = nodes[name]
        except KeyError as exc:
            raise ValueError(f"unknown validation node: {name}") from exc
        for dependency in node.dependencies:
            add(dependency)
        closure[name] = node

    for name in selected:
        add(name)
    return closure


def _dependency_result_key(
    node: ValidationNode,
    result: ValidationResult,
    *,
    root: Path,
) -> str:
    return _sha256_bytes(
        json.dumps(
            {
                "input_key": result.cache_key,
                "outputs": _output_identities(root, node.outputs),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _reject_redundant_nodes(graph: Mapping[str, ValidationNode]) -> None:
    coverage_keys: dict[str, str] = {}
    for name, node in graph.items():
        if node.coverage_key is None:
            continue
        prior = coverage_keys.get(node.coverage_key)
        if prior is not None:
            raise ValueError(
                f"redundant validation nodes {prior!r} and {name!r} claim coverage key "
                f"{node.coverage_key!r}"
            )
        coverage_keys[node.coverage_key] = name


def run_validation_graph(
    nodes: Mapping[str, ValidationNode],
    selected: Sequence[str],
    *,
    cache_path: Path,
    root: Path = REPO_ROOT,
    jobs: int = 4,
    no_cache: bool = False,
    environment: Mapping[str, str] | None = None,
    timing_path: Path | None = None,
    failure_root: Path | None = None,
    cancellation_path: Path | None = None,
) -> ValidationSummary:
    """Run selected nodes and dependencies, buffering subprocess output until failure."""

    graph = _dependency_closure(nodes, selected)
    _reject_redundant_nodes(graph)
    current_environment = dict(os.environ if environment is None else environment)
    current_environment.setdefault("PYTHONHASHSEED", "0")
    resolved_timing_path = (
        timing_path
        if timing_path is not None
        else cache_path.with_name("validation-timings.json")
    )
    resolved_failure_root = (
        failure_root
        if failure_root is not None
        else root / "reports" / "local" / "validation" / "failures"
    )
    pending_artifact_root = (
        resolved_failure_root.parent / "pending" / f"run-{os.getpid()}-{time.time_ns()}"
    )
    prior_timing_history = load_timing_history(resolved_timing_path)
    cache = ValidationCache(cache_path) if no_cache else ValidationCache.load(cache_path)
    pending = set(graph)
    running: dict[Future[ValidationResult], tuple[ValidationNode, CacheDecision]] = {}
    results: dict[str, ValidationResult] = {}
    started = time.perf_counter()
    failed = False
    cancelled = False
    first_failure_capsule: Path | None = None
    first_failure_rerun: str | None = None
    successful_order: list[str] = []
    node_context: dict[str, tuple[Mapping[str, str], Mapping[str, str], Path]] = {}

    def ready_nodes() -> list[ValidationNode]:
        ready = [
            graph[name]
            for name in pending
            if all(dependency in results for dependency in graph[name].dependencies)
            and all(results[dependency].returncode == 0 for dependency in graph[name].dependencies)
        ]
        return sorted(ready, key=lambda node: (node.priority, node.name))

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as executor:
        while pending or running:
            made_progress = False
            if cancellation_path is not None and cancellation_path.exists():
                cancelled = True
                failed = True
            if not failed:
                ready = ready_nodes()
                exclusive_running = any(node.exclusive for node, _decision in running.values())
                for node in ready:
                    if node.exclusive and running:
                        continue
                    if not node.exclusive and exclusive_running:
                        continue
                    if len(running) >= max(1, jobs):
                        break
                    dependency_keys = {
                        dependency: _dependency_result_key(
                            graph[dependency],
                            results[dependency],
                            root=root,
                        )
                        for dependency in node.dependencies
                    }
                    components = node_key_components(
                        node,
                        dependency_keys=dependency_keys,
                        root=root,
                        environment=current_environment,
                    )
                    key = node_cache_key(components)
                    decision = cache.decide(
                        node,
                        key=key,
                        components=components,
                        root=root,
                        no_cache=no_cache,
                    )
                    artifact_directory = pending_artifact_root / _safe_name(node.name)
                    node_context[node.name] = (
                        decision.components,
                        dependency_keys,
                        artifact_directory,
                    )
                    pending.remove(node.name)
                    made_progress = True
                    if decision.hit:
                        cached_duration = 0.0
                        prior = cache.exact_entry(node, key) or {}
                        prior_duration = prior.get("duration_seconds")
                        if isinstance(prior_duration, (int, float)):
                            cached_duration = float(prior_duration)
                        results[node.name] = ValidationResult(
                            name=node.name,
                            status="cached",
                            returncode=0,
                            duration_seconds=cached_duration,
                            cache_key=key,
                            cache_reason=decision.reason,
                        )
                        successful_order.append(node.name)
                        print(f"[cached] {node.name}: {decision.reason}", flush=True)
                    else:
                        print(
                            f"[run] {node.name}: {decision.reason}\n"
                            f"      {subprocess.list2cmdline(node.command)}",
                            flush=True,
                        )
                        future = executor.submit(
                            _run_subprocess,
                            node,
                            root=root,
                            environment=current_environment,
                            artifact_directory=artifact_directory,
                            cache_key=key,
                            cache_reason=decision.reason,
                        )
                        running[future] = (node, decision)
                        if node.exclusive:
                            break
            if running:
                completed_futures, _ = wait(tuple(running), return_when=FIRST_COMPLETED)
                for future in completed_futures:
                    node, decision = running.pop(future)
                    result = future.result()
                    results[node.name] = result
                    prior_nodes = prior_timing_history.get("nodes")
                    prior_statistics = (
                        _timing_statistics(prior_nodes.get(node.name))
                        if isinstance(prior_nodes, dict)
                        else None
                    )
                    if (
                        prior_statistics is not None
                        and prior_statistics.runs >= 5
                        and result.duration_seconds
                        > max(
                            prior_statistics.p95_seconds * 1.5,
                            prior_statistics.p95_seconds + 0.25,
                        )
                    ):
                        print(
                            f"[timing-regression] {node.name}: {result.duration_seconds:.2f}s "
                            f"versus historical p95 {prior_statistics.p95_seconds:.2f}s",
                            flush=True,
                        )
                    if result.returncode == 0:
                        successful_order.append(node.name)
                        if not no_cache:
                            cache.record_success(
                                node,
                                key=result.cache_key,
                                components=decision.components,
                                duration_seconds=result.duration_seconds,
                                root=root,
                            )
                            cache.save()
                        print(
                            f"[passed] {node.name} ({result.duration_seconds:.2f}s)",
                            flush=True,
                        )
                        _remove_owned_tree(
                            node_context[node.name][2],
                            owner_root=pending_artifact_root,
                        )
                    else:
                        failed = True
                        if first_failure_capsule is None:
                            (
                                failure_components,
                                failure_dependency_keys,
                                artifact_directory,
                            ) = node_context[node.name]
                            capsule_environment = dict(current_environment)
                            capsule_environment.update(
                                {
                                    "TEMP": str(artifact_directory / "tmp"),
                                    "TMP": str(artifact_directory / "tmp"),
                                    "TMPDIR": str(artifact_directory / "tmp"),
                                }
                            )
                            first_failure_capsule, first_failure_rerun = _write_failure_capsule(
                                node,
                                result,
                                components=failure_components,
                                dependency_keys=failure_dependency_keys,
                                successful_order=successful_order,
                                environment=capsule_environment,
                                artifact_directory=artifact_directory,
                                failure_root=resolved_failure_root,
                            )
                        print(
                            f"[failed] {node.name} ({result.duration_seconds:.2f}s)\n"
                            f"{result.output.rstrip()}\n"
                            f"Rerun failure: {first_failure_rerun}\n"
                            f"Rerun node: {subprocess.list2cmdline(node.command)}\n"
                            f"Seed: PYTHONHASHSEED={current_environment['PYTHONHASHSEED']}\n"
                            f"Input/cache key: {result.cache_key}\n"
                            f"Last successful dependency: "
                            f"{next((name for name in reversed(successful_order) if name in node.dependencies), '<none>')}\n"
                            f"Failure capsule: {first_failure_capsule}\n"
                            f"Temporary artifacts: {first_failure_capsule / 'artifacts'}",
                            flush=True,
                        )
            elif pending and not made_progress:
                break

    for name in sorted(pending):
        results[name] = ValidationResult(
            name=name,
            status="cancelled" if cancelled else "blocked",
            returncode=1,
            duration_seconds=0.0,
            cache_key="",
            cache_reason="superseded by a newer closeout" if cancelled else "dependency failed",
        )
    if not no_cache:
        cache.save()
    elapsed = time.perf_counter() - started
    _record_timing_history(resolved_timing_path, results, elapsed_seconds=elapsed)
    slowest = sorted(
        (result for result in results.values() if result.status == "passed"),
        key=lambda result: result.duration_seconds,
        reverse=True,
    )[:5]
    if slowest:
        print("Slowest executed nodes:", flush=True)
        for result in slowest:
            history = load_timing_history(resolved_timing_path)
            history_nodes = history.get("nodes")
            statistics = (
                _timing_statistics(history_nodes.get(result.name))
                if isinstance(history_nodes, dict)
                else None
            )
            percentile_text = (
                f", p50 {statistics.p50_seconds:.2f}s, p95 {statistics.p95_seconds:.2f}s"
                if statistics is not None
                else ""
            )
            print(
                f"  {result.name}: {result.duration_seconds:.2f}s{percentile_text}",
                flush=True,
            )
    _remove_owned_tree(pending_artifact_root, owner_root=resolved_failure_root.parent / "pending")
    return ValidationSummary(
        results=results,
        elapsed_seconds=elapsed,
        timing_path=resolved_timing_path,
        failure_capsule=first_failure_capsule,
    )
