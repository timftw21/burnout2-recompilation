from __future__ import annotations

import json
import struct
import tempfile
import unittest
from pathlib import Path

from tools.playability.preflight_validation import (
    ReplayStage,
    analyze_frame_sequence,
    analyze_probe_health,
    analyze_render_capabilities,
    inspect_frame_health,
    load_render_stream,
    run_replay_suite,
    scan_dic_texture_assets,
)
from tools.render.d3d8_stream import STREAM_FORMAT


def _write_bmp(path: Path, colors: list[tuple[int, int, int, int]]) -> None:
    width = len(colors)
    pixels = b"".join(bytes((blue, green, red, alpha)) for red, green, blue, alpha in colors)
    path.write_bytes(
        struct.pack("<2sIHHI", b"BM", 54 + len(pixels), 0, 0, 54)
        + struct.pack("<IiiHHIIiiII", 40, width, -1, 1, 32, 0, len(pixels), 2835, 2835, 0, 0)
        + pixels
    )


def _render_stream(
    *,
    primitive: int = 6,
    resource_format: str = "DXT1",
    resource_width: int = 4,
    resource_height: int = 4,
) -> dict[str, object]:
    address = 0x22001000
    draw_format = (2 << 24) | (2 << 20) | (1 << 16) | (0x0C << 8) | (2 << 4)
    words = (
        (4 << 18) | 0x1B00,
        address,
        draw_format,
        0,
        1 << 30,
        (1 << 18) | 0x17FC,
        primitive,
        (1 << 18) | 0x17FC,
        0,
    )
    payload = struct.pack(f"<{len(words)}I", *words)
    resource_bytes = 8 if resource_format == "DXT1" else 16
    return {
        "format": STREAM_FORMAT,
        "write_count": 1,
        "captured_write_count": 1,
        "resource_snapshots": [
            {
                "address": address,
                "format": resource_format,
                "width": resource_width,
                "height": resource_height,
                "bytes_hex": bytes(resource_bytes).hex().upper(),
            }
        ],
        "writes": [
            {
                "kind": "d3d_push_buffer",
                "address": 0x80000000,
                "size": len(payload),
                "bytes_hex": payload.hex().upper(),
            }
        ],
    }


