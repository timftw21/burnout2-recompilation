from __future__ import annotations

import unittest

from tools.render.d3d8_stream import (
    decode_render_stream,
    extract_render_streams_from_probe_summary,
    merge_render_streams,
    normalize_render_stream,
    replay_render_stream,
)


class RenderD3D8StreamTests(unittest.TestCase):
    def test_reassembles_locally_out_of_order_push_buffer_payload(self) -> None:
        decoded = decode_render_stream(
            {
                "format": "b2-recomp-render-command-stream",
                "writes": [
                    {"kind": "d3d_push_buffer", "address": 0x80000080, "value": 0x00081D90},
                    {"kind": "d3d_push_buffer", "address": 0x80000088, "value": 0x000000F0},
                    {"kind": "d3d_push_buffer", "address": 0x80000084, "value": 0x804020FF},
                ],
            }
        )

        packet = next(
            command for command in decoded["commands"]
            if command.get("method") == "nv2a_increasing_methods"
        )
        self.assertEqual(
            [(method["name"], method["data_hex"]) for method in packet["methods"]],
            [("clear_color", "0x804020FF"), ("clear_surface", "0x000000F0")],
        )

    def test_merges_sequential_windows_for_persistent_nv2a_state(self) -> None:
        first = {
            "format": "b2-recomp-render-command-stream",
            "write_count": 1,
            "writes": [
                {"sequence": 7, "kind": "d3d_push_buffer", "address": 0x8000, "value": 1}
            ],
        }
        second = {
            "format": "b2-recomp-render-command-stream",
            "write_count": 1,
            "writes": [
                {"sequence": 99, "kind": "d3d_push_buffer", "address": 0x8004, "value": 2}
            ],
        }

        merged = merge_render_streams(first, second)

        self.assertEqual(merged["write_count"], 2)
        self.assertEqual([write["sequence"] for write in merged["writes"]], [0, 1])
        self.assertEqual(
            [write["source_sequence"] for write in merged["writes"]], [7, 99]
        )
        self.assertEqual([write["source_window"] for write in merged["writes"]], [0, 1])

    def test_extracts_first_class_stream_from_probe_summary(self) -> None:
        summary = {
            "format": "b2-recomp-playability-probe",
            "entry_recovery": {
                "execution": {
                    "title_text_draw_fast_path": {
                        "sampled_strings": [{"bytes_hex": "48656C6C6F"}]
                    },
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
        self.assertEqual(streams[0]["source"]["frontend_text"], "Hello")
        self.assertEqual(streams[0]["captured_write_count"], 2)

    def test_extract_prefers_render_observer_stream_when_present(self) -> None:
        summary = {
            "format": "b2-recomp-playability-probe",
            "entry_recovery": {
                "execution": {
                    "status": "returned",
                    "render_watchpoint_stream": {
                        "write_count": 1,
                        "mmio_write_count": 0,
                        "push_buffer_write_count": 1,
                        "writes": [
                            {
                                "sequence": 1,
                                "kind": "d3d_push_buffer",
                                "address": 0x80000080,
                                        "value": 0x00041D90,
                            }
                        ],
                    },
                    "guest_thread_executions": [
                        {
                            "status": "returned_to_guest",
                            "start_address_hex": "0x000E682C",
                            "render_command_stream": {
                                "write_count": 1,
                                "mmio_write_count": 1,
                                "push_buffer_write_count": 0,
                                "writes": [
                                    {
                                        "sequence": 2,
                                        "kind": "d3d_mmio",
                                        "address": 0xFED00048,
                                        "value": 0x1200,
                                    }
                                ],
                            },
                        }
                    ],
                }
            },
        }

        streams = extract_render_streams_from_probe_summary(summary)

        self.assertEqual(len(streams), 2)
        self.assertEqual(streams[0]["source"]["kind"], "playability_probe_render_observer")
        self.assertEqual(streams[0]["push_buffer_write_count"], 1)
        self.assertEqual(streams[1]["source"]["kind"], "playability_probe_guest_thread")

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
                        "value": 0x90,
                        "size": 1,
                    },
                    {
                        "kind": "d3d_push_buffer",
                        "address": 0x80000081,
                        "value": 0x1D,
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
        self.assertEqual(decoded["commands"][1]["bytes_hex"], "901D0400FF204080")
        self.assertEqual(decoded["commands"][2]["method"], "nv2a_increasing_methods")
        self.assertEqual(decoded["commands"][2]["first_method_hex"], "0x00001D90")
        self.assertEqual(decoded["commands"][2]["methods"][0]["name"], "clear_color")
        self.assertEqual(decoded["commands"][2]["methods"][0]["data_hex"], "0x804020FF")
        self.assertEqual(decoded["method_counts"], {"clear_color": 1})
        self.assertEqual(decoded["unnamed_method_counts"], {})
        self.assertEqual(decoded["visual_gap_inventory"]["unnamed_method_writes"], 0)
        self.assertEqual(decoded["visual_gap_inventory"]["inline_vertex_words"], 0)
        self.assertEqual(decoded["frame_state"]["clear_color"]["r8"], 0x40)
        self.assertEqual(decoded["frame_state"]["clear_color"]["g8"], 0x20)
        self.assertEqual(decoded["frame_state"]["clear_color"]["b8"], 0xFF)
        self.assertEqual(decoded["state_update_count"], 1)

    def test_decodes_eight_byte_push_buffer_write_payload(self) -> None:
        stream = normalize_render_stream(
            {
                "format": "b2-recomp-render-command-stream",
                "write_count": 1,
                "push_buffer_write_count": 1,
                "writes": [
                    {
                        "kind": "d3d_push_buffer",
                        "address": 0x80000080,
                        "value": 0x00041D90,
                        "size": 8,
                        "bytes_hex": "901D0400FF204080",
                    },
                ],
            }
        )

        decoded = decode_render_stream(stream)

        self.assertEqual(stream["writes"][0]["bytes_hex"], "901D0400FF204080")
        self.assertEqual(decoded["commands"][0]["byte_count"], 8)
        self.assertEqual(decoded["commands"][0]["dword_count"], 2)
        self.assertEqual(decoded["commands"][1]["method"], "nv2a_increasing_methods")
        self.assertEqual(decoded["commands"][1]["methods"][0]["name"], "clear_color")
        self.assertEqual(decoded["commands"][1]["methods"][0]["data_hex"], "0x804020FF")

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
                {"kind": "d3d_push_buffer", "address": 0x80000080, "value": 0x00041D90},
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
                {"kind": "d3d_push_buffer", "address": 0x80000080, "value": 0x00041D90},
                {"kind": "d3d_push_buffer", "address": 0x80000084, "value": 0x804020FF},
                {"kind": "d3d_push_buffer", "address": 0x80000088, "value": 0x00041D94},
                {"kind": "d3d_push_buffer", "address": 0x8000008C, "value": 0x000000F0},
                {"kind": "d3d_push_buffer", "address": 0x80000090, "value": 0x000417FC},
                {"kind": "d3d_push_buffer", "address": 0x80000094, "value": 0x00000005},
                {"kind": "d3d_push_buffer", "address": 0x80000098, "value": 0x00040200},
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

    def test_replay_translates_observed_inline_vertex_methods_to_draw_work(self) -> None:
        formats = [0x42, 2, 2, 0x40, 2, 2, 2, 2, 2, 0x22, 2, 2, 2, 2, 2, 2]
        inline_words = [
            0x00000000, 0x00000000, 0, 0, 0xFFFF0000, 0, 0,
            0x3F800000, 0x00000000, 0, 0, 0xFF00FF00, 0x3F800000, 0,
            0x00000000, 0x3F800000, 0, 0, 0xFF0000FF, 0, 0x3F800000,
        ]
        values = [
            0x00401760,
            *formats,
            0x000417FC,
            5,
            0x40541818,
            *inline_words,
            0x000417FC,
            0,
        ]
        stream = {
            "format": "b2-recomp-render-command-stream",
            "write_count": len(values),
            "writes": [
                {
                    "kind": "d3d_push_buffer",
                    "address": 0x80000080 + index * 4,
                    "value": value,
                }
                for index, value in enumerate(values)
            ],
        }

        replay = replay_render_stream(stream)

        self.assertEqual(replay["vulkan_draw_work_count"], 1)
        self.assertEqual(
            replay["frame_state"]["inline_vertex_stream"]["method_count"],
            21,
        )
        draw = next(
            item
            for item in replay["vulkan_work"]
            if item["translated_kind"] == "vulkan_cmd_draw"
        )
        self.assertEqual(draw["vertex_source"], "inline_array")
        self.assertEqual(draw["primitive"], "triangle_list")
        self.assertEqual(draw["vertex_count"], 3)
        self.assertEqual(draw["vertices"][1]["color"]["g8"], 0xFF)


if __name__ == "__main__":
    unittest.main()
