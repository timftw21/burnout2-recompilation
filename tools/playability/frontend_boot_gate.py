"""Assess frontend boot progress across two playability-probe checkpoints."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


FORMAT = "b2-recomp-frontend-boot-gate"
LOADING_TEXT = "Loading - please wait"
ACTIVE_THREAD_STATUSES = {"render_boundary", "render_watchpoint_stop"}


def _execution(summary: dict[str, Any]) -> dict[str, Any]:
    execution = summary.get("entry_recovery", {}).get("execution", {})
    return execution if isinstance(execution, dict) else {}


def _primary_thread(execution: dict[str, Any]) -> dict[str, Any]:
    threads = execution.get("guest_thread_executions", [])
    if not isinstance(threads, list) or not threads:
        return {}
    candidates = [thread for thread in threads if isinstance(thread, dict)]
    if not candidates:
        return {}
    return max(
        candidates,
        key=lambda thread: int(
            thread.get("render_command_stream", {}).get("write_count", 0)
            if isinstance(thread.get("render_command_stream"), dict)
            else 0
        ),
    )


def _loading_text(execution: dict[str, Any]) -> str | None:
    text_draw = execution.get("title_text_draw_fast_path", {})
    if not isinstance(text_draw, dict):
        return None
    for sample in text_draw.get("sampled_strings", []):
        if not isinstance(sample, dict):
            continue
        bytes_hex = sample.get("bytes_hex")
        if not isinstance(bytes_hex, str):
            continue
        try:
            text = bytes.fromhex(bytes_hex).decode("cp1252").rstrip("\0")
        except (ValueError, UnicodeDecodeError):
            continue
        if text:
            return text
    return None


def checkpoint_metrics(summary: dict[str, Any]) -> dict[str, Any]:
    execution = _execution(summary)
    thread = _primary_thread(execution)
    render_stream = thread.get("render_command_stream", {})
    text_draw = execution.get("title_text_draw_fast_path", {})
    frontend_object = execution.get("title_frontend_object_fast_path", {})
    record_repair = thread.get("title_frontend_record_table_count_repair", {})
    return {
        "execution_status": execution.get("status"),
        "thread_status": thread.get("status"),
        "dynamic_block_count": int(execution.get("dynamic_block_count", 0) or 0),
        "dynamic_frontier_count": int(
            execution.get("dynamic_frontier_count", 0) or 0
        ),
        "loading_text": _loading_text(execution),
        "text_draw_count": int(
            text_draw.get("invocation_count", 0)
            if isinstance(text_draw, dict)
            else 0
        ),
        "frontend_method_count": int(
            frontend_object.get("method_invocation_count", 0)
            if isinstance(frontend_object, dict)
            else 0
        ),
        "runtime_abi_invocation_count": int(
            thread.get("runtime_abi_invocation_count", 0) or 0
        ),
        "render_write_count": int(
            render_stream.get("write_count", 0)
            if isinstance(render_stream, dict)
            else 0
        ),
        "record_table_observation_count": int(
            record_repair.get("observation_count", 0)
            if isinstance(record_repair, dict)
            else 0
        ),
        "record_table_repair_count": int(
            record_repair.get("repair_count", 0)
            if isinstance(record_repair, dict)
            else 0
        ),
    }


def assess_frontend_boot(
    early_summary: dict[str, Any], late_summary: dict[str, Any]
) -> dict[str, Any]:
    early = checkpoint_metrics(early_summary)
    late = checkpoint_metrics(late_summary)
    checks = {
        "no_dynamic_frontiers": (
            early["dynamic_frontier_count"] == 0
            and late["dynamic_frontier_count"] == 0
        ),
        "loading_draw_path_identified": (
            early["loading_text"] == LOADING_TEXT
            and late["loading_text"] == LOADING_TEXT
            and early["text_draw_count"] > 0
        ),
        "loading_draw_path_quiesced": (
            late["text_draw_count"] == early["text_draw_count"]
        ),
        "frontend_update_loop_progressed": (
            late["frontend_method_count"] > early["frontend_method_count"]
        ),
        "render_submission_progressed": (
            late["render_write_count"] > early["render_write_count"]
        ),
        "runtime_services_progressed": (
            late["runtime_abi_invocation_count"]
            > early["runtime_abi_invocation_count"]
        ),
        "record_scan_progressed_without_new_repairs": (
            late["record_table_observation_count"]
            > early["record_table_observation_count"]
            and late["record_table_repair_count"]
            == early["record_table_repair_count"]
        ),
        "late_checkpoint_is_active_rendering": (
            late["thread_status"] in ACTIVE_THREAD_STATUSES
        ),
    }
    passed = all(checks.values())
    return {
        "format": FORMAT,
        "status": "pass" if passed else "fail",
        "classification": (
            "title_menu_frontend_loop_reached"
            if passed
            else "frontend_boot_not_proven"
        ),
        "checks": checks,
        "early": early,
        "late": late,
        "note": (
            "This gate proves guest control-flow progress beyond the loading overlay; "
            "native geometry and texture translation are assessed separately."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare two probe summaries and gate frontend boot progress."
    )
    parser.add_argument("--early", type=Path, required=True)
    parser.add_argument("--late", type=Path, required=True)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    assessment = assess_frontend_boot(
        json.loads(args.early.read_text(encoding="utf-8")),
        json.loads(args.late.read_text(encoding="utf-8")),
    )
    output = json.dumps(
        assessment, indent=2 if args.pretty else None, sort_keys=True
    )
    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(output + "\n", encoding="utf-8", newline="\n")
    print(output)
    return 0 if assessment["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