class PreflightValidationTests(unittest.TestCase):
    def test_asset_preflight_inventories_dictionary_texture_formats(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dictionary = root / "frontend" / "global.dic"
            dictionary.parent.mkdir(parents=True)
            header = bytearray(20)
            header[:4] = b"\x00\x05\x00\x00"
            struct.pack_into("<HH", header, 8, 4, 4)
            header[15] = 0x0C
            struct.pack_into("<I", header, 16, 8)
            dictionary.write_bytes(b"test_texture\0".ljust(64, b"\0") + header + bytes(8))

            report = scan_dic_texture_assets(root)

        self.assertTrue(report["passed"])
        self.assertEqual(report["texture_count"], 1)
        self.assertEqual(report["format_counts"], {"DXT1": 1})
        self.assertEqual(report["textures"][0]["name"], "test_texture")

    def test_render_capability_gate_accepts_matching_supported_draw(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "render.json"
            path.write_text(json.dumps(_render_stream()), encoding="utf-8")

            report = analyze_render_capabilities(path)

        self.assertTrue(report["passed"])
        self.assertEqual(report["textured_draw_count"], 1)
        self.assertEqual(report["matched_textured_draw_count"], 1)

    def test_audit_manifest_replay_honors_exact_command_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            commands = root / "commands.bin"
            record = struct.pack("<BBHI8s", 0, 4, 0, 0xFED00000, bytes(8))
            commands.write_bytes(b"B2APPND1" + record + record)
            resources = root / "resources.json"
            resources.write_text('{"resource_snapshots":[]}', encoding="utf-8")
            manifest = root / "render.json"
            manifest.write_text(
                json.dumps(
                    {
                        "format": "b2-recomp-live-render-manifest",
                        "write_count": 2,
                        "audit_command_record_count": 1,
                        "command_snapshot_path": str(commands),
                        "resource_snapshot_path": str(resources),
                    }
                ),
                encoding="utf-8",
            )

            stream = load_render_stream(manifest)

        self.assertEqual(stream["write_count"], 1)
        self.assertEqual(stream["captured_write_count"], 1)

    def test_render_capability_gate_rejects_metadata_and_primitive_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "render.json"
            path.write_text(
                json.dumps(
                    _render_stream(
                        primitive=5,
                        resource_format="DXT5",
                        resource_width=8,
                    )
                ),
                encoding="utf-8",
            )

            report = analyze_render_capabilities(path)

        self.assertFalse(report["passed"])
        self.assertEqual(report["unsupported_primitives"], ["triangle_list"])
        self.assertEqual(report["draw_resource_consistency_failure_count"], 1)
        reasons = report["draw_resource_consistency_failures"][0]["reasons"]
        self.assertIn("format_mismatch", reasons)
        self.assertIn("width_mismatch", reasons)

    def test_frame_health_rejects_whiteout_and_accepts_detailed_frame(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            white = root / "white.bmp"
            detailed = root / "detailed.bmp"
            _write_bmp(white, [(255, 255, 255, 255)] * 16)
            _write_bmp(
                detailed,
                [(index * 15, 255 - index * 10, index * 7, 255) for index in range(16)],
            )

            white_report = inspect_frame_health(white)
            detailed_report = inspect_frame_health(detailed)

        self.assertFalse(white_report["passed"])
        self.assertIn("whiteout_frame", white_report["failure_reasons"])
        self.assertTrue(detailed_report["passed"])
        self.assertEqual(detailed_report["unique_colors"], 16)

    def test_frame_sequence_can_require_pixel_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            frame = Path(temp_dir) / "frame.bmp"
            _write_bmp(
                frame,
                [(index * 15, 255 - index * 10, index * 7, 255) for index in range(16)],
            )
            health = inspect_frame_health(frame)

        report = analyze_frame_sequence([health, health], require_change=True)

        self.assertFalse(report["passed"])
        self.assertEqual(report["distinct_pixel_frames"], 1)
        self.assertIn("frame_sequence_did_not_change", report["failure_reasons"])

    def test_probe_health_requires_progress_frontier_clear_and_live_music(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "probe.json"
            path.write_text(
                json.dumps(
                    {
                        "entry_recovery": {
                            "execution": {
                                "dynamic_frontier_count": 0,
                                "title_music_mode_fast_path": {
                                    "decode_error_count": 0,
                                    "invocation_count": 4,
                                    "submitted_track_count": 1,
                                    "recent_invocations": [{"mode": 2}],
                                },
                                "guest_thread_executions": [
                                    {"steps": 1000, "live_host_bridge": {"render_publish_count": 2}}
                                ],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            report = analyze_probe_health(path)

        self.assertTrue(report["passed"])
        self.assertEqual(report["music_mode"], 2)
        self.assertTrue(report["menu_music_loop_contract"])

    def test_replay_suite_applies_strict_host_and_frame_thresholds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            stream = root / "title.json"
            stream.write_text(json.dumps(_render_stream()), encoding="utf-8")

            def fake_runner(**kwargs: object) -> dict[str, object]:
                screenshot = Path(kwargs["screenshot"])
                colors = [(index * 15, 255 - index * 10, index * 7, 255) for index in range(16)]
                _write_bmp(screenshot, colors)
                return {
                    "returncode": 0,
                    "stderr": "",
                    "debug_json": str(kwargs["debug_json"]),
                    "screenshot": str(screenshot),
                    "events": [
                        {"event": "main_loop_enter"},
                        {
                            "event": "nv2a_native_resources_created",
                            "vertices": 4,
                            "draws": 1,
                            "presented_draws": 1,
                            "guest_flips": 1,
                            "textures": 1,
                        },
                        {
                            "event": "render_validation",
                            "passed": True,
                            "unsupported_texture_resource_count": 0,
                            "unmatched_presented_texture_draw_count": 0,
                        },
                        {
                            "event": "frame_readback_captured",
                            "unique_colors": 16,
                            "dominant_rgba": 0,
                            "dominant_count": 1,
                        },
                        {"event": "frame_presented"},
                        {"event": "main_loop_exit"},
                    ],
                }

            report = run_replay_suite(
                [ReplayStage("title", stream, min_native_textures=1)],
                output_dir=root / "output",
                replay_runner=fake_runner,
            )

        self.assertTrue(report["passed"])
        self.assertEqual(report["passed_stage_count"], 1)


if __name__ == "__main__":
    unittest.main()
