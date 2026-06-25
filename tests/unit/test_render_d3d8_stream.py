from __future__ import annotations

import unittest

from tools.render.d3d8_stream import (
    decode_render_stream,
    extract_render_streams_from_probe_summary,
    normalize_render_stream,
    replay_render_stream,
)


class RenderD3D8StreamTests(unittest.TestCase):
    def test_extracts_first_class_stream_from_probe_summary(self) -> None:
        summary = {
            "format": "b2-recomp-playability-probe",
            "entry_recovery": {
                "execution": {
                    "guest_thread_executions": [
                        {
                            "status": "scheduler_boundary",
                            "start_address_hex": "0x000E682C",
                            "render_command_stream": {
                                "write_count": 2,
                                "mmio_write_count": 1,
                                "push_buffer_write_count": 1,
                                "writes": [
                                    {
                                        "sequence": 1,
                                        "kind": "d3d_mmio",
                                        "address": 0xFED00048,
                                        "value": 0x1200,
                                    },
                                    {
                                        "sequence": 2,
                                        "kind": "d3d_push_buffer",
                                        "address": 0x80000080,
                                        "value": 0xFF,
                                        "size": 1,
                                    },
                                ],
                            },
                        }
                    ]
                }
            },
        }

        streams = extract_render_streams_from_probe_summary(summary)

        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0]["format"], "b2-recomp-render-command-stream")
        self.assertFalse(streams[0]["public_safe"])
        self.assertEqual(streams[0]["source"]["thread_status"], "scheduler_boundary")
        self.assertEqual(streams[0]["captured_write_count"], 2)

    def test_decodes_known_mmio_and_interprets_push_buffer_method_packet(self) -> None:
        stream = normalize_render_stream(
            {
                "format": "b2-recomp-render-command-stream",
                "write_count": 9,
                "mmio_write_count": 1,
                "push_buffer_write_count": 8,
                "writes": [
                    {
                        "kind": "d3d_mmio",
                        "address": 0xFED00048,
                        "value": 0x1200,
                    },
                    {
                        "kind": "d3d_push_buffer",
                        "address": 0x80000080,
                        "value": 0x10,
                        "size": 1,
                    },
                    {
                        "kind": "d3d_push_buffer",
                        "address": 0x80000081,
                        "value": 0x18,
                        "size": 1,
                    },
                    {
                        "kind": "d3d_push_buffer",
                        "address": 0x80000082,
                        "value": 0x04,
                        "size": 1,
                    },
                    {
                        "kind": "d3d_push_buffer",
                        "address": 0x80000083,
                        "value": 0x00,
                        "size": 1,
                    },
                    {
                        "kind": "d3d_push_buffer",
                        "address": 0x80000084,
                        "value": 0xFF,
                        "size": 1,
                    },
                    {
                        "kind": "d3d_push_buffer",
                        "address": 0x80000085,
                        "value": 0x20,
                        "size": 1,
                    },
                    {
                        "kind": "d3d_push_buffer",
                        "address": 0x80000086,
                        "value": 0x40,
                        "size": 1,
                    },
                    {
                        "kind": "d3d_push_buffer",
                        "address": 0x80000087,
                        "value": 0x80,
                        "size": 1,
                    },
                ],
            }
        )

        decoded = decode_render_stream(stream)

        self.assertEqual(decoded["format"], "b2-recomp-render-command-decode")
        self.assertEqual(decoded["decoded_command_count"], 3)
        self.assertEqual(decoded["commands"][0]["method"], "push_buffer_pitch_or_limit")
        self.assertEqual(decoded["commands"][1]["method"], "push_buffer_contiguous_bytes")
        self.assertEqual(decoded["commands"][1]["bytes_hex"], "10180400FF204080")
        self.assertEqual(decoded["commands"][2]["method"], "nv2a_increasing_methods")
        self.assertEqual(decoded["commands"][2]["first_method_hex"], "0x00001810")
        self.assertEqual(decoded["commands"][2]["methods"][0]["name"], "clear_color")
        self.assertEqual(decoded["commands"][2]["methods"][0]["data_hex"], "0x804020FF")
        self.assertEqual(decoded["frame_state"]["clear_color"]["r8"], 0x40)
        self.assertEqual(decoded["frame_state"]["clear_color"]["g8"], 0x20)
        self.assertEqual(decoded["frame_state"]["clear_color"]["b8"], 0xFF)
        self.assertEqual(decoded["state_update_count"], 1)

    def test_zero_count_method_words_do_not_count_as_method_packets(self) -> None:
        stream = {
            "format": "b2-recomp-render-command-stream",
            "write_count": 1,
            "writes": [
                {"kind": "d3d_push_buffer", "address": 0x80000080, "value": 0x00000200},
            ],
        }

        decoded = decode_render_stream(stream)
        replay = replay_render_stream(stream)

        self.assertEqual(decoded["push_buffer_method_packet_count"], 0)
        self.assertEqual(decoded["zero_count_method_word_count"], 1)
        self.assertEqual(decoded["commands"][1]["category"], "push-buffer-zero-count-word")
        self.assertEqual(replay["push_buffer_method_packet_count"], 0)
        self.assertEqual(replay["zero_count_method_word_count"], 1)

    def test_replays_stream_to_deterministic_interpreted_render_work(self) -> None:
        stream = {
            "format": "b2-recomp-render-command-stream",
            "write_count": 4,
            "writes": [
                {"kind": "d3d_mmio", "address": 0xFED00008, "value": 1},
                {"kind": "d3d_push_buffer", "address": 0x80000080, "value": 0x00041810},
                {"kind": "d3d_push_buffer", "address": 0x80000084, "value": 0x804020FF},
                {"kind": "d3d_mmio", "address": 0xFED00048, "value": 0x1200},
            ],
        }

        replay = replay_render_stream(stream, width=320, height=240)

        self.assertEqual(replay["format"], "b2-recomp-render-command-replay")
        self.assertEqual(replay["renderer_backend"], "vulkan")
        self.assertEqual(
            replay["translation_semantics"],
            "d3d8-nv2a-method-push-buffer-interpretation",
        )
        self.assertEqual(replay["push_buffer_method_packet_count"], 1)
        self.assertEqual(replay["interpreted_method_count"], 1)
        self.assertTrue(replay["frame_state"]["seed_sha256"])
        method_command = next(
            command
            for command in replay["commands"]
            if command["translated_kind"] == "nv2a_method_packet"
        )
        self.assertEqual(method_command["methods"][0]["name"], "clear_color")
        self.assertNotIn("rect", method_command)
        self.assertEqual(replay["frame_state"]["clear_color"]["raw_hex"], "0x804020FF")
        self.assertEqual(
            replay["frame_state"]["diagnostic_clear_source"],
            "d3d8_clear_color_method",
        )
        self.assertEqual(
            replay["frame_state"]["diagnostic_clear_color"],
            replay["frame_state"]["clear_color"]["normalized"],
        )
        self.assertGreaterEqual(replay["vulkan_clear_work_count"], 1)
        self.assertEqual(
            replay["vulkan_work"][0]["translated_kind"],
            "vulkan_render_pass_begin_clear",
        )

    def test_replay_uses_recovered_surface_payload_without_clear_method(self) -> None:
        stream = {
            "format": "b2-recomp-render-command-stream",
            "write_count": 3,
            "writes": [
                {"kind": "d3d_push_buffer", "address": 0x80000080, "value": 0x808080FF},
                {"kind": "d3d_push_buffer", "address": 0x800000A0, "value": 0x808080FF},
                {"kind": "d3d_push_buffer", "address": 0x800000C0, "value": 0x00000004},
            ],
        }

        decoded = decode_render_stream(stream)
        replay = replay_render_stream(stream)

        payload = decoded["frame_state"]["surface_payload"]
        self.assertEqual(payload["sample_count"], 3)
        self.assertEqual(payload["dominant_dword_hex"], "0x808080FF")
        self.assertEqual(payload["dominant_sample_count"], 2)
        self.assertIsNone(decoded["frame_state"]["clear_color"])
        self.assertEqual(
            replay["frame_state"]["diagnostic_clear_source"],
            "recovered_surface_payload",
        )
        self.assertEqual(
            replay["frame_state"]["diagnostic_clear_color"],
            payload["dominant_color"]["normalized"],
        )

    def test_replay_translates_clear_surface_and_begin_end_to_vulkan_work(self) -> None:
        stream = {
            "format": "b2-recomp-render-command-stream",
            "write_count": 8,
            "writes": [
                {"kind": "d3d_push_buffer", "address": 0x80000080, "value": 0x00041810},
                {"kind": "d3d_push_buffer", "address": 0x80000084, "value": 0x804020FF},
                {"kind": "d3d_push_buffer", "address": 0x80000088, "value": 0x00040300},
                {"kind": "d3d_push_buffer", "address": 0x8000008C, "value": 0x000000F0},
                {"kind": "d3d_push_buffer", "address": 0x80000090, "value": 0x000406B0},
                {"kind": "d3d_push_buffer", "address": 0x80000094, "value": 0x00000004},
                {"kind": "d3d_push_buffer", "address": 0x80000098, "value": 0x000401D8},
                {"kind": "d3d_push_buffer", "address": 0x8000009C, "value": 0x02800010},
            ],
        }

        replay = replay_render_stream(stream, width=640, height=480)
        work_kinds = [item["translated_kind"] for item in replay["vulkan_work"]]

        self.assertEqual(replay["push_buffer_method_packet_count"], 4)
        self.assertEqual(replay["interpreted_method_count"], 4)
        self.assertEqual(replay["vulkan_draw_work_count"], 1)
        self.assertGreaterEqual(replay["vulkan_clear_work_count"], 2)
        self.assertIn("vulkan_cmd_clear_attachments", work_kinds)
        self.assertIn("vulkan_cmd_draw", work_kinds)
        self.assertTrue(replay["frame_state"]["clear_surface"]["clears_color"])
        self.assertEqual(
            replay["frame_state"]["clear_surface"]["color_channel_mask_hex"],
            "0x000000F0",
        )
        self.assertEqual(replay["frame_state"]["begin_end"]["primitive"], "triangle_list")
        self.assertIn("color", replay["vulkan_state"]["clear_attachments"])
        clear = next(
            item
            for item in replay["vulkan_work"]
            if item["translated_kind"] == "vulkan_cmd_clear_attachments"
        )
        self.assertIn("color", clear["clear_attachments"])
        draw = next(
            item
            for item in replay["vulkan_work"]
            if item["translated_kind"] == "vulkan_cmd_draw"
        )
        self.assertEqual(draw["primitive"], "triangle_list")
        self.assertEqual(replay["vulkan_state"]["render_area"]["x"], 0x10)
        self.assertEqual(replay["vulkan_state"]["render_area"]["width"], 0x280)


if __name__ == "__main__":
    unittest.main()
