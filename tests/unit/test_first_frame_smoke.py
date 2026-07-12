from __future__ import annotations

import tempfile
import struct
import unittest
from pathlib import Path

from tools.host.first_frame_smoke import (
    Toolchain,
    build_command,
    inspect_bmp,
    read_debug_events,
    summarize_smoke,
)


class FirstFrameSmokeTests(unittest.TestCase):
    def test_live_presenter_hot_reloads_render_and_publishes_controller_state(self) -> None:
        host_source = Path("runtime/host/vulkan_first_frame.cpp").read_text(encoding="utf-8")
        runner_source = Path("tools/host/first_frame_smoke.py").read_text(encoding="utf-8")
        self.assertIn("--live-render-stream-json", host_source)
        self.assertIn("live_render_stream_reloaded", host_source)
        self.assertIn("--controller-state-json", host_source)
        self.assertIn("controller_state_published", host_source)
        self.assertIn('"--live-render-stream-json" if live_render_stream', runner_source)

    def test_keyboard_controller_pulses_survive_guest_polling_interval(self) -> None:
        source = Path("runtime/host/vulkan_first_frame.cpp").read_text(encoding="utf-8")

        self.assertIn("kControllerMinimumPulseMs = 150", source)
        self.assertIn("controller_latched_buttons_ |= mask", source)
        self.assertIn("update_controller_button_latches();", source)
        self.assertIn("case VK_RETURN: return 0x1010u", source)
        self.assertIn("effective_controller_buttons()", source)

    def test_native_dxt1_decoder_uses_linear_block_order(self) -> None:
        source = Path("runtime/host/vulkan_first_frame.cpp").read_text(encoding="utf-8")

        self.assertIn("block_index % blocks_x", source)
        self.assertIn("block_index / blocks_x", source)
        self.assertNotIn("block_index >> (bit * 2u)", source)

    def test_native_dxt5_decoder_uploads_title_alpha_textures(self) -> None:
        source = Path("runtime/host/vulkan_first_frame.cpp").read_text(encoding="utf-8")

        self.assertIn("decompress_dxt5", source)
        self.assertIn('resource.format == "DXT5"', source)
        self.assertIn("alpha_indices >> (3u * pixel_index)", source)
        self.assertIn("alphas[6] = 0u", source)
        self.assertIn("alphas[7] = 255u", source)

    def test_strict_render_validation_rejects_white_fallback_textures(self) -> None:
        host_source = Path("runtime/host/vulkan_first_frame.cpp").read_text(encoding="utf-8")
        runner_source = Path("tools/host/first_frame_smoke.py").read_text(encoding="utf-8")

        self.assertIn("--strict-render-validation", host_source)
        self.assertIn("unmatched_presented_texture_draw_count", host_source)
        self.assertIn("strict render validation failed", host_source)
        self.assertIn("--strict-render-validation", runner_source)

    def test_lossless_flip_ack_uses_share_friendly_binary_token(self) -> None:
        source = Path("runtime/host/vulkan_first_frame.cpp").read_text(encoding="utf-8")

        self.assertIn("cannot publish lossless flip audit acknowledgement", source)
        self.assertIn('std::memcpy(ack.data(), "B2ACK001", 8)', source)
        self.assertIn("FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE", source)
        self.assertIn('ack_transport", json_string("binary")', source)

    def test_lossless_flip_health_checks_are_sampled_and_candidate_triggered(self) -> None:
        source = Path("runtime/host/vulkan_first_frame.cpp").read_text(encoding="utf-8")

        self.assertIn("flip_audit_health_interval = 30", source)
        self.assertIn('return "resource_changed"', source)
        self.assertIn('return "command_shape_changed"', source)
        self.assertIn('return "periodic_sample"', source)
        self.assertIn("const bool record_frame_readback", source)
        self.assertIn('health_check_reason", json_string', source)

    def test_native_replay_maps_top_left_guest_y_to_negative_ndc(self) -> None:
        source = Path("runtime/host/vulkan_first_frame.cpp").read_text(encoding="utf-8")

        self.assertIn(
            "vertex.y = vertex.y * 2.0f / static_cast<float>(swapchain_extent_.height) - 1.0f",
            source,
        )
        self.assertNotIn(
            "vertex.y = 1.0f - vertex.y * 2.0f / static_cast<float>(swapchain_extent_.height)",
            source,
        )

    def test_native_replay_recovers_persistent_half_surface_splash_quad(self) -> None:
        source = Path("runtime/host/vulkan_first_frame.cpp").read_text(encoding="utf-8")

        self.assertIn("recover_presented_half_surface_quad", source)
        self.assertIn("vertex.x *= 2.0f", source)
        self.assertIn("vertex.u = (vertex.u - 0.5f) * 2.0f", source)
        self.assertIn("presented_half_quad_recovered", source)

    def test_native_replay_expands_observed_448_line_present_quad(self) -> None:
        source = Path("runtime/host/vulkan_first_frame.cpp").read_text(encoding="utf-8")

        self.assertIn("output_height == 480u", source)
        self.assertIn("nearly_equal(max_y, 448.0f)", source)
        self.assertIn("static_cast<float>(output_height) / max_y", source)
        self.assertIn("presented_overscan_height_recovered", source)

    def test_native_manual_mode_accepts_zero_as_unlimited_frames(self) -> None:
        host_source = Path("runtime/host/vulkan_first_frame.cpp").read_text(encoding="utf-8")
        runner_source = Path("tools/host/first_frame_smoke.py").read_text(encoding="utf-8")

        self.assertIn("options_.max_frames == 0u || frame_count_ < options_.max_frames", host_source)
        self.assertNotIn("--max-frames must be greater than zero", host_source)
        self.assertIn("timeout=timeout_seconds if timeout_seconds > 0 else None", runner_source)

    def test_f12_captures_unique_current_frame_screenshots(self) -> None:
        host_source = Path("runtime/host/vulkan_first_frame.cpp").read_text(encoding="utf-8")
        runner_source = Path("tools/host/first_frame_smoke.py").read_text(encoding="utf-8")

        self.assertIn("key == VK_F12", host_source)
        self.assertIn("hotkey_screenshot_pending_ = true", host_source)
        self.assertIn("next_hotkey_screenshot_path()", host_source)
        self.assertIn('capture_screenshot(next_hotkey_screenshot_path(), "f12")', host_source)
        self.assertIn("--hotkey-screenshot-directory", host_source)
        self.assertIn("DEFAULT_HOTKEY_SCREENSHOT_DIR", runner_source)

    def test_native_replay_uses_latest_completed_guest_flip(self) -> None:
        source = Path("runtime/host/vulkan_first_frame.cpp").read_text(encoding="utf-8")

        self.assertIn("if (method == 0x012Cu)", source)
        self.assertIn("presented_draw_begin = interpreted.frame_draw_begin", source)
        self.assertIn("draw_index < end_draw", source)

    def test_native_push_buffer_packets_do_not_cross_capture_runs(self) -> None:
        source = Path("runtime/host/vulkan_first_frame.cpp").read_text(encoding="utf-8")

        self.assertIn("bool run_start = false", source)
        self.assertIn("index >= words.size() || words[index].run_start", source)

    def test_inspects_and_summarizes_pixel_readback_bmp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            screenshot = Path(temp_dir) / "frame.bmp"
            pixels = bytes((3, 2, 1, 255))
            screenshot.write_bytes(
                struct.pack("<2sIHHI", b"BM", 54 + len(pixels), 0, 0, 54)
                + struct.pack("<IiiHHIIiiII", 40, 1, -1, 1, 32, 0, len(pixels), 2835, 2835, 0, 0)
                + pixels
            )
            inspected = inspect_bmp(screenshot)
            summary = summarize_smoke(
                compile_result=None,
                run_result={
                    "returncode": 0,
                    "debug_json": "events.jsonl",
                    "screenshot": str(screenshot),
                    "events": [
                        {
                            "event": "frame_readback_captured",
                            "trigger": "automatic",
                            "unique_colors": 1,
                            "dominant_rgba": 0x010203FF,
                            "dominant_count": 1,
                        },
                        {
                            "event": "frame_readback_captured",
                            "trigger": "f12",
                            "output": "reports/local/screenshots/capture.bmp",
                            "unique_colors": 1,
                            "dominant_rgba": 0x010203FF,
                            "dominant_count": 1,
                        },
                    ],
                },
            )

        self.assertEqual(inspected["width"], 1)
        self.assertEqual(inspected["height"], 1)
        self.assertEqual(inspected["bits_per_pixel"], 32)
        self.assertTrue(summary["run"]["pixel_readback_captured"])
        self.assertEqual(summary["run"]["readback_unique_colors"], 1)
        self.assertEqual(summary["run"]["readback_dominant_rgba"], 0x010203FF)
        self.assertEqual(summary["run"]["readback_dominant_count"], 1)
        self.assertEqual(summary["run"]["hotkey_screenshot_count"], 1)
        self.assertEqual(
            summary["run"]["hotkey_screenshot_outputs"],
            ["reports/local/screenshots/capture.bmp"],
        )

    def test_build_command_targets_windows_vulkan_dependencies(self) -> None:
        toolchain = Toolchain(
            clangxx=Path("C:/Program Files/LLVM/bin/clang++.exe"),
            vulkan_sdk=Path("C:/VulkanSDK/1.4.341.1"),
        )

        command = build_command(
            source=Path("runtime/host/vulkan_first_frame.cpp"),
            output=Path("build/local/first-frame/b2_first_frame.exe"),
            toolchain=toolchain,
        )

        self.assertIn("-std=c++17", command)
        self.assertIn("-DUNICODE", command)
        self.assertIn("-D_UNICODE", command)
        self.assertIn("-lvulkan-1", command)
        self.assertIn("-luser32", command)
        self.assertIn("-lgdi32", command)
        self.assertIn("-lshell32", command)
        normalized = [item.replace("\\", "/") for item in command]
        self.assertTrue(any(item.startswith("-IC:/VulkanSDK/1.4.341.1") for item in normalized))

    def test_reads_jsonl_debug_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "events.jsonl"
            path.write_text(
                '{"sequence":0,"event":"main_loop_enter"}\n'
                '{"sequence":1,"event":"frame_presented","frame":1}\n',
                encoding="utf-8",
            )

            events = read_debug_events(path)

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["event"], "main_loop_enter")
        self.assertEqual(events[1]["frame"], 1)

    def test_summarizes_first_frame_and_input_evidence(self) -> None:
        run_result = {
            "returncode": 0,
            "debug_json": "reports/local/first-frame/events.jsonl",
            "events": [
                {"sequence": 0, "event": "startup"},
                {"sequence": 1, "event": "physical_device_selected", "name": "GPU"},
                {"sequence": 2, "event": "main_loop_enter"},
                {"sequence": 3, "event": "input_injected", "virtual_key": 32},
                {
                    "sequence": 4,
                    "event": "input_event",
                    "message": "keydown",
                    "virtual_key": 32,
                    "frame": 1,
                },
                {
                    "sequence": 5,
                    "event": "input_event",
                    "message": "keyup",
                    "virtual_key": 32,
                    "frame": 1,
                },
                {
                    "sequence": 6,
                    "event": "input_injection_processed",
                    "messages": 2,
                    "input_events": 2,
                    "before_frame": 1,
                },
                {
                    "sequence": 7,
                    "event": "recovered_d3d_command_stream_loaded",
                    "commands": 6,
                    "mmio_writes": 4,
                    "push_buffer_writes": 2,
                    "source": "reports/local/render/recovered-d3d-stream.json",
                },
                {
                    "sequence": 8,
                    "event": "d3d8_stream_interpreted",
                    "push_buffer_words": 2,
                    "method_packets": 1,
                    "interpreted_methods": 1,
                    "zero_count_method_words": 0,
                    "surface_payload_samples": 2,
                    "surface_payload_dominant_count": 1,
                    "surface_payload_color_valid": "false",
                    "diagnostic_clear_source": "d3d8_clear_color_method",
                    "clear_color_valid": "true",
                    "translation_semantics": "d3d8-nv2a-method-push-buffer-interpretation",
                },
                {
                    "sequence": 9,
                    "event": "translated_render_work_recorded",
                    "commands": 6,
                    "method_packets": 1,
                    "interpreted_methods": 1,
                    "translation_semantics": "d3d8-nv2a-method-push-buffer-interpretation",
                },
                {
                    "sequence": 10,
                    "event": "nv2a_native_resources_created",
                    "vertices": 8,
                    "draws": 2,
                    "presented_draws": 1,
                    "guest_flips": 2,
                    "textures": 1,
                    "presented_half_quad_recovered": True,
                    "presented_overscan_height_recovered": True,
                },
                {
                    "sequence": 11,
                    "event": "frontend_text_draw_recorded",
                    "text": "Recovered frontend",
                    "rectangles": 42,
                },
                {
                    "sequence": 12,
                    "event": "frame_presented",
                    "frame": 1,
                    "translated_commands": 6,
                    "source_d3d_commands": 6,
                    "target_frame_ms": 16,
                    "pacing_sleep_ms": 12,
                },
                {"sequence": 13, "event": "main_loop_exit"},
            ],
        }
        compile_result = {
            "returncode": 0,
            "output": "build/local/first-frame/b2_first_frame.exe",
        }

        summary = summarize_smoke(
            compile_result=compile_result,
            run_result=run_result,
        )

        self.assertEqual(summary["format"], "b2-recomp-first-frame-smoke")
        self.assertEqual(summary["target_platform"], "windows")
        self.assertEqual(summary["renderer_backend"], "vulkan")
        self.assertTrue(summary["build"]["compiled"])
        self.assertTrue(summary["run"]["main_loop_entered"])
        self.assertTrue(summary["run"]["main_loop_exited"])
        self.assertTrue(summary["run"]["visible_frame_presented"])
        self.assertTrue(summary["run"]["recovered_frontend_text_drawn"])
        self.assertEqual(summary["run"]["recovered_frontend_text"], "Recovered frontend")
        self.assertEqual(summary["run"]["frontend_text_rectangles"], 42)
        self.assertEqual(summary["run"]["nv2a_presented_draws"], 1)
        self.assertEqual(summary["run"]["nv2a_guest_flips"], 2)
        self.assertTrue(summary["run"]["nv2a_presented_half_quad_recovered"])
        self.assertTrue(summary["run"]["nv2a_presented_overscan_height_recovered"])
        self.assertEqual(summary["run"]["frames_presented"], 1)
        self.assertEqual(summary["run"]["input_events"], 2)
        self.assertEqual(summary["run"]["input_keydown_events"], 1)
        self.assertEqual(summary["run"]["input_keyup_events"], 1)
        self.assertTrue(summary["run"]["injected_input_processed_before_first_frame"])
        self.assertEqual(summary["run"]["injected_input_processed_messages"], 2)
        self.assertTrue(summary["run"]["translated_renderer_work"])
        self.assertEqual(summary["run"]["translated_command_count"], 6)
        self.assertTrue(summary["run"]["recovered_d3d_command_stream"])
        self.assertEqual(summary["run"]["recovered_d3d_command_count"], 6)
        self.assertEqual(
            summary["run"]["recovered_d3d_stream_source"],
            "reports/local/render/recovered-d3d-stream.json",
        )
        self.assertEqual(summary["run"]["recovered_d3d_mmio_writes"], 4)
        self.assertEqual(summary["run"]["recovered_d3d_push_buffer_writes"], 2)
        self.assertTrue(summary["run"]["d3d8_method_interpretation"])
        self.assertEqual(
            summary["run"]["d3d8_translation_semantics"],
            "d3d8-nv2a-method-push-buffer-interpretation",
        )
        self.assertEqual(summary["run"]["d3d8_push_buffer_words"], 2)
        self.assertEqual(summary["run"]["d3d8_method_packets"], 1)
        self.assertEqual(summary["run"]["d3d8_interpreted_methods"], 1)
        self.assertEqual(summary["run"]["d3d8_zero_count_method_words"], 0)
        self.assertEqual(summary["run"]["d3d8_surface_payload_samples"], 2)
        self.assertEqual(summary["run"]["d3d8_surface_payload_dominant_count"], 1)
        self.assertFalse(summary["run"]["d3d8_surface_payload_color_valid"])
        self.assertEqual(
            summary["run"]["d3d8_diagnostic_clear_source"],
            "d3d8_clear_color_method",
        )
        self.assertTrue(summary["run"]["d3d8_clear_color_valid"])
        self.assertEqual(summary["run"]["frame_pacing"]["target_frame_ms"], 16)
        self.assertEqual(summary["run"]["frame_pacing"]["events_with_pacing"], 1)
        self.assertEqual(summary["run"]["selected_device"]["name"], "GPU")


if __name__ == "__main__":
    unittest.main()
