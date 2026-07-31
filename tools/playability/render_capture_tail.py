"""Create a bootstrap-backed tail capture for fast render-state analysis."""

from __future__ import annotations

import argparse
import json
import shutil
import struct
from pathlib import Path


SPAN_MAGIC = b"B2SPAN01"
SPAN_HEADER = struct.Struct("<BBHIII")


def trim_render_capture(
    manifest_path: Path,
    output_directory: Path,
    tail_bytes: int,
) -> Path:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    command_path = Path(manifest["command_snapshot_path"])
    resource_path = Path(manifest["resource_snapshot_path"])
    bootstrap_path = Path(manifest["interpreter_bootstrap_path"])
    if not command_path.is_absolute():
        command_path = manifest_path.parent / command_path
    if not resource_path.is_absolute():
        resource_path = manifest_path.parent / resource_path
    if not bootstrap_path.is_absolute():
        bootstrap_path = manifest_path.parent / bootstrap_path

    spans: list[tuple[int, int, int]] = []
    with command_path.open("rb") as source:
        if source.read(len(SPAN_MAGIC)) != SPAN_MAGIC:
            raise ValueError("render capture does not use bulk spans")
        cursor = len(SPAN_MAGIC)
        while header := source.read(SPAN_HEADER.size):
            if len(header) != SPAN_HEADER.size:
                raise ValueError("truncated render command span header")
            kind, flags, _reserved, _address, payload_size, logical_count = (
                SPAN_HEADER.unpack(header)
            )
            if kind > 1 or flags & ~1 or not payload_size or not logical_count:
                raise ValueError("invalid render command span header")
            span_size = SPAN_HEADER.size + payload_size
            spans.append((cursor, span_size, logical_count))
            source.seek(payload_size, 1)
            cursor += span_size
    if not spans:
        raise ValueError("render command capture contains no spans")

    first_span = len(spans) - 1
    retained_bytes = spans[first_span][1]
    while (
        first_span > 0
        and retained_bytes + spans[first_span - 1][1] <= tail_bytes
    ):
        first_span -= 1
        retained_bytes += spans[first_span][1]
    retained_records = sum(span[2] for span in spans[first_span:])

    output_directory.mkdir(parents=True, exist_ok=True)
    command_output = output_directory / "commands.bin"
    with command_path.open("rb") as source, command_output.open("wb") as output:
        output.write(SPAN_MAGIC)
        source.seek(spans[first_span][0])
        shutil.copyfileobj(source, output)
    resource_output = output_directory / "resources.bin"
    bootstrap_output = output_directory / "interpreter-bootstrap.bin"
    shutil.copyfile(resource_path, resource_output)
    shutil.copyfile(bootstrap_path, bootstrap_output)

    manifest.update(
        {
            "write_count": retained_records,
            "published_command_record_count": retained_records,
            "presentable_command_record_count": retained_records,
            "command_snapshot_base_record_count": 0,
            "command_snapshot_record_count": retained_records,
            "command_snapshot_source_record_count": retained_records,
            "command_snapshot_trimmed_record_count": sum(
                span[2] for span in spans[:first_span]
            ),
            "command_snapshot_exact_prefix": False,
            "published_command_byte_count": retained_bytes,
            "presentable_command_byte_count": retained_bytes,
            "command_snapshot_base_byte_count": 0,
            "command_snapshot_byte_count": retained_bytes,
            "command_snapshot_path": str(command_output.resolve()),
            "resource_snapshot_path": str(resource_output.resolve()),
            "interpreter_bootstrap_path": str(bootstrap_output.resolve()),
        }
    )
    manifest_output = output_directory / "render.json"
    manifest_output.write_text(
        json.dumps(manifest, separators=(",", ":")),
        encoding="utf-8",
    )
    return manifest_output


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Retain complete command spans from the end of an F12 capture "
            "and pair them with its interpreter bootstrap."
        )
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--tail-bytes", type=int, default=4 * 1024 * 1024)
    args = parser.parse_args()
    if args.tail_bytes <= 0:
        parser.error("--tail-bytes must be positive")
    print(trim_render_capture(
        args.manifest,
        args.output_directory,
        args.tail_bytes,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
