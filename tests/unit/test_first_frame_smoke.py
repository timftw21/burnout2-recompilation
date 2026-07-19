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
        self.assertIn("if live_render_stream or analyze_render_stream", runner_source)

    def test_headless_render_stream_analysis_reports_transform_coverage(self) -> None:
        host_source = Path("runtime/host/vulkan_first_frame.cpp").read_text(
            encoding="utf-8"
        )
        runner_source = Path("tools/host/first_frame_smoke.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("--analyze-render-stream", host_source)
        self.assertIn("render_stream_analysis_complete", host_source)
        self.assertIn("continuation_bootstrapped", host_source)
        self.assertIn("nv2a_render_state_diagnostics", host_source)
        self.assertIn("indexed_program_tiny_coverage_suspected", host_source)
        self.assertIn("subpixel_indexed_program_draw_count", host_source)
        self.assertIn("viewport_constants_valid", host_source)
        self.assertIn("draw_has_supported_host_transform", host_source)
        self.assertIn("NV097_SET_VIEWPORT_OFFSET aliases vertex constant 59", host_source)
        self.assertIn("NV097_SET_VIEWPORT_SCALE aliases vertex constant 58", host_source)
        self.assertIn("method == 0x1E90u", host_source)
        self.assertIn("(draw.transform_execution_mode & 3u) != 2u", host_source)
        self.assertIn("--analyze-render-stream", runner_source)
        self.assertIn("render_stream_analysis_completed", runner_source)

    def test_program_vertices_reconstruct_homogeneous_clip_positions(self) -> None:
        host_source = Path("runtime/host/vulkan_first_frame.cpp").read_text(
            encoding="utf-8"
        )
        shader_source = Path("runtime/host/shaders/nv2a_inline.vert").read_text(
            encoding="utf-8"
        )

        self.assertIn("program_position_valid", host_source)
        self.assertIn("vertex.x *= vertex.w", host_source)
        self.assertIn("vertex.z *= vertex.w", host_source)
        self.assertIn("VK_FORMAT_R32G32B32A32_SFLOAT", host_source)
        self.assertIn("layout(location = 0) in vec4 in_position", shader_source)
        self.assertIn("gl_Position = in_position", shader_source)
        self.assertIn("create_depth_resources();", host_source)
        self.assertIn("nv2a_depth_compare_op", host_source)
        self.assertIn("state.depth_write_enable", host_source)
        self.assertIn("clip_width >= swapchain_extent_.width", host_source)
        self.assertIn("clip_height >= swapchain_extent_.height", host_source)
        self.assertIn("nv2a_cull_mode", host_source)
        self.assertIn("nv2a_front_face", host_source)
        self.assertIn("--presented-draw-begin", host_source)
        self.assertIn("--presented-draw-end", host_source)
        vertex_program_source = Path("runtime/host/nv2a_vertex_program.h").read_text(
            encoding="utf-8"
        )
        self.assertIn("index == 12u", vertex_program_source)
        self.assertIn("ilu_opcode != 0u && temporary_index == 1u", vertex_program_source)

    def test_live_reload_waits_only_for_in_flight_frame_and_reuses_pipelines(self) -> None:
        source = Path("runtime/host/vulkan_first_frame.cpp").read_text(encoding="utf-8")

        self.assertIn('"vkWaitForFences(live reload)"', source)
        self.assertNotIn('"vkDeviceWaitIdle(live reload)"', source)
        self.assertIn("nv2a_graphics_pipeline_cache_hit", source)
        self.assertIn("graphics_pipelines_.begin(), graphics_pipelines_.end()", source)
        self.assertIn("live_render_stream_reload_skipped", source)
        self.assertIn("already_presented_without_command_or_resource_delta", source)

    def test_live_reload_cost_does_not_scale_with_complete_command_history(self) -> None:
        source = Path("runtime/host/vulkan_first_frame.cpp").read_text(encoding="utf-8")

        self.assertIn("interpret_recovered_d3d_append", source)
        self.assertIn("last_interpreted_command_delta_", source)
        self.assertIn("presentable_command_record_count", source)
        self.assertNotIn("tail_context_commands = 65536", source)
        self.assertIn("record_count > commands.capacity()", source)
        self.assertIn("commands.size() / 4u", source)
        self.assertIn("std::array<uint8_t, 8> payload{}", source)
        self.assertIn("uint8_t payload_size = 0", source)
        command_struct = source.split("struct RecoveredD3DCommand", 1)[1].split("};", 1)[0]
        self.assertNotIn("std::vector<uint8_t> payload;", command_struct)
        # The one-shot loader still reserves its known final size; the live
        # append path must not reserve the exact growing count on each reload.
        self.assertEqual(source.count("commands.reserve(record_count);"), 1)

    def test_live_reload_batches_reads_reuses_texture_content_and_paces_at_60_hz(self) -> None:
        source = Path("runtime/host/vulkan_first_frame.cpp").read_text(encoding="utf-8")

        self.assertIn("std::vector<uint8_t> appended_bytes", source)
        self.assertIn("read_live_render_manifest", source)
        self.assertIn("read_text_handle_shared(live_manifest_file_)", source)
        self.assertIn('"B2PRS001"', source)
        self.assertIn("acknowledge_current_presentation();", source)
        self.assertIn("OpenEventW", source)
        self.assertIn("SetEvent(presentation_ack_event_)", source)
        self.assertIn("WaitForMultipleObjects", source)
        self.assertIn("publication_event_", source)
        self.assertIn('"B2TEX001"', source)
        self.assertIn("texture_content_identity", source)
        self.assertIn("retain_matching", source)
        self.assertIn('"nv2a_texture_resources_refreshed"', source)
        self.assertIn("std::chrono::nanoseconds(16666667)", source)
        self.assertIn("CreateWaitableTimerExW", source)
        self.assertIn("kHighResolutionWaitableTimerFlag", source)
        self.assertIn("wait_for_frame_deadline(next_frame_time)", source)
        self.assertIn("constexpr auto poll_interval = std::chrono::milliseconds(1)", source)
        self.assertIn('"target_frame_us"', source)

    def test_live_reload_retries_incomplete_shared_manifest(self) -> None:
        source = Path("runtime/host/vulkan_first_frame.cpp").read_text(encoding="utf-8")

        self.assertIn("load_initial_recovered_render_work", source)
        self.assertIn("live_render_startup_manifest_retried", source)
        self.assertIn("std::chrono::seconds(2)", source)
        self.assertIn("The validated manifest is the publication boundary", source)
        self.assertIn("FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE", source)
        self.assertIn("manifest_text[final_non_space] != '}'", source)
        self.assertIn("write_time == live_render_write_time_", source)
        self.assertNotIn("command-sidecar-only fast path", source)

    def test_live_reload_samples_expensive_presented_geometry_diagnostics(self) -> None:
        source = Path("runtime/host/vulkan_first_frame.cpp").read_text(encoding="utf-8")

        self.assertIn("const bool emit_presented_details", source)
        self.assertIn("live_render_reload_count_ % 120u == 0u", source)
        self.assertIn("if (emit_presented_details)", source)
        self.assertIn('"nv2a_presented_geometry_anomalies"', source)
        self.assertIn("exact_center_origin_draw_count", source)
        self.assertIn("manifest_guest_flip_count", source)
        self.assertIn("presented_vertex_count", source)
        self.assertIn("fullscreen_draw_count", source)
        self.assertIn("presented_texture_addresses", source)

    def test_frame_readback_reports_low_information_coverage(self) -> None:
        source = Path("runtime/host/vulkan_first_frame.cpp").read_text(encoding="utf-8")

        self.assertIn("bright_count", source)
        self.assertIn("dark_count", source)
        self.assertIn("near_solid", source)
        self.assertIn("low_information", source)

    def test_keyboard_controller_pulses_survive_guest_polling_interval(self) -> None:
        source = Path("runtime/host/vulkan_first_frame.cpp").read_text(encoding="utf-8")

        self.assertIn("kControllerMinimumPulseMs = 150", source)
        self.assertIn("kControllerMaximumPulseMs = 2000", source)
        self.assertIn("kControllerMinimumPulseGuestFlips = 2u", source)
        self.assertIn("controller_key_down_[key_index]", source)
        self.assertIn("controller_buttons_from_keys()", source)
        self.assertIn("controller_latched_buttons_ |= bit", source)
        self.assertIn("controller_latch_guest_flip_counts_[bit_index]", source)
        self.assertIn("controller_latch_max_deadlines_[bit_index]", source)
        self.assertIn("guest_flip_pulse >= kControllerMinimumPulseGuestFlips", source)
        self.assertIn("update_controller_button_latches();", source)
        self.assertIn("case VK_RETURN: return 0x1000u", source)
        self.assertNotIn("case VK_RETURN: return 0x1010u", source)
        self.assertIn("case VK_SPACE: return 0x1000u", source)
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

    def test_native_texture_lookup_disambiguates_reused_guest_addresses(self) -> None:
        source = Path("runtime/host/vulkan_first_frame.cpp").read_text(encoding="utf-8")

        self.assertIn("host_texture_matches_draw", source)
        self.assertIn("texture.width == width", source)
        self.assertIn("texture.height == height", source)
        self.assertIn(
            "nv2a_texture_format_matches(texture.format, format_raw)",
            source,
        )
        self.assertIn("descriptor_for_draw(draw)", source)
        self.assertNotIn("descriptor_for_texture(uint32_t guest_address)", source)

    def test_native_replay_converts_quad_lists_to_triangle_strips(self) -> None:
        source = Path("runtime/host/vulkan_first_frame.cpp").read_text(encoding="utf-8")

        self.assertIn(
            "quad_strip_order{0u, 1u, 3u, 2u}",
            source,
        )
        self.assertIn("converted.primitive = 6u", source)
        self.assertIn("converted_quad_count", source)
        self.assertIn("discarded_quad_vertex_count", source)

    def test_native_replay_materializes_indexed_vertex_arrays(self) -> None:
        source = Path("runtime/host/vulkan_first_frame.cpp").read_text(
            encoding="utf-8"
        )

        self.assertIn("method == 0x1800u", source)
        self.assertIn("method == 0x1808u", source)
        self.assertIn("finish_indexed_draw", source)
        self.assertIn("materialize_indexed_draws", source)
        self.assertIn('resource.format != "VERTEX_BUFFER"', source)
        self.assertIn("vertex.program_inputs_valid = true", source)
        self.assertIn('"materialized_indexed_draws"', source)
        self.assertIn(
            '"missing_presented_indexed_resource_draw_count"',
            source,
        )

    def test_native_replay_preserves_current_attributes_for_disabled_arrays(
        self,
    ) -> None:
        source = Path("runtime/host/vulkan_first_frame.cpp").read_text(
            encoding="utf-8"
        )

        self.assertIn("Nv2aVertexAttributes current_vertex_attributes", source)
        self.assertIn("method >= 0x1940u && method <= 0x197Cu", source)
        self.assertIn("static_cast<float>(data & 0xFFu) / 255.0f", source)
        self.assertIn(
            "vertex.program_inputs = draw.current_vertex_attributes",
            source,
        )
        self.assertIn(
            "0x1A00u + static_cast<uint32_t>(attribute) * 16u",
            source,
        )

    def test_native_replay_distinguishes_widget_scissors_from_offscreen_targets(
        self,
    ) -> None:
        source = Path("runtime/host/vulkan_first_frame.cpp").read_text(
            encoding="utf-8"
        )

        self.assertIn("presented_surface_color_offset", source)
        self.assertIn("draw_targets_presented_surface", source)
        self.assertIn("draw_surface_clip_is_subsurface_viewport", source)
        self.assertIn("kViewportBias = 0.53125f", source)
        self.assertIn("viewport_scale_x - half_width", source)
        self.assertIn("viewport_scale_y + half_height", source)
        self.assertIn("clip_x != 0u || clip_y != 0u", source)
        self.assertIn("VK_DYNAMIC_STATE_SCISSOR", source)
        self.assertIn("vkCmdSetScissor", source)
        self.assertIn("draw_scissor(draw, target_extent)", source)
        self.assertIn('"offscreen_render_target_draw_count"', source)
        self.assertIn("state.primitive == 5u", source)
        self.assertIn("VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST", source)
        self.assertIn("draw.primitive != 5u && draw.primitive != 6u", source)

    def test_native_replay_retains_linear_render_target_feedback(self) -> None:
        source = Path("runtime/host/vulkan_first_frame.cpp").read_text(encoding="utf-8")

        self.assertIn("texture_image_rects", source)
        self.assertIn("nv2a_texture_extent", source)
        self.assertIn(
            "normalize_presented_linear_texture_coordinates",
            source,
        )
        self.assertIn("render_target_feedback", source)
        self.assertIn("record_render_target_feedback", source)
        self.assertIn("render_target_feedback_texture_matches_spec", source)
        self.assertIn("active_count != specs.size()", source)
        self.assertIn("|| texture.render_target_feedback", source)
        self.assertIn("const bool already_retained", source)
        self.assertIn('"render_target_feedback_stale"', source)
        self.assertIn('"render_target_feedback_missing"', source)
        self.assertIn('"render_target_feedback_required_addresses"', source)
        self.assertIn("vkCmdCopyImage(", source)
        self.assertIn("VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT", source)
        self.assertIn("VK_IMAGE_USAGE_TRANSFER_SRC_BIT", source)

    def test_native_replay_resolves_aliased_offscreen_shadow_targets(self) -> None:
        source = Path("runtime/host/vulkan_first_frame.cpp").read_text(
            encoding="utf-8"
        )

        self.assertIn("nv2a_canonical_resource_address", source)
        self.assertIn("offscreen_produced", source)
        self.assertIn("record_offscreen_render_targets", source)
        self.assertIn("host_offscreen_target_replay_supported", source)
        self.assertIn("offscreen_render_target_replay_draw_count", source)
        self.assertIn("VK_DYNAMIC_STATE_VIEWPORT", source)
        self.assertIn("create_depth_attachment", source)
        self.assertIn("target.depth_view", source)
        self.assertIn("VK_PIPELINE_STAGE_LATE_FRAGMENT_TESTS_BIT", source)
        self.assertIn('"dedicated_depth_count"', source)
        self.assertIn('"shares_presented_depth", json_bool(false)', source)

    def test_native_pipeline_preserves_captured_nv2a_blend_state(self) -> None:
        source = Path("runtime/host/vulkan_first_frame.cpp").read_text(encoding="utf-8")

        pipeline_state = source.split("struct NativePipelineState", 1)[1].split(
            "};",
            1,
        )[0]
        self.assertIn("blend_source_factor", pipeline_state)
        self.assertIn("blend_destination_factor", pipeline_state)
        self.assertIn("blend_equation", pipeline_state)
        self.assertIn(
            "case 0x0304u: return VK_BLEND_FACTOR_DST_ALPHA;",
            source,
        )
        self.assertIn(
            "case 0x800Bu: return VK_BLEND_OP_REVERSE_SUBTRACT;",
            source,
        )
        self.assertIn(
            "state.blend_source_factor,\n                VK_BLEND_FACTOR_ONE",
            source,
        )
        self.assertIn("nv2a_blend_op(state.blend_equation)", source)
        self.assertNotIn(
            "blend_attachment.srcColorBlendFactor = VK_BLEND_FACTOR_SRC_ALPHA;",
            source,
        )

    def test_native_front_face_preserves_guest_screen_winding(self) -> None:
        source = Path("runtime/host/vulkan_first_frame.cpp").read_text(
            encoding="utf-8"
        )
        front_face = source.split("VkFrontFace nv2a_front_face", 1)[1].split(
            "}", 1
        )[0]

        self.assertIn("face == 0x0900u", front_face)
        self.assertIn("? VK_FRONT_FACE_CLOCKWISE", front_face)
        self.assertIn(": VK_FRONT_FACE_COUNTER_CLOCKWISE", front_face)

    def test_native_swapchain_preserves_nv2a_combiner_byte_values(self) -> None:
        source = Path("runtime/host/vulkan_first_frame.cpp").read_text(
            encoding="utf-8"
        )
        choose_format = source.split("choose_surface_format", 1)[1].split(
            "VkExtent2D choose_extent", 1
        )[0]

        self.assertLess(
            choose_format.index("VK_FORMAT_B8G8R8A8_UNORM"),
            choose_format.index("VK_FORMAT_B8G8R8A8_SRGB"),
        )
        self.assertIn("xbox_combiner_unorm_output", source)

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
            "static_cast<float>(target_height) - 1.0f",
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
        self.assertIn("retain_hotkey_render_capture", host_source)
        self.assertIn("hotkey_render_capture_retained", host_source)
        self.assertIn('capture_screenshot(pending_hotkey_screenshot_path_, "f12")', host_source)
        self.assertIn('"render_capture_manifest"', host_source)
        self.assertIn("--hotkey-screenshot-directory", host_source)
        self.assertIn("DEFAULT_HOTKEY_SCREENSHOT_DIR", runner_source)

    def test_f12_retains_only_the_completed_flip_command_prefix(self) -> None:
        source = Path("runtime/host/vulkan_first_frame.cpp").read_text(
            encoding="utf-8"
        )

        self.assertIn("command_snapshot_source_record_count", source)
        self.assertIn("command_snapshot_trimmed_record_count", source)
        self.assertIn("command_snapshot_exact_prefix", source)
        self.assertIn("std::filesystem::resize_file", source)
        self.assertIn(
            "commands.resize(static_cast<size_t>(declared_record_count))",
            source,
        )
        self.assertIn("native_draw_interpreter_bootstrap_methods", source)
        self.assertIn("interpreter-bootstrap.bin", source)
        self.assertIn("interpreter_bootstrap_path", source)
        self.assertIn("load_interpreter_bootstrap_state", source)

    def test_native_replay_uses_latest_completed_guest_flip(self) -> None:
        source = Path("runtime/host/vulkan_first_frame.cpp").read_text(encoding="utf-8")

        self.assertIn("if (method == 0x012Cu)", source)
        self.assertIn("presented_draw_begin = interpreted.frame_draw_begin", source)
        self.assertIn("draw_index < end_draw", source)

    def test_live_replay_accepts_acknowledged_command_continuation_epochs(self) -> None:
        source = Path("runtime/host/vulkan_first_frame.cpp").read_text(encoding="utf-8")

        self.assertIn("command_snapshot_base_record_count", source)
        self.assertIn("resident_interpret_begin", source)
        self.assertIn("options_.analyze_render_stream_only", source)
        self.assertIn("interpreted_source_command_count_ = command_base_count", source)
        self.assertIn("render_stream_continuation_bootstrapped", source)
        self.assertIn(
            "live render continuation snapshot cannot bootstrap a new presenter",
            source,
        )
        self.assertIn(
            "live render continuation snapshot skipped uninterpreted commands",
            source,
        )

    def test_renderer_translates_texture_alpha_and_fixed_function_state(self) -> None:
        host_source = Path("runtime/host/vulkan_first_frame.cpp").read_text(
            encoding="utf-8"
        )
        vertex_shader = Path(
            "runtime/host/shaders/nv2a_inline.vert"
        ).read_text(encoding="utf-8")
        fragment_shader = Path(
            "runtime/host/shaders/nv2a_inline.frag"
        ).read_text(encoding="utf-8")

        self.assertIn("register_offset == 0x08u", host_source)
        self.assertIn("register_offset == 0x14u", host_source)
        self.assertIn("VK_SAMPLER_ADDRESS_MODE_REPEAT", host_source)
        self.assertIn("VK_SAMPLER_ADDRESS_MODE_MIRRORED_REPEAT", host_source)
        self.assertIn("host_texture_bindings_", host_source)
        self.assertIn("HostTextureBindingSpec", host_source)
        self.assertIn("draw.texture_filters[stage]", host_source)
        self.assertIn("decompress_dxt_mip_chain", host_source)
        self.assertIn("image_info.mipLevels = texture.mip_levels", host_source)
        self.assertIn("sampler_info.mipmapMode", host_source)
        self.assertIn("sampler_info.mipLodBias", host_source)
        self.assertIn("convert_bgra8_texture", host_source)
        self.assertIn("convert_r5g6b5_texture", host_source)
        self.assertIn("unswizzle_texture_2d", host_source)
        self.assertIn("opaque_x8_alpha_forced", host_source)
        self.assertIn("method == 0x0300u", host_source)
        self.assertIn("method == 0x033Cu", host_source)
        self.assertIn("method == 0x0340u", host_source)
        self.assertIn("method >= 0x0680u && method <= 0x06BCu", host_source)
        self.assertIn("execute_presented_fixed_function_transform", host_source)
        self.assertIn("fixed_function_transformed_draw_count", host_source)
        self.assertIn("vkCmdPushConstants", host_source)
        self.assertIn("layout(location = 2) in vec4 in_uv", vertex_shader)
        self.assertIn("textureProj(texture0, frag_uv.xyw)", fragment_shader)
        self.assertIn("texture_alpha_kill", fragment_shader)
        self.assertIn("texture_opaque_alpha", fragment_shader)
        self.assertLess(
            fragment_shader.index("sampled.a = 1.0"),
            fragment_shader.index("state.texture_alpha_kill"),
        )
        self.assertIn("discard", fragment_shader)

    def test_native_alpha_kill_requires_an_active_texture_stage(self) -> None:
        host_source = Path("runtime/host/vulkan_first_frame.cpp").read_text(
            encoding="utf-8"
        )
        fragment_shader = Path(
            "runtime/host/shaders/nv2a_inline.frag"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "state.texture_mode = draw.texture_enabled",
            host_source,
        )
        self.assertIn(
            "state.texture_alpha_kill = state.texture_mode != 0u",
            host_source,
        )
        self.assertIn(
            "if (state.texture_mode != 0u\n"
            "        && state.texture_alpha_kill != 0u",
            fragment_shader,
        )

    def test_native_replay_overlays_recovered_text_on_rendered_frames(self) -> None:
        source = Path("runtime/host/vulkan_first_frame.cpp").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "frontend_text_rectangle_count_ = append_frontend_text_vertices(",
            source,
        )
        self.assertNotIn("if (interpreted_stream_.draws.empty())", source)
        self.assertIn("frontend_text_pipeline_state()", source)
        self.assertIn("vkCmdDraw(\n            command_buffer,\n            frontend_text_vertex_count_", source)
        self.assertNotIn("vkCmdClearAttachments", source)
        self.assertIn("rasterize_frontend_text", source)
        self.assertIn('L"Impact"', source)
        self.assertIn("recovered_source_.frontend_text_x_bits", source)
        self.assertIn("recovered_source_.frontend_text_color_argb", source)
        self.assertIn("vertex.a = alpha * color_a", source)
        self.assertIn("state.blend_source_factor = 0x0302u", source)
        self.assertIn("json_string(recovered_frontend_text_)", source)

    def test_native_push_buffer_packets_carry_only_across_contiguous_appends(self) -> None:
        source = Path("runtime/host/vulkan_first_frame.cpp").read_text(encoding="utf-8")

        self.assertIn("bool run_start = false", source)
        self.assertIn("if (words[index].run_start)", source)
        self.assertIn("interpret_pending_push_buffer_method_packet", source)
        self.assertIn(
            "words[index].address != interpreted.pending_next_address", source
        )

    def test_native_replay_retains_full_dynamic_push_buffer_aperture(self) -> None:
        source = Path("runtime/host/vulkan_first_frame.cpp").read_text(encoding="utf-8")

        self.assertIn(
            "kRecoveredPushBufferApertureSize = 0x01000000u",
            source,
        )
        self.assertIn("std::vector<uint8_t> pending_bytes(aperture_span)", source)
        self.assertNotIn("std::array<uint8_t, 0x10000> pending_bytes", source)

    def test_live_vertex_upload_reuses_mapping_and_compacts_presented_span(self) -> None:
        source = Path("runtime/host/vulkan_first_frame.cpp").read_text(
            encoding="utf-8"
        )

        self.assertIn("void* vertex_mapped_ = nullptr", source)
        self.assertIn("if (!vertex_mapped_)", source)
        self.assertIn("presented_vertex_span(", source)
        self.assertIn("uploaded_vertex_base_", source)
        self.assertIn("uploaded_vertex_count_", source)
        self.assertIn("draw.first_vertex - uploaded_vertex_base_", source)
        self.assertIn('"uploaded_vertices"', source)

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
                            "event": "hotkey_render_capture_retained",
                            "manifest": "reports/local/screenshots/capture-render/render.json",
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
        self.assertEqual(summary["run"]["hotkey_render_capture_count"], 1)
        self.assertEqual(
            summary["run"]["hotkey_render_capture_manifests"],
            ["reports/local/screenshots/capture-render/render.json"],
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
        self.assertIn("-O2", command)
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
