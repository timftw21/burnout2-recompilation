#include "vulkan_presenter_runtime.h"

namespace b2r::host::vulkan_detail {

std::vector<NativeVertex> VulkanPresenter::prepare_presented_vertices(
    bool collect_diagnostics) {
    uploaded_vertex_base_ = 0;
    uploaded_vertex_count_ = 0;
    presented_vertex_program_transformed_count_ = 0;
    presented_fixed_function_transformed_count_ = 0;
    presented_linear_texture_normalized_vertex_count_ = 0;
    presented_vertex_transform_diagnostics_ = {};
    presented_draw_transform_diagnostics_.clear();
    presented_diagnostics_valid_ = false;
    last_vertex_transform_us_ = 0;
    const auto [presented_vertex_begin, presented_vertex_end] =
        presented_vertex_span(interpreted_stream_);
    const auto transform_begin = std::chrono::steady_clock::now();
    std::vector<NativeVertex> vertices;
    if (presented_vertex_begin < presented_vertex_end) {
        vertices.assign(
            interpreted_stream_.vertices.begin()
                + static_cast<std::ptrdiff_t>(presented_vertex_begin),
            interpreted_stream_.vertices.begin()
                + static_cast<std::ptrdiff_t>(presented_vertex_end));
        uploaded_vertex_base_ = static_cast<uint32_t>(
            presented_vertex_begin);
    }
    uploaded_vertex_count_ = static_cast<uint32_t>(vertices.size());
    const size_t first_draw = std::min<size_t>(
        interpreted_stream_.presented_draw_begin,
        interpreted_stream_.draws.size());
    const size_t end_draw = std::min<size_t>(
        first_draw + interpreted_stream_.presented_draw_count,
        interpreted_stream_.draws.size());
    const std::vector<RenderTargetFeedbackSpec>& feedback_specs =
        presented_render_target_feedback_specs();
    for (size_t draw_index = first_draw; draw_index < end_draw; ++draw_index) {
        NativeDraw local_draw = interpreted_stream_.draws[draw_index];
        const bool targets_presented =
            draw_targets_presented_surface(local_draw);
        const auto offscreen_spec = std::find_if(
            feedback_specs.begin(),
            feedback_specs.end(),
            [&](const RenderTargetFeedbackSpec& spec) {
                return spec.offscreen_produced
                    && spec.producer_matches(
                        local_draw.surface_color_offset);
            });
        if (!targets_presented
            && offscreen_spec == feedback_specs.end()) {
            continue;
        }
        if (!native_pipeline_primitive_supported(local_draw.primitive)) {
            continue;
        }
        const bool use_gpu_vertex_program =
            !options_.cpu_vertex_programs
            && !options_.analyze_render_stream_only
            && gpu_vertex_program_compatible(local_draw);
        if (local_draw.gpu_raw_attribute_fetch) {
            if (!use_gpu_vertex_program) {
                continue;
            }
            if (targets_presented) {
                presented_vertex_program_transformed_count_ +=
                    local_draw.vertex_count;
            } else {
                ++offscreen_render_target_draw_count_;
                offscreen_render_target_transformed_vertex_count_ +=
                    local_draw.vertex_count;
            }
            continue;
        }
        if (local_draw.first_vertex < uploaded_vertex_base_) {
            continue;
        }
        local_draw.first_vertex -= uploaded_vertex_base_;
        if (local_draw.first_vertex + local_draw.vertex_count
            > vertices.size()) {
            continue;
        }
        PresentedDrawTransformDiagnostics draw_diagnostics{};
        if (collect_diagnostics) {
            draw_diagnostics.presented_index = static_cast<uint32_t>(
                draw_index - first_draw);
            draw_diagnostics.primitive = local_draw.primitive;
            draw_diagnostics.vertex_count = local_draw.vertex_count;
            draw_diagnostics.transform_execution_mode =
                local_draw.transform_execution_mode;
            draw_diagnostics.indexed_array = local_draw.indexed_array;
        }
        if (use_gpu_vertex_program) {
            if (targets_presented) {
                presented_vertex_program_transformed_count_ +=
                    local_draw.vertex_count;
            } else {
                ++offscreen_render_target_draw_count_;
                offscreen_render_target_transformed_vertex_count_ +=
                    local_draw.vertex_count;
            }
            // Keep decoded program inputs and original attributes intact.
            // The Vulkan vertex shader performs the program, fog, linear
            // texture normalization, and final viewport conversion.
            continue;
        }
        const uint32_t program_transformed =
            execute_presented_vertex_program(
                vertices,
                local_draw,
                collect_diagnostics ? &draw_diagnostics.transform : nullptr);
        const uint32_t fixed_transformed =
            execute_presented_fixed_function_transform(
                vertices,
                local_draw,
                collect_diagnostics ? &draw_diagnostics.transform : nullptr);
        const uint32_t linear_texture_normalized =
            normalize_presented_linear_texture_coordinates(
                vertices,
                local_draw);
        if (targets_presented) {
            presented_vertex_program_transformed_count_ +=
                program_transformed;
            presented_fixed_function_transformed_count_ +=
                fixed_transformed;
            presented_linear_texture_normalized_vertex_count_ +=
                linear_texture_normalized;
            if (collect_diagnostics) {
                presented_vertex_transform_diagnostics_.merge(
                    draw_diagnostics.transform);
            }
            presented_half_quad_recovered_ |=
                recover_presented_half_surface_quad(
                    vertices,
                    local_draw,
                    swapchain_extent_.width,
                    swapchain_extent_.height,
                    presented_overscan_height_recovered_);
        } else {
            ++offscreen_render_target_draw_count_;
            offscreen_render_target_transformed_vertex_count_ +=
                program_transformed + fixed_transformed;
            if (options_.analyze_render_stream_only) {
                const PositionBounds& output_bounds =
                    draw_diagnostics.transform.output_pixel_bounds;
                const PresentedVertexTransformDiagnostics& transform =
                    draw_diagnostics.transform;
                log_.emit(
                    "nv2a_offscreen_draw_diagnostics",
                    {
                        {"presented_index", std::to_string(
                            draw_index - first_draw)},
                        {"surface_color_offset", std::to_string(
                            local_draw.surface_color_offset)},
                        {"surface_width", std::to_string(
                            offscreen_spec->width)},
                        {"surface_height", std::to_string(
                            offscreen_spec->height)},
                        {"primitive", std::to_string(local_draw.primitive)},
                        {"vertex_count", std::to_string(
                            local_draw.vertex_count)},
                        {"texture_address", std::to_string(
                            local_draw.texture_address)},
                        {"shader_stage_program", std::to_string(
                            local_draw.shader_stage_program)},
                        {"combiner_control", std::to_string(
                            local_draw.combiner_control)},
                        {"combiner_color_input0", std::to_string(
                            local_draw.combiner_color_inputs[0])},
                        {"combiner_color_output0", std::to_string(
                            local_draw.combiner_color_outputs[0])},
                        {"combiner_alpha_input0", std::to_string(
                            local_draw.combiner_alpha_inputs[0])},
                        {"combiner_alpha_output0", std::to_string(
                            local_draw.combiner_alpha_outputs[0])},
                        {"final_combiner_inputs0", std::to_string(
                            local_draw.final_combiner_inputs0)},
                        {"final_combiner_inputs1", std::to_string(
                            local_draw.final_combiner_inputs1)},
                        {"blend_enable", std::to_string(
                            local_draw.blend_enable)},
                        {"blend_source_factor", std::to_string(
                            local_draw.blend_source_factor)},
                        {"blend_destination_factor", std::to_string(
                            local_draw.blend_destination_factor)},
                        {"blend_equation", std::to_string(
                            local_draw.blend_equation)},
                        {"depth_test_enable", std::to_string(
                            local_draw.depth_test_enable)},
                        {"depth_function", std::to_string(
                            local_draw.depth_function)},
                        {"depth_write_enable", std::to_string(
                            local_draw.depth_write_enable)},
                        {"polygon_offset_fill_enable", std::to_string(
                            local_draw.polygon_offset_fill_enable)},
                        {"polygon_offset_scale_factor", json_float(
                            float_from_u32(
                                local_draw.polygon_offset_scale_factor))},
                        {"polygon_offset_bias", json_float(float_from_u32(
                            local_draw.polygon_offset_bias))},
                        {"cull_face_enable", std::to_string(
                            local_draw.cull_face_enable)},
                        {"cull_face", std::to_string(local_draw.cull_face)},
                        {"front_face", std::to_string(local_draw.front_face)},
                        {"transform_execution_mode", std::to_string(
                            local_draw.transform_execution_mode)},
                        {"transform_program_start", std::to_string(
                            local_draw.transform_program_start)},
                        {"output_pixel_min_x", output_bounds.valid
                            ? json_float(output_bounds.min_x) : "null"},
                        {"output_pixel_max_x", output_bounds.valid
                            ? json_float(output_bounds.max_x) : "null"},
                        {"output_pixel_min_y", output_bounds.valid
                            ? json_float(output_bounds.min_y) : "null"},
                        {"output_pixel_max_y", output_bounds.valid
                            ? json_float(output_bounds.max_y) : "null"},
                        {"output_min_r", transform.output_color_bounds_valid
                            ? json_float(transform.output_min_r) : "null"},
                        {"output_max_r", transform.output_color_bounds_valid
                            ? json_float(transform.output_max_r) : "null"},
                        {"output_min_a", transform.output_color_bounds_valid
                            ? json_float(transform.output_min_a) : "null"},
                        {"output_max_a", transform.output_color_bounds_valid
                            ? json_float(transform.output_max_a) : "null"},
                    });
            }
        }
        if (targets_presented && collect_diagnostics) {
        for (uint32_t vertex_index = 0;
             vertex_index < local_draw.vertex_count;
             ++vertex_index) {
            const NativeVertex& vertex =
                vertices[local_draw.first_vertex + vertex_index];
            draw_diagnostics.final_pixel_bounds.include(vertex.x, vertex.y);
            draw_diagnostics.final_u_bounds.include(vertex.u);
            draw_diagnostics.final_v_bounds.include(vertex.v);
            draw_diagnostics.final_q_bounds.include(vertex.texture_q);
            draw_diagnostics.final_fog_bounds.include(vertex.fog);
            for (uint32_t stage = 0u; stage < 4u; ++stage) {
                for (uint32_t component = 0u; component < 4u; ++component) {
                    draw_diagnostics.final_texture_coordinate_bounds[stage]
                        [component].include(
                            vertex.texture_coordinates[stage][component]);
                }
            }
            if ((local_draw.transform_execution_mode & 3u) == 0u) {
                draw_diagnostics.fixed_function_vertices.push_back({
                    vertex.x,
                    vertex.y,
                    vertex.z,
                    vertex.w,
                    vertex.u,
                    vertex.v,
                });
            }
        }
        const uint32_t triangle_count = local_draw.primitive == 5u
            ? local_draw.vertex_count / 3u
            : local_draw.primitive == 6u && local_draw.vertex_count >= 3u
                ? local_draw.vertex_count - 2u
                : 0u;
        for (uint32_t triangle = 0;
             triangle < triangle_count;
             ++triangle) {
            uint32_t index0 = local_draw.primitive == 5u
                ? triangle * 3u
                : triangle;
            uint32_t index1 = index0 + 1u;
            const uint32_t index2 = index0 + 2u;
            if (local_draw.primitive == 6u && (triangle & 1u) != 0u) {
                std::swap(index0, index1);
            }
            const NativeVertex& vertex0 =
                vertices[local_draw.first_vertex + index0];
            const NativeVertex& vertex1 =
                vertices[local_draw.first_vertex + index1];
            const NativeVertex& vertex2 =
                vertices[local_draw.first_vertex + index2];
            const float signed_area =
                (vertex1.x - vertex0.x) * (vertex2.y - vertex0.y)
                - (vertex1.y - vertex0.y) * (vertex2.x - vertex0.x);
            if (!std::isfinite(signed_area)
                || std::abs(signed_area) <= 0.000001f) {
                ++draw_diagnostics.degenerate_triangle_count;
            } else if (signed_area > 0.0f) {
                ++draw_diagnostics.positive_area_triangle_count;
            } else {
                ++draw_diagnostics.negative_area_triangle_count;
            }
        }
        presented_draw_transform_diagnostics_.push_back(
            std::move(draw_diagnostics));
        }

        const uint32_t target_width = targets_presented
            ? swapchain_extent_.width
            : offscreen_spec->width;
        const uint32_t target_height = targets_presented
            ? swapchain_extent_.height
            : offscreen_spec->height;
        for (uint32_t vertex_index = 0;
             vertex_index < local_draw.vertex_count;
             ++vertex_index) {
            NativeVertex& vertex =
                vertices[local_draw.first_vertex + vertex_index];
            const float screen_x = vertex.x;
            const float screen_y = vertex.y;
            if (vertex.program_position_valid) {
                vertex.x = nv2a_screen_coordinate_to_vulkan_ndc(
                    screen_x, target_width);
                vertex.y = nv2a_screen_coordinate_to_vulkan_ndc(
                    screen_y, target_height);
            } else {
                vertex.x = screen_x * 2.0f
                    / static_cast<float>(target_width) - 1.0f;
                vertex.y = screen_y * 2.0f
                    / static_cast<float>(target_height) - 1.0f;
            }
            // A positive-height Vulkan viewport maps NDC -1 to the top
            // edge, matching the guest's top-left screen coordinates.
            if (vertex.program_position_valid) {
                vertex.x *= vertex.w;
                vertex.y *= vertex.w;
                vertex.z *= vertex.w;
            } else {
                vertex.z = 0.0f;
                vertex.w = 1.0f;
            }
        }
    }
    last_vertex_transform_us_ = std::chrono::duration_cast<
        std::chrono::microseconds>(
            std::chrono::steady_clock::now() - transform_begin).count();
    if (collect_diagnostics) {
        presented_diagnostics_valid_ = true;
        presented_diagnostics_generation_ = render_work_generation_;
    }
    return vertices;
}

void VulkanPresenter::emit_presented_vertex_transform_diagnostics(bool emit_draw_details) {
    const size_t first_draw = std::min<size_t>(
        interpreted_stream_.presented_draw_begin,
        interpreted_stream_.draws.size());
    const std::vector<RenderTargetFeedbackSpec>& host_feedback_specs =
        presented_render_target_feedback_specs();
    uint32_t program_draw_count = 0;
    uint32_t indexed_program_draw_count = 0;
    uint32_t subpixel_indexed_program_draw_count = 0;
    uint32_t tiny_indexed_program_draw_count = 0;
    uint32_t fixed_function_indexed_draw_count = 0;
    uint32_t raw_fallback_draw_count = 0;
    uint64_t indexed_program_vertex_count = 0;
    PositionBounds final_pixel_bounds{};
    PositionBounds indexed_program_pixel_bounds{};
    const NativeDraw* indexed_program_state = nullptr;
    for (const PresentedDrawTransformDiagnostics& draw :
         presented_draw_transform_diagnostics_) {
        const bool programmable =
            (draw.transform_execution_mode & 3u) == 2u;
        program_draw_count += programmable ? 1u : 0u;
        indexed_program_draw_count +=
            programmable && draw.indexed_array ? 1u : 0u;
        fixed_function_indexed_draw_count +=
            !programmable && draw.indexed_array ? 1u : 0u;
        raw_fallback_draw_count +=
            draw.transform.raw_position_fallback_vertex_count != 0u ? 1u : 0u;
        final_pixel_bounds.merge(draw.final_pixel_bounds);
        if (programmable && draw.indexed_array) {
            const size_t native_draw_index = first_draw + draw.presented_index;
            if (indexed_program_state == nullptr
                && native_draw_index < interpreted_stream_.draws.size()) {
                indexed_program_state = &interpreted_stream_.draws[native_draw_index];
            }
            indexed_program_vertex_count += draw.vertex_count;
            indexed_program_pixel_bounds.merge(draw.final_pixel_bounds);
            if (draw.final_pixel_bounds.valid) {
                const float span_x = draw.final_pixel_bounds.max_x
                    - draw.final_pixel_bounds.min_x;
                const float span_y = draw.final_pixel_bounds.max_y
                    - draw.final_pixel_bounds.min_y;
                subpixel_indexed_program_draw_count +=
                    span_x < 1.0f && span_y < 1.0f ? 1u : 0u;
                tiny_indexed_program_draw_count +=
                    span_x < 4.0f && span_y < 4.0f ? 1u : 0u;
            }
        }
    }
    const float indexed_program_span_x = indexed_program_pixel_bounds.valid
        ? indexed_program_pixel_bounds.max_x - indexed_program_pixel_bounds.min_x
        : 0.0f;
    const float indexed_program_span_y = indexed_program_pixel_bounds.valid
        ? indexed_program_pixel_bounds.max_y - indexed_program_pixel_bounds.min_y
        : 0.0f;
    const bool indexed_program_tiny_coverage_suspected =
        indexed_program_draw_count != 0u
        && indexed_program_pixel_bounds.valid
        && indexed_program_span_x
            < static_cast<float>(swapchain_extent_.width) * 0.05f
        && indexed_program_span_y
            < static_cast<float>(swapchain_extent_.height) * 0.05f;
    const PresentedVertexTransformDiagnostics& diagnostics =
        presented_vertex_transform_diagnostics_;
    log_.emit(
        "nv2a_vertex_transform_diagnostics",
        {
            {"reload", std::to_string(options_.live_render_stream ? live_render_reload_count_ + 1u : 0u)},
            {"manifest_guest_flip_count", std::to_string(current_manifest_guest_flip_count_)},
            {"manifest_guest_steps", std::to_string(current_manifest_guest_steps_)},
            {"launch_transform_program_count", std::to_string(interpreted_stream_.launch_transform_program_count)},
            {"failed_launch_transform_program_count", std::to_string(interpreted_stream_.failed_launch_transform_program_count)},
            {"execution_backend", json_string(
                options_.analyze_render_stream_only
                        || options_.cpu_vertex_programs
                    ? "cpu_reference"
                    : "mixed_gpu_cpu")},
            {"source_presented_draw_count", std::to_string(
                interpreted_stream_.presented_draw_count)},
            {"presented_draw_count", std::to_string(presented_draw_transform_diagnostics_.size())},
            {"gpu_vertex_program_draws", std::to_string(
                gpu_vertex_program_draw_count_)},
            {"gpu_vertex_program_vertices", std::to_string(
                gpu_vertex_program_vertex_count_)},
            {"gpu_raw_attribute_draws", std::to_string(
                gpu_raw_attribute_draw_count_)},
            {"gpu_raw_attribute_vertices", std::to_string(
                gpu_raw_attribute_vertex_count_)},
            {"cpu_vertex_program_fallback_draws", std::to_string(
                cpu_vertex_program_fallback_draw_count_)},
            {"cpu_vertex_program_fallback_vertices", std::to_string(
                cpu_vertex_program_fallback_vertex_count_)},
            {"gpu_output_diagnostics_deferred", json_bool(
                gpu_vertex_program_draw_count_ != 0u)},
            {"program_draw_count", std::to_string(program_draw_count)},
            {"indexed_program_draw_count", std::to_string(indexed_program_draw_count)},
            {"indexed_program_vertex_count", std::to_string(indexed_program_vertex_count)},
            {"subpixel_indexed_program_draw_count", std::to_string(subpixel_indexed_program_draw_count)},
            {"tiny_indexed_program_draw_count", std::to_string(tiny_indexed_program_draw_count)},
            {"fixed_function_indexed_draw_count", std::to_string(fixed_function_indexed_draw_count)},
            {"raw_fallback_draw_count", std::to_string(raw_fallback_draw_count)},
            {"eligible_vertex_count", std::to_string(diagnostics.eligible_vertex_count)},
            {"valid_program_vertex_count", std::to_string(diagnostics.valid_program_vertex_count)},
            {"invalid_program_vertex_count", std::to_string(diagnostics.invalid_program_vertex_count)},
            {"fixed_function_vertex_count", std::to_string(diagnostics.fixed_function_vertex_count)},
            {"fixed_function_transformed_vertex_count", std::to_string(diagnostics.fixed_function_transformed_vertex_count)},
            {"invalid_fixed_function_vertex_count", std::to_string(diagnostics.invalid_fixed_function_vertex_count)},
            {"position_output_vertex_count", std::to_string(diagnostics.position_output_vertex_count)},
            {"homogeneous_position_vertex_count", std::to_string(diagnostics.homogeneous_position_vertex_count)},
            {"implicit_position_w_vertex_count", std::to_string(diagnostics.implicit_position_w_vertex_count)},
            {"invalid_position_w_vertex_count", std::to_string(diagnostics.invalid_position_w_vertex_count)},
            {"non_finite_position_w_vertex_count", std::to_string(diagnostics.non_finite_position_w_vertex_count)},
            {"defaulted_position_w_vertex_count", std::to_string(diagnostics.defaulted_position_w_vertex_count)},
            {"position_output_mask_union", std::to_string(diagnostics.position_output_mask_union)},
            {"screen_space_position_vertex_count", std::to_string(diagnostics.screen_space_position_vertex_count)},
            {"viewport_mapped_vertex_count", std::to_string(diagnostics.viewport_mapped_vertex_count)},
            {"raw_position_fallback_vertex_count", std::to_string(diagnostics.raw_position_fallback_vertex_count)},
            {"near_zero_w_vertex_count", std::to_string(diagnostics.near_zero_w_vertex_count)},
            {"non_finite_position_vertex_count", std::to_string(diagnostics.non_finite_position_vertex_count)},
            {"ndc_outside_clip_vertex_count", std::to_string(diagnostics.ndc_outside_clip_vertex_count)},
            {"ndc_extreme_vertex_count", std::to_string(diagnostics.ndc_extreme_vertex_count)},
            {"diffuse_output_vertex_count", std::to_string(diagnostics.diffuse_output_vertex_count)},
            {"texture_output_vertex_count", std::to_string(diagnostics.texture_output_vertex_count)},
            {"input_min_x", diagnostics.input_position_bounds.valid ? json_float(diagnostics.input_position_bounds.min_x) : "null"},
            {"input_max_x", diagnostics.input_position_bounds.valid ? json_float(diagnostics.input_position_bounds.max_x) : "null"},
            {"input_min_y", diagnostics.input_position_bounds.valid ? json_float(diagnostics.input_position_bounds.min_y) : "null"},
            {"input_max_y", diagnostics.input_position_bounds.valid ? json_float(diagnostics.input_position_bounds.max_y) : "null"},
            {"input_min_z", diagnostics.input_z_bounds.valid ? json_float(diagnostics.input_z_bounds.minimum) : "null"},
            {"input_max_z", diagnostics.input_z_bounds.valid ? json_float(diagnostics.input_z_bounds.maximum) : "null"},
            {"input_min_w", diagnostics.input_w_bounds.valid ? json_float(diagnostics.input_w_bounds.minimum) : "null"},
            {"input_max_w", diagnostics.input_w_bounds.valid ? json_float(diagnostics.input_w_bounds.maximum) : "null"},
            {"clip_min_x", diagnostics.clip_position_bounds.valid ? json_float(diagnostics.clip_position_bounds.min_x) : "null"},
            {"clip_max_x", diagnostics.clip_position_bounds.valid ? json_float(diagnostics.clip_position_bounds.max_x) : "null"},
            {"clip_min_y", diagnostics.clip_position_bounds.valid ? json_float(diagnostics.clip_position_bounds.min_y) : "null"},
            {"clip_max_y", diagnostics.clip_position_bounds.valid ? json_float(diagnostics.clip_position_bounds.max_y) : "null"},
            {"program_output_min_z", diagnostics.program_output_z_bounds.valid ? json_float(diagnostics.program_output_z_bounds.minimum) : "null"},
            {"program_output_max_z", diagnostics.program_output_z_bounds.valid ? json_float(diagnostics.program_output_z_bounds.maximum) : "null"},
            {"program_output_min_w", diagnostics.program_output_w_bounds.valid ? json_float(diagnostics.program_output_w_bounds.minimum) : "null"},
            {"program_output_max_w", diagnostics.program_output_w_bounds.valid ? json_float(diagnostics.program_output_w_bounds.maximum) : "null"},
            {"ndc_min_x", diagnostics.ndc_position_bounds.valid ? json_float(diagnostics.ndc_position_bounds.min_x) : "null"},
            {"ndc_max_x", diagnostics.ndc_position_bounds.valid ? json_float(diagnostics.ndc_position_bounds.max_x) : "null"},
            {"ndc_min_y", diagnostics.ndc_position_bounds.valid ? json_float(diagnostics.ndc_position_bounds.min_y) : "null"},
            {"ndc_max_y", diagnostics.ndc_position_bounds.valid ? json_float(diagnostics.ndc_position_bounds.max_y) : "null"},
            {"output_pixel_min_x", final_pixel_bounds.valid ? json_float(final_pixel_bounds.min_x) : "null"},
            {"output_pixel_max_x", final_pixel_bounds.valid ? json_float(final_pixel_bounds.max_x) : "null"},
            {"output_pixel_min_y", final_pixel_bounds.valid ? json_float(final_pixel_bounds.min_y) : "null"},
            {"output_pixel_max_y", final_pixel_bounds.valid ? json_float(final_pixel_bounds.max_y) : "null"},
            {"indexed_program_output_pixel_min_x", indexed_program_pixel_bounds.valid ? json_float(indexed_program_pixel_bounds.min_x) : "null"},
            {"indexed_program_output_pixel_max_x", indexed_program_pixel_bounds.valid ? json_float(indexed_program_pixel_bounds.max_x) : "null"},
            {"indexed_program_output_pixel_min_y", indexed_program_pixel_bounds.valid ? json_float(indexed_program_pixel_bounds.min_y) : "null"},
            {"indexed_program_output_pixel_max_y", indexed_program_pixel_bounds.valid ? json_float(indexed_program_pixel_bounds.max_y) : "null"},
            {"indexed_program_output_span_x", indexed_program_pixel_bounds.valid ? json_float(indexed_program_span_x) : "null"},
            {"indexed_program_output_span_y", indexed_program_pixel_bounds.valid ? json_float(indexed_program_span_y) : "null"},
            {"indexed_program_width_coverage", indexed_program_pixel_bounds.valid && swapchain_extent_.width != 0u ? json_float(indexed_program_span_x / static_cast<float>(swapchain_extent_.width)) : "null"},
            {"indexed_program_height_coverage", indexed_program_pixel_bounds.valid && swapchain_extent_.height != 0u ? json_float(indexed_program_span_y / static_cast<float>(swapchain_extent_.height)) : "null"},
            {"viewport_scale_x", indexed_program_state != nullptr ? json_float(float_from_u32(indexed_program_state->transform_constants[58][0])) : "null"},
            {"viewport_scale_y", indexed_program_state != nullptr ? json_float(float_from_u32(indexed_program_state->transform_constants[58][1])) : "null"},
            {"viewport_scale_z", indexed_program_state != nullptr ? json_float(float_from_u32(indexed_program_state->transform_constants[58][2])) : "null"},
            {"viewport_scale_w", indexed_program_state != nullptr ? json_float(float_from_u32(indexed_program_state->transform_constants[58][3])) : "null"},
            {"viewport_offset_x", indexed_program_state != nullptr ? json_float(float_from_u32(indexed_program_state->transform_constants[59][0])) : "null"},
            {"viewport_offset_y", indexed_program_state != nullptr ? json_float(float_from_u32(indexed_program_state->transform_constants[59][1])) : "null"},
            {"viewport_offset_z", indexed_program_state != nullptr ? json_float(float_from_u32(indexed_program_state->transform_constants[59][2])) : "null"},
            {"viewport_offset_w", indexed_program_state != nullptr ? json_float(float_from_u32(indexed_program_state->transform_constants[59][3])) : "null"},
            {"viewport_constants_valid", json_bool(
                indexed_program_state != nullptr
                && std::isfinite(float_from_u32(indexed_program_state->transform_constants[58][0]))
                && std::isfinite(float_from_u32(indexed_program_state->transform_constants[58][1]))
                && std::abs(float_from_u32(indexed_program_state->transform_constants[58][0])) > 0.000001f
                && std::abs(float_from_u32(indexed_program_state->transform_constants[58][1])) > 0.000001f)},
            {"output_min_r", diagnostics.output_color_bounds_valid ? json_float(diagnostics.output_min_r) : "null"},
            {"output_max_r", diagnostics.output_color_bounds_valid ? json_float(diagnostics.output_max_r) : "null"},
            {"output_min_g", diagnostics.output_color_bounds_valid ? json_float(diagnostics.output_min_g) : "null"},
            {"output_max_g", diagnostics.output_color_bounds_valid ? json_float(diagnostics.output_max_g) : "null"},
            {"output_min_b", diagnostics.output_color_bounds_valid ? json_float(diagnostics.output_min_b) : "null"},
            {"output_max_b", diagnostics.output_color_bounds_valid ? json_float(diagnostics.output_max_b) : "null"},
            {"output_min_a", diagnostics.output_color_bounds_valid ? json_float(diagnostics.output_min_a) : "null"},
            {"output_max_a", diagnostics.output_color_bounds_valid ? json_float(diagnostics.output_max_a) : "null"},
            {"coordinate_collapse_suspected", json_bool(indexed_program_tiny_coverage_suspected)},
            {"indexed_program_tiny_coverage_suspected", json_bool(indexed_program_tiny_coverage_suspected)},
        });
    if (!emit_draw_details) {
        return;
    }
    for (const PresentedDrawTransformDiagnostics& draw :
         presented_draw_transform_diagnostics_) {
        const PresentedVertexTransformDiagnostics& transform = draw.transform;
        const size_t native_draw_index = first_draw + draw.presented_index;
        const NativeDraw* native_draw =
            native_draw_index < interpreted_stream_.draws.size()
            ? &interpreted_stream_.draws[native_draw_index]
            : nullptr;
        const NativeVertex* first_source_vertex = native_draw != nullptr
                && native_draw->vertex_count != 0u
                && native_draw->first_vertex
                    < interpreted_stream_.vertices.size()
            ? &interpreted_stream_.vertices[native_draw->first_vertex]
            : nullptr;
        const uint32_t texture_stage = native_draw != nullptr
            ? std::min<uint32_t>(native_draw->texture_stage, 3u)
            : 0u;
        const uint32_t texture_address_state = native_draw != nullptr
            ? native_draw->texture_addresses[texture_stage]
            : 0u;
        const uint32_t guest_address_u = texture_address_state & 7u;
        const uint32_t guest_address_v =
            (texture_address_state >> 8u) & 7u;
        const bool sampler_address_match = native_draw == nullptr
            || !native_draw->texture_enabled
            || ((guest_address_u >= 1u && guest_address_u <= 5u)
                && (guest_address_v >= 1u && guest_address_v <= 5u));
        const uint32_t texture_mode = native_draw != nullptr
            ? (native_draw->shader_stage_program
                >> (texture_stage * 5u)) & 0x1Fu
            : 0u;
        const uint32_t texture_color_format = native_draw != nullptr
            ? (native_draw->texture_formats[texture_stage] >> 8u) & 0xFFu
            : 0u;
        const bool opaque_x8_alpha_forced =
            texture_color_format == 0x07u
            || texture_color_format == 0x1Eu;
        const bool uncompressed_texture_unswizzled =
            texture_color_format == 0x05u
            || texture_color_format == 0x06u
            || texture_color_format == 0x07u;
        const bool fixed_composite_valid = native_draw != nullptr
            && (native_draw->transform_execution_mode & 3u) == 0u
            && fixed_function_composite_matrix_valid(*native_draw);
        const std::string host_transform_path = native_draw == nullptr
            ? "unavailable"
            : (native_draw->transform_execution_mode & 3u) == 2u
                ? "programmable"
                : fixed_composite_valid
                    ? "fixed_composite"
                    : "filtered";
        const std::vector<uint32_t> referenced_constants =
            native_draw != nullptr
            ? referenced_vertex_program_constants(*native_draw)
            : std::vector<uint32_t>{};
        uint32_t zero_referenced_constant_count = 0;
        uint32_t non_finite_referenced_constant_count = 0;
        if (native_draw != nullptr) {
            for (const uint32_t constant_index : referenced_constants) {
                const auto& bits =
                    native_draw->transform_constants[constant_index];
                bool all_zero = true;
                bool all_finite = true;
                for (const uint32_t value : bits) {
                    all_zero &= value == 0u;
                    all_finite &= std::isfinite(float_from_u32(value));
                }
                zero_referenced_constant_count += all_zero ? 1u : 0u;
                non_finite_referenced_constant_count += all_finite ? 0u : 1u;
            }
        }
        uint32_t enabled_texture_stage_count = 0u;
        bool host_texture_stage_coverage_complete = true;
        const RecoveredTextureResource* recovered_texture = nullptr;
        uint32_t texture_producer_draw_count = 0u;
        uint32_t texture_raw_producer_draw_count = 0u;
        uint32_t texture_first_producer_address = 0u;
        uint32_t texture_width = 0u;
        uint32_t texture_height = 0u;
        if (native_draw != nullptr) {
            enabled_texture_stage_count = static_cast<uint32_t>(
                std::count_if(
                    native_draw->texture_controls.begin(),
                    native_draw->texture_controls.end(),
                    [](uint32_t control) {
                        return (control & (1u << 30u)) != 0u;
                    }));
            for (uint32_t stage = 0u;
                 stage < native_draw->texture_controls.size();
                 ++stage) {
                const uint32_t stage_mode =
                    (native_draw->shader_stage_program >> (stage * 5u))
                    & 0x1Fu;
                if ((native_draw->texture_controls[stage] & (1u << 30u)) == 0u
                    || stage_mode == 0u) {
                    continue;
                }
                const bool mode_supported =
                    stage_mode == 1u || stage_mode == 3u;
                const bool resource_supported = std::any_of(
                    host_textures_.begin(),
                    host_textures_.end(),
                    [&](const HostTexture& texture) {
                        return host_texture_matches_stage(
                            texture, *native_draw, stage);
                    });
                host_texture_stage_coverage_complete &=
                    mode_supported && resource_supported;
            }
            if (native_draw->texture_enabled) {
                const auto texture_extent = nv2a_texture_extent(
                    native_draw->texture_formats[texture_stage],
                    native_draw->texture_image_rects[texture_stage]);
                texture_width = texture_extent.first;
                texture_height = texture_extent.second;
                const auto texture_match = std::find_if(
                    recovered_source_.textures.begin(),
                    recovered_source_.textures.end(),
                    [&](const RecoveredTextureResource& resource) {
                        return resource.address
                                == native_draw->texture_address
                            && resource.width == texture_width
                            && resource.height == texture_height
                            && nv2a_texture_format_matches(
                                resource.format,
                                native_draw->texture_formats[texture_stage]);
                    });
                if (texture_match != recovered_source_.textures.end()) {
                    recovered_texture = &*texture_match;
                }
                for (size_t producer_index = 0u;
                     producer_index < native_draw_index;
                     ++producer_index) {
                    const uint32_t producer_address =
                        interpreted_stream_.draws[producer_index]
                            .surface_color_offset;
                    const bool raw_match = producer_address
                        == native_draw->texture_address;
                    const bool canonical_match =
                        nv2a_canonical_resource_address(
                            producer_address)
                            == nv2a_canonical_resource_address(
                                native_draw->texture_address);
                    texture_raw_producer_draw_count += raw_match ? 1u : 0u;
                    texture_producer_draw_count +=
                        canonical_match ? 1u : 0u;
                    if (canonical_match
                        && texture_first_producer_address == 0u) {
                        texture_first_producer_address = producer_address;
                    }
                }
            }
        }
        const bool texture_payload_all_zero =
            recovered_texture != nullptr
            && !recovered_texture->payload.empty()
            && std::all_of(
                recovered_texture->payload.begin(),
                recovered_texture->payload.end(),
                [](uint8_t value) { return value == 0u; });
        const bool host_render_target_feedback_supported =
            native_draw != nullptr
            && std::any_of(
                host_feedback_specs.begin(),
                host_feedback_specs.end(),
                [&](const RenderTargetFeedbackSpec& spec) {
                    return nv2a_canonical_resource_address(spec.address)
                            == nv2a_canonical_resource_address(
                                native_draw->texture_address)
                        && spec.width == texture_width
                        && spec.height == texture_height
                        && nv2a_texture_format_matches(
                            spec.format,
                            native_draw->texture_formats[texture_stage]);
                });
        const bool host_offscreen_target_replay_supported =
            native_draw != nullptr
            && std::any_of(
                host_feedback_specs.begin(),
                host_feedback_specs.end(),
                [&](const RenderTargetFeedbackSpec& spec) {
                    return spec.offscreen_produced
                        && nv2a_canonical_resource_address(spec.address)
                            == nv2a_canonical_resource_address(
                                native_draw->texture_address);
                });
        const bool host_offscreen_target_replay_applied =
            native_draw != nullptr
            && std::any_of(
                offscreen_render_targets_.begin(),
                offscreen_render_targets_.end(),
                [&](const OffscreenRenderTarget& target) {
                    return nv2a_canonical_resource_address(
                        target.spec.address)
                        == nv2a_canonical_resource_address(
                            native_draw->texture_address);
                });
        const float viewport_depth_scale = native_draw != nullptr
            ? float_from_u32(native_draw->transform_constants[58][2])
            : 0.0f;
        const float viewport_depth_offset = native_draw != nullptr
            ? float_from_u32(native_draw->transform_constants[59][2])
            : 0.0f;
        const bool normalized_depth_valid =
            transform.program_output_z_bounds.valid
            && std::isfinite(viewport_depth_scale)
            && std::abs(viewport_depth_scale) > 0.000001f;
        log_.emit(
            "nv2a_presented_transform_diagnostics",
            {
                {"presented_index", std::to_string(draw.presented_index)},
                {"primitive", std::to_string(draw.primitive)},
                {"vertex_count", std::to_string(draw.vertex_count)},
                {"indexed_array", json_bool(draw.indexed_array)},
                {"transform_execution_mode", std::to_string(draw.transform_execution_mode)},
                {"host_transform_path", json_string(host_transform_path)},
                {"fixed_composite_matrix_valid", json_bool(fixed_composite_valid)},
                {"fixed_function_transformed_vertex_count", std::to_string(transform.fixed_function_transformed_vertex_count)},
                {"invalid_fixed_function_vertex_count", std::to_string(transform.invalid_fixed_function_vertex_count)},
                {"texture_enabled", native_draw != nullptr ? json_bool(native_draw->texture_enabled) : "null"},
                {"texture_stage", std::to_string(texture_stage)},
                {"texture_address", native_draw != nullptr ? std::to_string(native_draw->texture_address) : "null"},
                {"texture_address_state", native_draw != nullptr ? std::to_string(texture_address_state) : "null"},
                {"guest_address_u", native_draw != nullptr ? json_string(nv2a_sampler_address_name(guest_address_u)) : "null"},
                {"guest_address_v", native_draw != nullptr ? json_string(nv2a_sampler_address_name(guest_address_v)) : "null"},
                {"host_address_u", native_draw != nullptr ? json_string(nv2a_sampler_address_name(guest_address_u)) : "null"},
                {"host_address_v", native_draw != nullptr ? json_string(nv2a_sampler_address_name(guest_address_v)) : "null"},
                {"sampler_address_match", json_bool(sampler_address_match)},
                {"texture_mode", std::to_string(texture_mode)},
                {"texture_projective", json_bool(texture_mode == 1u)},
                {"texture_alpha_kill", native_draw != nullptr ? json_bool((native_draw->texture_controls[texture_stage] & (1u << 2u)) != 0u) : "null"},
                {"enabled_texture_stage_count", native_draw != nullptr ? std::to_string(enabled_texture_stage_count) : "null"},
                {"host_texture_stage_coverage_complete", native_draw != nullptr ? json_bool(host_texture_stage_coverage_complete) : "null"},
                {"shader_stage_program", native_draw != nullptr ? std::to_string(native_draw->shader_stage_program) : "null"},
                {"texture_offsets", native_draw != nullptr ? json_u32_array(native_draw->texture_offsets) : "null"},
                {"texture_addresses", native_draw != nullptr ? json_u32_array(native_draw->texture_addresses) : "null"},
                {"texture_formats", native_draw != nullptr ? json_u32_array(native_draw->texture_formats) : "null"},
                {"texture_image_rects", native_draw != nullptr ? json_u32_array(native_draw->texture_image_rects) : "null"},
                {"texture_controls", native_draw != nullptr ? json_u32_array(native_draw->texture_controls) : "null"},
                {"texture_filters", native_draw != nullptr ? json_u32_array(native_draw->texture_filters) : "null"},
                {"texture_resource_matched", native_draw != nullptr ? json_bool(recovered_texture != nullptr) : "null"},
                {"texture_resource_payload_bytes", recovered_texture != nullptr ? std::to_string(recovered_texture->payload.size()) : "null"},
                {"texture_resource_payload_all_zero", recovered_texture != nullptr ? json_bool(texture_payload_all_zero) : "null"},
                {"host_render_target_feedback_supported", json_bool(host_render_target_feedback_supported)},
                {"texture_producer_draw_count", native_draw != nullptr ? std::to_string(texture_producer_draw_count) : "null"},
                {"texture_raw_producer_draw_count", native_draw != nullptr ? std::to_string(texture_raw_producer_draw_count) : "null"},
                {"texture_first_producer_address", native_draw != nullptr ? std::to_string(texture_first_producer_address) : "null"},
                {"texture_address_alias_applied", native_draw != nullptr ? json_bool(texture_producer_draw_count > texture_raw_producer_draw_count) : "null"},
                {"host_offscreen_target_replay_supported", native_draw != nullptr ? json_bool(host_offscreen_target_replay_supported) : "null"},
                {"host_offscreen_target_replay_applied", native_draw != nullptr ? json_bool(host_offscreen_target_replay_applied) : "null"},
                {"opaque_x8_alpha_forced", json_bool(opaque_x8_alpha_forced)},
                {"uncompressed_texture_unswizzled", json_bool(uncompressed_texture_unswizzled)},
                {"blend_enable", native_draw != nullptr ? std::to_string(native_draw->blend_enable) : "null"},
                {"blend_source_factor", native_draw != nullptr ? std::to_string(native_draw->blend_source_factor) : "null"},
                {"blend_destination_factor", native_draw != nullptr ? std::to_string(native_draw->blend_destination_factor) : "null"},
                {"blend_equation", native_draw != nullptr ? std::to_string(native_draw->blend_equation) : "null"},
                {"color_mask", native_draw != nullptr ? std::to_string(native_draw->color_mask) : "null"},
                {"depth_test_enable", native_draw != nullptr ? std::to_string(native_draw->depth_test_enable) : "null"},
                {"depth_function", native_draw != nullptr ? std::to_string(native_draw->depth_function) : "null"},
                {"depth_write_enable", native_draw != nullptr ? std::to_string(native_draw->depth_write_enable) : "null"},
                {"viewport_depth_scale", native_draw != nullptr ? json_float(viewport_depth_scale) : "null"},
                {"viewport_depth_offset", native_draw != nullptr ? json_float(viewport_depth_offset) : "null"},
                {"normalized_depth_min", normalized_depth_valid ? json_float((transform.program_output_z_bounds.minimum - viewport_depth_offset) / viewport_depth_scale) : "null"},
                {"normalized_depth_max", normalized_depth_valid ? json_float((transform.program_output_z_bounds.maximum - viewport_depth_offset) / viewport_depth_scale) : "null"},
                {"surface_color_offset", native_draw != nullptr ? std::to_string(native_draw->surface_color_offset) : "null"},
                {"surface_pitch", native_draw != nullptr ? std::to_string(native_draw->surface_pitch) : "null"},
                {"surface_format", native_draw != nullptr ? std::to_string(native_draw->surface_format) : "null"},
                {"surface_clip_horizontal", native_draw != nullptr ? std::to_string(native_draw->surface_clip_horizontal) : "null"},
                {"surface_clip_vertical", native_draw != nullptr ? std::to_string(native_draw->surface_clip_vertical) : "null"},
                {"polygon_offset_fill_enable", native_draw != nullptr ? std::to_string(native_draw->polygon_offset_fill_enable) : "null"},
                {"polygon_offset_scale_factor", native_draw != nullptr ? json_float(float_from_u32(native_draw->polygon_offset_scale_factor)) : "null"},
                {"polygon_offset_bias", native_draw != nullptr ? json_float(float_from_u32(native_draw->polygon_offset_bias)) : "null"},
                {"combiner_stage_count", native_draw != nullptr ? std::to_string(native_draw->combiner_control & 0xFFu) : "null"},
                {"host_register_combiner_applied", native_draw != nullptr ? json_bool(true) : "null"},
                {"fixed_function_texture_combiner_recovered", native_draw != nullptr ? json_bool(default_fixed_function_texture_combiner_recovery_required(*native_draw)) : "null"},
                {"combiner_control", native_draw != nullptr ? std::to_string(native_draw->combiner_control) : "null"},
                {"combiner_color_inputs", native_draw != nullptr ? json_u32_array(native_draw->combiner_color_inputs) : "null"},
                {"combiner_color_outputs", native_draw != nullptr ? json_u32_array(native_draw->combiner_color_outputs) : "null"},
                {"combiner_alpha_inputs", native_draw != nullptr ? json_u32_array(native_draw->combiner_alpha_inputs) : "null"},
                {"combiner_alpha_outputs", native_draw != nullptr ? json_u32_array(native_draw->combiner_alpha_outputs) : "null"},
                {"combiner_factors0", native_draw != nullptr ? json_u32_array(native_draw->combiner_factors0) : "null"},
                {"combiner_factors1", native_draw != nullptr ? json_u32_array(native_draw->combiner_factors1) : "null"},
                {"combiner_color_input0", native_draw != nullptr ? std::to_string(native_draw->combiner_color_inputs[0]) : "null"},
                {"combiner_color_output0", native_draw != nullptr ? std::to_string(native_draw->combiner_color_outputs[0]) : "null"},
                {"combiner_alpha_input0", native_draw != nullptr ? std::to_string(native_draw->combiner_alpha_inputs[0]) : "null"},
                {"combiner_alpha_output0", native_draw != nullptr ? std::to_string(native_draw->combiner_alpha_outputs[0]) : "null"},
                {"combiner_factor0_0", native_draw != nullptr ? std::to_string(native_draw->combiner_factors0[0]) : "null"},
                {"combiner_factor1_0", native_draw != nullptr ? std::to_string(native_draw->combiner_factors1[0]) : "null"},
                {"final_combiner_inputs0", native_draw != nullptr ? std::to_string(native_draw->final_combiner_inputs0) : "null"},
                {"final_combiner_inputs1", native_draw != nullptr ? std::to_string(native_draw->final_combiner_inputs1) : "null"},
                {"final_combiner_factor0", native_draw != nullptr ? std::to_string(native_draw->final_combiner_factors[0]) : "null"},
                {"final_combiner_factor1", native_draw != nullptr ? std::to_string(native_draw->final_combiner_factors[1]) : "null"},
                {"fog_enable", native_draw != nullptr ? std::to_string(native_draw->fog_enable) : "null"},
                {"fog_mode", native_draw != nullptr ? std::to_string(native_draw->fog_mode) : "null"},
                {"fog_generation_mode", native_draw != nullptr ? std::to_string(native_draw->fog_generation_mode) : "null"},
                {"fog_color", native_draw != nullptr ? std::to_string(native_draw->fog_color) : "null"},
                {"fog_params", native_draw != nullptr ? json_u32_array(native_draw->fog_params) : "null"},
                {"host_fog_factor_applied", native_draw != nullptr ? json_bool(native_draw->fog_enable == 0u || native_draw->fog_params[0] != 0u || native_draw->fog_params[1] != 0u) : "null"},
                {"cull_face_enable", native_draw != nullptr ? std::to_string(native_draw->cull_face_enable) : "null"},
                {"cull_face", native_draw != nullptr ? std::to_string(native_draw->cull_face) : "null"},
                {"front_face", native_draw != nullptr ? std::to_string(native_draw->front_face) : "null"},
                {"specular_enable", native_draw != nullptr ? std::to_string(native_draw->specular_enable) : "null"},
                {"alpha_test_enable", native_draw != nullptr ? std::to_string(native_draw->alpha_test_enable) : "null"},
                {"alpha_function", native_draw != nullptr ? std::to_string(native_draw->alpha_function) : "null"},
                {"alpha_reference", native_draw != nullptr ? std::to_string(native_draw->alpha_reference & 0xFFu) : "null"},
                {"host_alpha_test_applied", native_draw != nullptr ? json_bool(true) : "null"},
                {"vertex_format_0", native_draw != nullptr ? std::to_string(native_draw->vertex_formats[0]) : "null"},
                {"vertex_format_3", native_draw != nullptr ? std::to_string(native_draw->vertex_formats[3]) : "null"},
                {"vertex_format_9", native_draw != nullptr ? std::to_string(native_draw->vertex_formats[9]) : "null"},
                {"vertex_offset_0", native_draw != nullptr ? std::to_string(native_draw->vertex_offsets[0]) : "null"},
                {"vertex_offset_3", native_draw != nullptr ? std::to_string(native_draw->vertex_offsets[3]) : "null"},
                {"vertex_offset_9", native_draw != nullptr ? std::to_string(native_draw->vertex_offsets[9]) : "null"},
                {"transform_program_start", native_draw != nullptr ? std::to_string(native_draw->transform_program_start) : "null"},
                {"first_input_diffuse_r", first_source_vertex != nullptr
                    ? json_float(first_source_vertex->program_inputs_valid
                        ? first_source_vertex->program_inputs[3][0]
                        : first_source_vertex->r)
                    : "null"},
                {"first_input_diffuse_g", first_source_vertex != nullptr
                    ? json_float(first_source_vertex->program_inputs_valid
                        ? first_source_vertex->program_inputs[3][1]
                        : first_source_vertex->g)
                    : "null"},
                {"first_input_diffuse_b", first_source_vertex != nullptr
                    ? json_float(first_source_vertex->program_inputs_valid
                        ? first_source_vertex->program_inputs[3][2]
                        : first_source_vertex->b)
                    : "null"},
                {"first_input_diffuse_a", first_source_vertex != nullptr
                    ? json_float(first_source_vertex->program_inputs_valid
                        ? first_source_vertex->program_inputs[3][3]
                        : first_source_vertex->a)
                    : "null"},
                {"valid_program_vertex_count", std::to_string(transform.valid_program_vertex_count)},
                {"invalid_program_vertex_count", std::to_string(transform.invalid_program_vertex_count)},
                {"position_output_vertex_count", std::to_string(transform.position_output_vertex_count)},
                {"homogeneous_position_vertex_count", std::to_string(transform.homogeneous_position_vertex_count)},
                {"implicit_position_w_vertex_count", std::to_string(transform.implicit_position_w_vertex_count)},
                {"invalid_position_w_vertex_count", std::to_string(transform.invalid_position_w_vertex_count)},
                {"non_finite_position_w_vertex_count", std::to_string(transform.non_finite_position_w_vertex_count)},
                {"defaulted_position_w_vertex_count", std::to_string(transform.defaulted_position_w_vertex_count)},
                {"position_output_mask_union", std::to_string(transform.position_output_mask_union)},
                {"viewport_mapped_vertex_count", std::to_string(transform.viewport_mapped_vertex_count)},
                {"raw_position_fallback_vertex_count", std::to_string(transform.raw_position_fallback_vertex_count)},
                {"near_zero_w_vertex_count", std::to_string(transform.near_zero_w_vertex_count)},
                {"ndc_outside_clip_vertex_count", std::to_string(transform.ndc_outside_clip_vertex_count)},
                {"ndc_extreme_vertex_count", std::to_string(transform.ndc_extreme_vertex_count)},
                {"input_min_x", transform.input_position_bounds.valid ? json_float(transform.input_position_bounds.min_x) : "null"},
                {"input_max_x", transform.input_position_bounds.valid ? json_float(transform.input_position_bounds.max_x) : "null"},
                {"input_min_y", transform.input_position_bounds.valid ? json_float(transform.input_position_bounds.min_y) : "null"},
                {"input_max_y", transform.input_position_bounds.valid ? json_float(transform.input_position_bounds.max_y) : "null"},
                {"input_min_z", transform.input_z_bounds.valid ? json_float(transform.input_z_bounds.minimum) : "null"},
                {"input_max_z", transform.input_z_bounds.valid ? json_float(transform.input_z_bounds.maximum) : "null"},
                {"input_min_w", transform.input_w_bounds.valid ? json_float(transform.input_w_bounds.minimum) : "null"},
                {"input_max_w", transform.input_w_bounds.valid ? json_float(transform.input_w_bounds.maximum) : "null"},
                {"program_output_min_z", transform.program_output_z_bounds.valid ? json_float(transform.program_output_z_bounds.minimum) : "null"},
                {"program_output_max_z", transform.program_output_z_bounds.valid ? json_float(transform.program_output_z_bounds.maximum) : "null"},
                {"program_output_min_w", transform.program_output_w_bounds.valid ? json_float(transform.program_output_w_bounds.minimum) : "null"},
                {"program_output_max_w", transform.program_output_w_bounds.valid ? json_float(transform.program_output_w_bounds.maximum) : "null"},
                {"ndc_min_x", transform.ndc_position_bounds.valid ? json_float(transform.ndc_position_bounds.min_x) : "null"},
                {"ndc_max_x", transform.ndc_position_bounds.valid ? json_float(transform.ndc_position_bounds.max_x) : "null"},
                {"ndc_min_y", transform.ndc_position_bounds.valid ? json_float(transform.ndc_position_bounds.min_y) : "null"},
                {"ndc_max_y", transform.ndc_position_bounds.valid ? json_float(transform.ndc_position_bounds.max_y) : "null"},
                {"output_pixel_min_x", draw.final_pixel_bounds.valid ? json_float(draw.final_pixel_bounds.min_x) : "null"},
                {"output_pixel_max_x", draw.final_pixel_bounds.valid ? json_float(draw.final_pixel_bounds.max_x) : "null"},
                {"output_pixel_min_y", draw.final_pixel_bounds.valid ? json_float(draw.final_pixel_bounds.min_y) : "null"},
                {"output_pixel_max_y", draw.final_pixel_bounds.valid ? json_float(draw.final_pixel_bounds.max_y) : "null"},
                {"texture_min_u", draw.final_u_bounds.valid ? json_float(draw.final_u_bounds.minimum) : "null"},
                {"texture_max_u", draw.final_u_bounds.valid ? json_float(draw.final_u_bounds.maximum) : "null"},
                {"texture_min_v", draw.final_v_bounds.valid ? json_float(draw.final_v_bounds.minimum) : "null"},
                {"texture_max_v", draw.final_v_bounds.valid ? json_float(draw.final_v_bounds.maximum) : "null"},
                {"texture_min_q", draw.final_q_bounds.valid ? json_float(draw.final_q_bounds.minimum) : "null"},
                {"texture_max_q", draw.final_q_bounds.valid ? json_float(draw.final_q_bounds.maximum) : "null"},
                {"texture_stage1_min_x", draw.final_texture_coordinate_bounds[1][0].valid ? json_float(draw.final_texture_coordinate_bounds[1][0].minimum) : "null"},
                {"texture_stage1_max_x", draw.final_texture_coordinate_bounds[1][0].valid ? json_float(draw.final_texture_coordinate_bounds[1][0].maximum) : "null"},
                {"texture_stage1_min_y", draw.final_texture_coordinate_bounds[1][1].valid ? json_float(draw.final_texture_coordinate_bounds[1][1].minimum) : "null"},
                {"texture_stage1_max_y", draw.final_texture_coordinate_bounds[1][1].valid ? json_float(draw.final_texture_coordinate_bounds[1][1].maximum) : "null"},
                {"texture_stage1_min_z", draw.final_texture_coordinate_bounds[1][2].valid ? json_float(draw.final_texture_coordinate_bounds[1][2].minimum) : "null"},
                {"texture_stage1_max_z", draw.final_texture_coordinate_bounds[1][2].valid ? json_float(draw.final_texture_coordinate_bounds[1][2].maximum) : "null"},
                {"texture_stage1_min_w", draw.final_texture_coordinate_bounds[1][3].valid ? json_float(draw.final_texture_coordinate_bounds[1][3].minimum) : "null"},
                {"texture_stage1_max_w", draw.final_texture_coordinate_bounds[1][3].valid ? json_float(draw.final_texture_coordinate_bounds[1][3].maximum) : "null"},
                {"fog_factor_min", draw.final_fog_bounds.valid ? json_float(draw.final_fog_bounds.minimum) : "null"},
                {"fog_factor_max", draw.final_fog_bounds.valid ? json_float(draw.final_fog_bounds.maximum) : "null"},
                {"positive_area_triangle_count", std::to_string(draw.positive_area_triangle_count)},
                {"negative_area_triangle_count", std::to_string(draw.negative_area_triangle_count)},
                {"degenerate_triangle_count", std::to_string(draw.degenerate_triangle_count)},
                {"vertex_indices", native_draw != nullptr && (native_draw->transform_execution_mode & 3u) == 0u ? json_u32_vector(native_draw->vertex_indices) : "null"},
                {"output_min_r", transform.output_color_bounds_valid ? json_float(transform.output_min_r) : "null"},
                {"output_max_r", transform.output_color_bounds_valid ? json_float(transform.output_max_r) : "null"},
                {"output_min_g", transform.output_color_bounds_valid ? json_float(transform.output_min_g) : "null"},
                {"output_max_g", transform.output_color_bounds_valid ? json_float(transform.output_max_g) : "null"},
                {"output_min_b", transform.output_color_bounds_valid ? json_float(transform.output_min_b) : "null"},
                {"output_max_b", transform.output_color_bounds_valid ? json_float(transform.output_max_b) : "null"},
                {"output_min_a", transform.output_color_bounds_valid ? json_float(transform.output_min_a) : "null"},
                {"output_max_a", transform.output_color_bounds_valid ? json_float(transform.output_max_a) : "null"},
                {"referenced_constant_count", std::to_string(referenced_constants.size())},
                {"zero_referenced_constant_count", std::to_string(zero_referenced_constant_count)},
                {"non_finite_referenced_constant_count", std::to_string(non_finite_referenced_constant_count)},
            });
        for (size_t vertex_index = 0u;
             vertex_index < draw.fixed_function_vertices.size();
             ++vertex_index) {
            const auto& vertex = draw.fixed_function_vertices[vertex_index];
            const bool source_index_available = native_draw != nullptr
                && vertex_index < native_draw->vertex_indices.size();
            log_.emit(
                "nv2a_fixed_function_vertex_diagnostics",
                {
                    {"presented_index", std::to_string(draw.presented_index)},
                    {"vertex_index", std::to_string(vertex_index)},
                    {"source_index", source_index_available ? std::to_string(native_draw->vertex_indices[vertex_index]) : "null"},
                    {"x", json_float(vertex[0])},
                    {"y", json_float(vertex[1])},
                    {"z", json_float(vertex[2])},
                    {"w", json_float(vertex[3])},
                    {"u", json_float(vertex[4])},
                    {"v", json_float(vertex[5])},
                });
        }
        if (native_draw == nullptr) {
            continue;
        }
        for (const uint32_t constant_index : referenced_constants) {
            const auto& bits = native_draw->transform_constants[constant_index];
            log_.emit(
                "nv2a_presented_transform_constant",
                {
                    {"presented_index", std::to_string(draw.presented_index)},
                    {"constant_index", std::to_string(constant_index)},
                    {"x_bits", std::to_string(bits[0])},
                    {"y_bits", std::to_string(bits[1])},
                    {"z_bits", std::to_string(bits[2])},
                    {"w_bits", std::to_string(bits[3])},
                    {"x", json_float(float_from_u32(bits[0]))},
                    {"y", json_float(float_from_u32(bits[1]))},
                    {"z", json_float(float_from_u32(bits[2]))},
                    {"w", json_float(float_from_u32(bits[3]))},
                });
        }
    }
}

void VulkanPresenter::emit_presented_render_state_diagnostics() {
    const size_t first_draw = std::min<size_t>(
        interpreted_stream_.presented_draw_begin,
        interpreted_stream_.draws.size());
    const size_t end_draw = std::min<size_t>(
        first_draw + interpreted_stream_.presented_draw_count,
        interpreted_stream_.draws.size());
    uint32_t routed_presented_draw_count = 0u;
    uint32_t different_surface_draw_count = 0u;
    uint32_t subsurface_viewport_draw_count = 0u;
    uint32_t presented_surface_subsurface_viewport_draw_count = 0u;
    for (size_t draw_index = first_draw; draw_index < end_draw; ++draw_index) {
        const NativeDraw& draw = interpreted_stream_.draws[draw_index];
        const bool subsurface_viewport =
            draw_surface_clip_is_subsurface_viewport(draw);
        const bool selected_surface = presented_surface_color_offset_ != 0u
            && draw.surface_color_offset == presented_surface_color_offset_;
        const bool routed_presented = draw_targets_presented_surface(draw);
        routed_presented_draw_count += routed_presented ? 1u : 0u;
        different_surface_draw_count +=
            !routed_presented && !subsurface_viewport ? 1u : 0u;
        subsurface_viewport_draw_count += subsurface_viewport ? 1u : 0u;
        presented_surface_subsurface_viewport_draw_count +=
            subsurface_viewport && selected_surface ? 1u : 0u;
        if (!options_.analyze_render_stream_only || !subsurface_viewport) {
            continue;
        }
        const uint32_t clip_x = draw.surface_clip_horizontal & 0xFFFFu;
        const uint32_t clip_width = draw.surface_clip_horizontal >> 16u;
        const uint32_t clip_y = draw.surface_clip_vertical & 0xFFFFu;
        const uint32_t clip_height = draw.surface_clip_vertical >> 16u;
        log_.emit(
            "nv2a_subsurface_viewport_draw",
            {
                {"presented_index", std::to_string(draw_index - first_draw)},
                {"surface_color_offset", std::to_string(draw.surface_color_offset)},
                {"selected_surface", json_bool(selected_surface)},
                {"clip_x", std::to_string(clip_x)},
                {"clip_y", std::to_string(clip_y)},
                {"clip_width", std::to_string(clip_width)},
                {"clip_height", std::to_string(clip_height)},
                {"viewport_scale_x", json_float(float_from_u32(draw.transform_constants[58][0]))},
                {"viewport_scale_y", json_float(float_from_u32(draw.transform_constants[58][1]))},
                {"viewport_offset_x", json_float(float_from_u32(draw.transform_constants[59][0]))},
                {"viewport_offset_y", json_float(float_from_u32(draw.transform_constants[59][1]))},
                {"primitive", std::to_string(draw.primitive)},
                {"vertex_count", std::to_string(draw.vertex_count)},
                {"indexed_array", json_bool(draw.indexed_array)},
                {"texture_enabled", json_bool(draw.texture_enabled)},
                {"texture_address", std::to_string(draw.texture_address)},
            });
    }
    log_.emit(
        "nv2a_draw_surface_routing_diagnostics",
        {
            {"source_presented_draw_count", std::to_string(end_draw - first_draw)},
            {"presented_surface_color_offset", std::to_string(presented_surface_color_offset_)},
            {"routed_presented_draw_count", std::to_string(routed_presented_draw_count)},
            {"different_surface_draw_count", std::to_string(different_surface_draw_count)},
            {"subsurface_viewport_draw_count", std::to_string(subsurface_viewport_draw_count)},
            {"presented_surface_subsurface_viewport_draw_count", std::to_string(presented_surface_subsurface_viewport_draw_count)},
        });
    uint32_t textured_draw_count = 0u;
    uint32_t texture_address_state_draw_count = 0u;
    uint32_t out_of_unit_texture_coordinate_draw_count = 0u;
    uint32_t repeat_address_draw_count = 0u;
    uint32_t mirrored_repeat_address_draw_count = 0u;
    uint32_t sampler_address_mismatch_draw_count = 0u;
    uint32_t projective_texture_draw_count = 0u;
    uint32_t non_unit_projective_q_draw_count = 0u;
    uint32_t alpha_blend_enabled_draw_count = 0u;
    uint32_t alpha_test_enabled_draw_count = 0u;
    uint32_t alpha_test_applied_draw_count = 0u;
    uint32_t alpha_test_state_mismatch_draw_count = 0u;
    uint32_t texture_alpha_kill_enabled_draw_count = 0u;
    uint32_t texture_alpha_kill_applied_draw_count = 0u;
    uint32_t opaque_x8_texture_draw_count = 0u;
    uint32_t opaque_x8_alpha_forced_draw_count = 0u;
    uint32_t uncompressed_swizzled_texture_draw_count = 0u;
    uint32_t uncompressed_texture_unswizzled_draw_count = 0u;
    uint32_t register_combiner_programmed_draw_count = 0u;
    uint32_t register_combiner_applied_draw_count = 0u;
    uint32_t fixed_function_texture_combiner_recovered_draw_count = 0u;
    uint32_t register_combiner_state_mismatch_draw_count = 0u;
    uint32_t fog_enabled_draw_count = 0u;
    uint32_t fog_factor_applied_draw_count = 0u;
    uint32_t fog_state_mismatch_draw_count = 0u;
    uint32_t multiple_texture_stage_draw_count = 0u;
    uint32_t texture_stage_coverage_mismatch_draw_count = 0u;
    uint32_t cubemap_texture_stage_draw_count = 0u;
    uint32_t cubemap_texture_stage_coverage_mismatch_draw_count = 0u;
    uint32_t zero_payload_textured_draw_count = 0u;
    uint32_t unproduced_zero_payload_texture_draw_count = 0u;
    uint32_t aliased_gpu_produced_texture_draw_count = 0u;
    uint32_t offscreen_render_target_replay_draw_count = 0u;
    uint32_t render_target_feedback_draw_count = 0u;
    uint32_t fixed_function_draw_count = 0u;
    uint32_t fixed_function_transformed_draw_count = 0u;
    uint32_t fixed_function_raw_fallback_draw_count = 0u;
    uint32_t fixed_function_filtered_draw_count = 0u;
    uint64_t fixed_function_invalid_vertex_count = 0u;
    const std::vector<RenderTargetFeedbackSpec>& feedback_specs =
        presented_render_target_feedback_specs();
    for (const PresentedDrawTransformDiagnostics& diagnostics :
         presented_draw_transform_diagnostics_) {
        const size_t native_draw_index = first_draw
            + diagnostics.presented_index;
        if (native_draw_index >= interpreted_stream_.draws.size()) {
            continue;
        }
        const NativeDraw& draw = interpreted_stream_.draws[native_draw_index];
        const uint32_t stage = std::min<uint32_t>(draw.texture_stage, 3u);
        const bool combiner_programmed =
            (draw.combiner_control & 0xFFu) != 0u;
        register_combiner_programmed_draw_count +=
            combiner_programmed ? 1u : 0u;
        register_combiner_applied_draw_count +=
            combiner_programmed ? 1u : 0u;
        fixed_function_texture_combiner_recovered_draw_count +=
            default_fixed_function_texture_combiner_recovery_required(draw)
            ? 1u : 0u;
        const bool fog_enabled = draw.fog_enable != 0u;
        const bool fog_parameters_available = draw.fog_params[0] != 0u
            || draw.fog_params[1] != 0u;
        fog_enabled_draw_count += fog_enabled ? 1u : 0u;
        fog_factor_applied_draw_count +=
            fog_enabled && fog_parameters_available ? 1u : 0u;
        fog_state_mismatch_draw_count +=
            fog_enabled && !fog_parameters_available ? 1u : 0u;
        const uint32_t enabled_texture_stage_count =
            static_cast<uint32_t>(std::count_if(
                draw.texture_controls.begin(),
                draw.texture_controls.end(),
                [](uint32_t control) {
                    return (control & (1u << 30u)) != 0u;
                }));
        multiple_texture_stage_draw_count +=
            enabled_texture_stage_count > 1u ? 1u : 0u;
        bool texture_stage_coverage_complete = true;
        bool cubemap_stage_present = false;
        for (uint32_t texture_stage = 0u;
             texture_stage < draw.texture_controls.size();
             ++texture_stage) {
            const uint32_t texture_mode =
                (draw.shader_stage_program >> (texture_stage * 5u)) & 0x1Fu;
            if ((draw.texture_controls[texture_stage] & (1u << 30u)) == 0u
                || texture_mode == 0u) {
                continue;
            }
            const bool cubemap = nv2a_texture_format_is_cubemap(
                draw.texture_formats[texture_stage]);
            cubemap_stage_present |= cubemap;
            const bool mode_supported =
                texture_mode == 1u || texture_mode == 3u;
            const bool resource_supported = std::any_of(
                host_textures_.begin(),
                host_textures_.end(),
                [&](const HostTexture& texture) {
                    return host_texture_matches_stage(
                        texture, draw, texture_stage);
                });
            texture_stage_coverage_complete &=
                mode_supported && resource_supported;
        }
        texture_stage_coverage_mismatch_draw_count +=
            texture_stage_coverage_complete ? 0u : 1u;
        cubemap_texture_stage_draw_count += cubemap_stage_present ? 1u : 0u;
        cubemap_texture_stage_coverage_mismatch_draw_count +=
            cubemap_stage_present && !texture_stage_coverage_complete ? 1u : 0u;
        if (draw.texture_enabled) {
            ++textured_draw_count;
            ++texture_address_state_draw_count;
            const uint32_t address = draw.texture_addresses[stage];
            const uint32_t u_mode = address & 7u;
            const uint32_t v_mode = (address >> 8u) & 7u;
            repeat_address_draw_count +=
                u_mode == 1u || v_mode == 1u ? 1u : 0u;
            mirrored_repeat_address_draw_count +=
                u_mode == 2u || v_mode == 2u ? 1u : 0u;
            sampler_address_mismatch_draw_count +=
                (u_mode < 1u || u_mode > 5u
                    || v_mode < 1u || v_mode > 5u)
                ? 1u : 0u;
            const bool outside_unit =
                diagnostics.final_u_bounds.valid
                && diagnostics.final_v_bounds.valid
                && (diagnostics.final_u_bounds.minimum < 0.0f
                    || diagnostics.final_u_bounds.maximum > 1.0f
                    || diagnostics.final_v_bounds.minimum < 0.0f
                    || diagnostics.final_v_bounds.maximum > 1.0f);
            out_of_unit_texture_coordinate_draw_count +=
                outside_unit ? 1u : 0u;
            const uint32_t texture_mode =
                (draw.shader_stage_program >> (stage * 5u)) & 0x1Fu;
            projective_texture_draw_count += texture_mode == 1u ? 1u : 0u;
            non_unit_projective_q_draw_count +=
                texture_mode == 1u
                    && diagnostics.final_q_bounds.valid
                    && (std::abs(diagnostics.final_q_bounds.minimum - 1.0f)
                            > 0.000001f
                        || std::abs(diagnostics.final_q_bounds.maximum - 1.0f)
                            > 0.000001f)
                ? 1u : 0u;
            const bool alpha_kill =
                (draw.texture_controls[stage] & (1u << 2u)) != 0u;
            texture_alpha_kill_enabled_draw_count += alpha_kill ? 1u : 0u;
            texture_alpha_kill_applied_draw_count += alpha_kill ? 1u : 0u;
            const uint32_t color_format =
                (draw.texture_formats[stage] >> 8u) & 0xFFu;
            const bool opaque_x8 = color_format == 0x07u
                || color_format == 0x1Eu;
            opaque_x8_texture_draw_count += opaque_x8 ? 1u : 0u;
            opaque_x8_alpha_forced_draw_count += opaque_x8 ? 1u : 0u;
            const bool uncompressed_swizzled = color_format == 0x05u
                || color_format == 0x06u
                || color_format == 0x07u;
            uncompressed_swizzled_texture_draw_count +=
                uncompressed_swizzled ? 1u : 0u;
            uncompressed_texture_unswizzled_draw_count +=
                uncompressed_swizzled ? 1u : 0u;
            const auto texture_extent = nv2a_texture_extent(
                draw.texture_formats[stage],
                draw.texture_image_rects[stage]);
            const bool render_target_feedback = std::any_of(
                feedback_specs.begin(),
                feedback_specs.end(),
                [&](const RenderTargetFeedbackSpec& target) {
                    return nv2a_canonical_resource_address(target.address)
                            == nv2a_canonical_resource_address(
                                draw.texture_address)
                        && target.width == texture_extent.first
                        && target.height == texture_extent.second
                        && nv2a_texture_format_matches(
                            target.format,
                            draw.texture_formats[stage]);
                });
            render_target_feedback_draw_count +=
                render_target_feedback ? 1u : 0u;
            const auto resource = std::find_if(
                recovered_source_.textures.begin(),
                recovered_source_.textures.end(),
                [&](const RecoveredTextureResource& candidate) {
                    return candidate.address == draw.texture_address;
                });
            const bool all_zero = !render_target_feedback
                && resource != recovered_source_.textures.end()
                && !resource->payload.empty()
                && std::all_of(
                    resource->payload.begin(),
                    resource->payload.end(),
                    [](uint8_t value) { return value == 0u; });
            zero_payload_textured_draw_count += all_zero ? 1u : 0u;
            if (all_zero) {
                bool raw_producer_exists = false;
                bool producer_exists = false;
                for (auto producer = interpreted_stream_.draws.begin();
                     producer != interpreted_stream_.draws.begin()
                        + static_cast<std::ptrdiff_t>(native_draw_index);
                     ++producer) {
                    raw_producer_exists |= producer->surface_color_offset
                        == draw.texture_address;
                    producer_exists |= nv2a_canonical_resource_address(
                            producer->surface_color_offset)
                        == nv2a_canonical_resource_address(
                            draw.texture_address);
                }
                unproduced_zero_payload_texture_draw_count +=
                    producer_exists ? 0u : 1u;
                aliased_gpu_produced_texture_draw_count +=
                    producer_exists && !raw_producer_exists ? 1u : 0u;
                offscreen_render_target_replay_draw_count +=
                    std::any_of(
                        feedback_specs.begin(),
                        feedback_specs.end(),
                        [&](const RenderTargetFeedbackSpec& target) {
                            return target.offscreen_produced
                                && nv2a_canonical_resource_address(
                                    target.address)
                                == nv2a_canonical_resource_address(
                                    draw.texture_address);
                        })
                    ? 1u : 0u;
            }
        }
        alpha_blend_enabled_draw_count += draw.blend_enable ? 1u : 0u;
        alpha_test_enabled_draw_count += draw.alpha_test_enable ? 1u : 0u;
        alpha_test_applied_draw_count += draw.alpha_test_enable ? 1u : 0u;
        const bool fixed_function =
            (draw.transform_execution_mode & 3u) == 0u;
        if (!fixed_function) {
            continue;
        }
        ++fixed_function_draw_count;
        const bool transformed =
            diagnostics.vertex_count != 0u
            && diagnostics.transform.fixed_function_transformed_vertex_count
                == diagnostics.vertex_count;
        fixed_function_transformed_draw_count += transformed ? 1u : 0u;
        fixed_function_raw_fallback_draw_count +=
            diagnostics.transform.raw_position_fallback_vertex_count != 0u
            ? 1u : 0u;
        fixed_function_filtered_draw_count +=
            draw_has_supported_host_transform(draw) ? 0u : 1u;
        fixed_function_invalid_vertex_count +=
            diagnostics.transform.invalid_fixed_function_vertex_count;
    }
    std::ostringstream feedback_addresses;
    feedback_addresses << '[';
    for (size_t index = 0; index < feedback_specs.size(); ++index) {
        if (index != 0u) {
            feedback_addresses << ',';
        }
        feedback_addresses << feedback_specs[index].address;
    }
    feedback_addresses << ']';
    log_.emit(
        "nv2a_render_state_diagnostics",
        {
            {"source_presented_draw_count", std::to_string(
                interpreted_stream_.presented_draw_count)},
            {"presented_draw_count", std::to_string(presented_draw_transform_diagnostics_.size())},
            {"gpu_vertex_program_draws", std::to_string(
                gpu_vertex_program_draw_count_)},
            {"gpu_output_diagnostics_deferred", json_bool(
                gpu_vertex_program_draw_count_ != 0u)},
            {"textured_presented_draw_count", std::to_string(textured_draw_count)},
            {"texture_address_state_draw_count", std::to_string(texture_address_state_draw_count)},
            {"out_of_unit_texture_coordinate_draw_count", std::to_string(out_of_unit_texture_coordinate_draw_count)},
            {"repeat_address_draw_count", std::to_string(repeat_address_draw_count)},
            {"mirrored_repeat_address_draw_count", std::to_string(mirrored_repeat_address_draw_count)},
            {"sampler_address_mismatch_draw_count", std::to_string(sampler_address_mismatch_draw_count)},
            {"projective_texture_draw_count", std::to_string(projective_texture_draw_count)},
            {"non_unit_projective_q_draw_count", std::to_string(non_unit_projective_q_draw_count)},
            {"alpha_blend_enabled_draw_count", std::to_string(alpha_blend_enabled_draw_count)},
            {"alpha_test_enabled_draw_count", std::to_string(alpha_test_enabled_draw_count)},
            {"alpha_test_applied_draw_count", std::to_string(alpha_test_applied_draw_count)},
            {"alpha_test_state_mismatch_draw_count", std::to_string(alpha_test_state_mismatch_draw_count)},
            {"texture_alpha_kill_enabled_draw_count", std::to_string(texture_alpha_kill_enabled_draw_count)},
            {"texture_alpha_kill_applied_draw_count", std::to_string(texture_alpha_kill_applied_draw_count)},
            {"opaque_x8_texture_draw_count", std::to_string(opaque_x8_texture_draw_count)},
            {"opaque_x8_alpha_forced_draw_count", std::to_string(opaque_x8_alpha_forced_draw_count)},
            {"uncompressed_swizzled_texture_draw_count", std::to_string(uncompressed_swizzled_texture_draw_count)},
            {"uncompressed_texture_unswizzled_draw_count", std::to_string(uncompressed_texture_unswizzled_draw_count)},
            {"register_combiner_programmed_draw_count", std::to_string(register_combiner_programmed_draw_count)},
            {"register_combiner_applied_draw_count", std::to_string(register_combiner_applied_draw_count)},
            {"fixed_function_texture_combiner_recovered_draw_count", std::to_string(fixed_function_texture_combiner_recovered_draw_count)},
            {"register_combiner_state_mismatch_draw_count", std::to_string(register_combiner_state_mismatch_draw_count)},
            {"fog_enabled_draw_count", std::to_string(fog_enabled_draw_count)},
            {"fog_factor_applied_draw_count", std::to_string(fog_factor_applied_draw_count)},
            {"fog_state_mismatch_draw_count", std::to_string(fog_state_mismatch_draw_count)},
            {"multiple_texture_stage_draw_count", std::to_string(multiple_texture_stage_draw_count)},
            {"texture_stage_coverage_mismatch_draw_count", std::to_string(texture_stage_coverage_mismatch_draw_count)},
            {"cubemap_texture_stage_draw_count", std::to_string(cubemap_texture_stage_draw_count)},
            {"cubemap_texture_stage_coverage_mismatch_draw_count", std::to_string(cubemap_texture_stage_coverage_mismatch_draw_count)},
            {"zero_payload_textured_draw_count", std::to_string(zero_payload_textured_draw_count)},
            {"unproduced_zero_payload_texture_draw_count", std::to_string(unproduced_zero_payload_texture_draw_count)},
            {"aliased_gpu_produced_texture_draw_count", std::to_string(aliased_gpu_produced_texture_draw_count)},
            {"offscreen_render_target_replay_draw_count", std::to_string(offscreen_render_target_replay_draw_count)},
            {"render_target_feedback_draw_count", std::to_string(render_target_feedback_draw_count)},
            {"render_target_feedback_required", std::to_string(
                feedback_specs.size())},
            {"render_target_feedback_required_addresses",
                feedback_addresses.str()},
            {"clear_surface_method_count", std::to_string(
                interpreted_stream_.clear_surface_method_count)},
            {"last_clear_surface_method_count", std::to_string(
                interpreted_stream_.last_clear_surface_method_count)},
            {"presented_surface_clear_count", std::to_string(
                interpreted_stream_.presented_surface_clears.size())},
            {"presented_color_clear_count", std::to_string(
                presented_surface_color_clear_count())},
            {"latest_presented_surface_clear_address",
                interpreted_stream_.presented_surface_clears.empty()
                    ? "null"
                    : std::to_string(interpreted_stream_
                        .presented_surface_clears.back()
                        .surface_color_offset)},
            {"latest_presented_surface_clear_flags",
                interpreted_stream_.presented_surface_clears.empty()
                    ? "null"
                    : std::to_string(interpreted_stream_
                        .presented_surface_clears.back().flags)},
            {"latest_presented_surface_clear_color_argb",
                interpreted_stream_.presented_surface_clears.empty()
                    ? "null"
                    : std::to_string(interpreted_stream_
                        .presented_surface_clears.back().color_argb)},
            {"latest_presented_surface_clear_draw_index",
                interpreted_stream_.presented_surface_clears.empty()
                    ? "null"
                    : std::to_string(interpreted_stream_
                        .presented_surface_clears.back().draw_index)},
            {"presented_draw_begin", std::to_string(
                interpreted_stream_.presented_draw_begin)},
            {"presented_draw_count", std::to_string(
                interpreted_stream_.presented_draw_count)},
            {"retained_presented_surface_available", json_bool(
                presented_render_target_feedback_available())},
            {"fixed_function_draw_count", std::to_string(fixed_function_draw_count)},
            {"fixed_function_transformed_draw_count", std::to_string(fixed_function_transformed_draw_count)},
            {"fixed_function_raw_fallback_draw_count", std::to_string(fixed_function_raw_fallback_draw_count)},
            {"fixed_function_filtered_draw_count", std::to_string(fixed_function_filtered_draw_count)},
            {"fixed_function_invalid_vertex_count", std::to_string(fixed_function_invalid_vertex_count)},
            {"continuation_bootstrap_used", json_bool(continuation_analysis_bootstrap_)},
            {"analysis_state_complete", json_bool(!continuation_analysis_bootstrap_)},
        });
}

void VulkanPresenter::write_u16(std::ofstream& output, uint16_t value) {
    const char bytes[2] = {
        static_cast<char>(value & 0xffu),
        static_cast<char>((value >> 8u) & 0xffu),
    };
    output.write(bytes, sizeof(bytes));
}

void VulkanPresenter::write_u32(std::ofstream& output, uint32_t value) {
    const char bytes[4] = {
        static_cast<char>(value & 0xffu),
        static_cast<char>((value >> 8u) & 0xffu),
        static_cast<char>((value >> 16u) & 0xffu),
        static_cast<char>((value >> 24u) & 0xffu),
    };
    output.write(bytes, sizeof(bytes));
}

std::filesystem::path VulkanPresenter::retain_hotkey_render_capture(
    const std::filesystem::path& screenshot_path) {
    if (current_live_manifest_text_.empty()
        || current_live_command_snapshot_path_.empty()
        || current_live_resource_snapshot_path_.empty()) {
        return {};
    }
    const std::filesystem::path capture_directory =
        screenshot_path.parent_path()
        / (screenshot_path.stem().string() + "-render-capture");
    std::error_code directory_error;
    std::filesystem::create_directories(
        capture_directory,
        directory_error);
    const std::filesystem::path command_copy =
        capture_directory / "commands.bin";
    const std::filesystem::path resource_copy =
        capture_directory / "resources.bin";
    const std::filesystem::path interpreter_bootstrap_copy =
        capture_directory / "interpreter-bootstrap.bin";
    const std::filesystem::path original_manifest_copy =
        capture_directory / "original-render.json";
    const std::filesystem::path retained_manifest =
        capture_directory / "render.json";
    std::error_code command_error;
    std::error_code resource_error;
    const uint64_t resident_command_count =
        current_manifest_presentable_command_count_
        - std::min(
            current_manifest_presentable_command_count_,
            current_manifest_command_base_count_);
    bool command_copied = false;
    if (!directory_error && current_manifest_shared_command_transport_) {
        const uint64_t resident_byte_count =
            current_manifest_presentable_command_byte_count_
            - current_manifest_command_base_byte_count_;
        if (resident_byte_count <= live_command_capture_bytes_.size()) {
            std::ofstream command_output(
                command_copy,
                std::ios::binary | std::ios::trunc);
            if (command_output) {
                command_output.write("B2SPAN01", 8);
                command_output.write(
                    reinterpret_cast<const char*>(
                        live_command_capture_bytes_.data()),
                    static_cast<std::streamsize>(resident_byte_count));
                command_copied = static_cast<bool>(command_output);
            }
        }
        if (!command_copied) {
            command_error = std::make_error_code(
                std::errc::io_error);
        }
    } else if (!directory_error) {
        command_copied = std::filesystem::copy_file(
            current_live_command_snapshot_path_,
            command_copy,
            std::filesystem::copy_options::overwrite_existing,
            command_error);
    }
    uint64_t command_snapshot_source_record_count = 0u;
    uint64_t command_snapshot_trimmed_record_count = 0u;
    bool command_snapshot_exact_prefix = false;
    if (command_copied && current_manifest_bulk_span_commands_) {
        constexpr uintmax_t header_size = 8u;
        const uint64_t resident_command_byte_count =
            current_manifest_presentable_command_byte_count_
            - current_manifest_command_base_byte_count_;
        std::array<char, 8> magic{};
        std::ifstream copied(command_copy, std::ios::binary);
        copied.read(magic.data(), static_cast<std::streamsize>(magic.size()));
        std::error_code size_error;
        const uintmax_t copied_size = std::filesystem::file_size(
            command_copy,
            size_error);
        if (!copied
            || std::string(magic.data(), magic.size()) != "B2SPAN01"
            || size_error
            || copied_size < header_size
            || resident_command_byte_count > copied_size - header_size) {
            command_error = std::make_error_code(std::errc::invalid_argument);
            command_copied = false;
        } else {
            copied.close();
            std::filesystem::resize_file(
                command_copy,
                header_size + resident_command_byte_count,
                command_error);
            command_copied = !command_error;
            command_snapshot_exact_prefix = command_copied;
            command_snapshot_source_record_count = resident_command_count;
        }
    } else if (command_copied) {
        constexpr uintmax_t header_size = 8u;
        constexpr uintmax_t record_size = 16u;
        std::array<char, 8> magic{};
        std::ifstream copied(command_copy, std::ios::binary);
        copied.read(magic.data(), static_cast<std::streamsize>(magic.size()));
        std::error_code size_error;
        const uintmax_t copied_size = std::filesystem::file_size(
            command_copy,
            size_error);
        if (!copied
            || std::string(magic.data(), magic.size()) != "B2APPND1"
            || size_error
            || copied_size < header_size
            || (copied_size - header_size) % record_size != 0u) {
            command_error = std::make_error_code(std::errc::invalid_argument);
            command_copied = false;
        } else {
            command_snapshot_source_record_count =
                static_cast<uint64_t>((copied_size - header_size) / record_size);
            if (command_snapshot_source_record_count < resident_command_count
                || resident_command_count
                    > (std::numeric_limits<uintmax_t>::max() - header_size)
                        / record_size) {
                command_error = std::make_error_code(
                    std::errc::result_out_of_range);
                command_copied = false;
            } else {
                const uintmax_t retained_size = header_size
                    + static_cast<uintmax_t>(resident_command_count)
                        * record_size;
                copied.close();
                std::filesystem::resize_file(
                    command_copy,
                    retained_size,
                    command_error);
                command_copied = !command_error;
                command_snapshot_exact_prefix = command_copied;
                command_snapshot_trimmed_record_count =
                    command_snapshot_source_record_count
                    - resident_command_count;
            }
        }
    }
    bool resource_copied = false;
    if (!directory_error && !current_live_resource_snapshot_bytes_.empty()) {
        std::ofstream resource_output(
            resource_copy,
            std::ios::binary | std::ios::trunc);
        if (resource_output) {
            resource_output.write(
                reinterpret_cast<const char*>(
                    current_live_resource_snapshot_bytes_.data()),
                static_cast<std::streamsize>(
                    current_live_resource_snapshot_bytes_.size()));
            resource_copied = static_cast<bool>(resource_output);
        }
        if (!resource_copied) {
            resource_error = std::make_error_code(std::errc::io_error);
        }
    } else if (!directory_error) {
        resource_copied = std::filesystem::copy_file(
            current_live_resource_snapshot_path_,
            resource_copy,
            std::filesystem::copy_options::overwrite_existing,
            resource_error);
    }
    bool original_manifest_written = false;
    if (!directory_error) {
        std::ofstream original(
            original_manifest_copy,
            std::ios::binary | std::ios::trunc);
        if (original) {
            original << current_live_manifest_text_;
            original_manifest_written = static_cast<bool>(original);
        }
    }
    bool interpreter_bootstrap_written = false;
    uint32_t interpreter_bootstrap_method_count = 0u;
    const size_t first_presented_draw = std::min<size_t>(
        interpreted_stream_.presented_draw_begin,
        interpreted_stream_.draws.size());
    if (!directory_error
        && interpreted_stream_.presented_draw_count != 0u
        && first_presented_draw < interpreted_stream_.draws.size()) {
        const std::vector<Nv2aBootstrapMethod> bootstrap_methods =
            native_draw_interpreter_bootstrap_methods(
                interpreted_stream_.draws[first_presented_draw],
                interpreted_stream_.clear_color_valid,
                interpreted_stream_.clear_color_argb);
        std::ofstream bootstrap(
            interpreter_bootstrap_copy,
            std::ios::binary | std::ios::trunc);
        if (bootstrap) {
            bootstrap.write("B2NVST01", 8);
            write_u32(
                bootstrap,
                static_cast<uint32_t>(bootstrap_methods.size()));
            for (const auto& [method, value] : bootstrap_methods) {
                write_u32(bootstrap, method);
                write_u32(bootstrap, value);
            }
            interpreter_bootstrap_written = static_cast<bool>(bootstrap);
            interpreter_bootstrap_method_count =
                interpreter_bootstrap_written
                ? static_cast<uint32_t>(bootstrap_methods.size())
                : 0u;
        }
    }
    bool retained_manifest_written = false;
    if (command_copied && resource_copied) {
        std::ofstream retained(
            retained_manifest,
            std::ios::binary | std::ios::trunc);
        if (retained) {
            retained
                << "{\"format\":\"b2-recomp-hotkey-render-capture\""
                << ",\"guest_flip_count\":"
                << current_manifest_guest_flip_count_
                << ",\"guest_steps\":" << current_manifest_guest_steps_
                << ",\"write_count\":"
                << current_manifest_presentable_command_count_
                << ",\"published_command_record_count\":"
                << current_manifest_presentable_command_count_
                << ",\"presentable_command_record_count\":"
                << current_manifest_presentable_command_count_
                << ",\"command_snapshot_base_record_count\":"
                << current_manifest_command_base_count_
                << ",\"command_snapshot_record_count\":"
                << resident_command_count
                << ",\"command_snapshot_source_record_count\":"
                << command_snapshot_source_record_count
                << ",\"command_snapshot_trimmed_record_count\":"
                << command_snapshot_trimmed_record_count
                << ",\"command_snapshot_exact_prefix\":"
                << json_bool(command_snapshot_exact_prefix)
                << ",\"command_transport_format\":"
                << json_string(
                    current_manifest_bulk_span_commands_
                        ? "bulk_span_v1" : "packed_record_v1")
                << ",\"published_command_byte_count\":"
                << current_manifest_presentable_command_byte_count_
                << ",\"presentable_command_byte_count\":"
                << current_manifest_presentable_command_byte_count_
                << ",\"command_snapshot_base_byte_count\":"
                << current_manifest_command_base_byte_count_
                << ",\"command_snapshot_byte_count\":"
                << (current_manifest_presentable_command_byte_count_
                    - current_manifest_command_base_byte_count_)
                << ",\"command_stream_generation\":"
                << json_string(live_command_generation_)
                << ",\"resource_stream_generation\":"
                << json_string(live_resource_generation_)
                << ",\"command_snapshot_path\":"
                << json_string(command_copy.string())
                << ",\"resource_snapshot_path\":"
                << json_string(resource_copy.string())
                << ",\"interpreter_bootstrap_path\":"
                << (interpreter_bootstrap_written
                    ? json_string(interpreter_bootstrap_copy.string())
                    : "null")
                << ",\"interpreter_bootstrap_method_count\":"
                << interpreter_bootstrap_method_count
                << ",\"screenshot_path\":"
                << json_string(screenshot_path.string());
            if (!recovered_frontend_text_.empty()) {
                retained << ",\"frontend_text\":"
                         << json_string(recovered_frontend_text_)
                         << ",\"frontend_text_x_bits\":"
                         << recovered_source_.frontend_text_x_bits
                         << ",\"frontend_text_y_bits\":"
                         << recovered_source_.frontend_text_y_bits
                         << ",\"frontend_text_size_bits\":"
                         << recovered_source_.frontend_text_size_bits
                         << ",\"frontend_text_color_argb\":"
                         << recovered_source_.frontend_text_color_argb;
            }
            retained
                << ",\"live_state_history_complete\":"
                << json_bool(!continuation_analysis_bootstrap_)
                << ",\"standalone_replay_state_complete\":"
                << json_bool(
                    current_manifest_command_base_count_ == 0u
                    || interpreter_bootstrap_written)
                << "}";
            retained_manifest_written = static_cast<bool>(retained);
        }
    }
    log_.emit(
        "hotkey_render_capture_retained",
        {
            {"screenshot", json_string(screenshot_path.string())},
            {"directory", json_string(capture_directory.string())},
            {"manifest", retained_manifest_written
                ? json_string(retained_manifest.string()) : "null"},
            {"command_snapshot_copied", json_bool(command_copied)},
            {"resource_snapshot_copied", json_bool(resource_copied)},
            {"interpreter_bootstrap_written", json_bool(
                interpreter_bootstrap_written)},
            {"interpreter_bootstrap_method_count", std::to_string(
                interpreter_bootstrap_method_count)},
            {"interpreter_bootstrap", interpreter_bootstrap_written
                ? json_string(interpreter_bootstrap_copy.string()) : "null"},
            {"original_manifest_copied", json_bool(original_manifest_written)},
            {"manifest_guest_flip_count", std::to_string(
                current_manifest_guest_flip_count_)},
            {"command_snapshot_base_record_count", std::to_string(
                current_manifest_command_base_count_)},
            {"command_snapshot_record_count", std::to_string(
                resident_command_count)},
            {"command_snapshot_source_record_count", std::to_string(
                command_snapshot_source_record_count)},
            {"command_snapshot_trimmed_record_count", std::to_string(
                command_snapshot_trimmed_record_count)},
            {"command_snapshot_exact_prefix", json_bool(
                command_snapshot_exact_prefix)},
            {"standalone_replay_state_complete", json_bool(
                current_manifest_command_base_count_ == 0u
                || interpreter_bootstrap_written)},
            {"command_error", command_error
                ? json_string(command_error.message()) : "null"},
            {"resource_error", resource_error
                ? json_string(resource_error.message()) : "null"},
        });
    return retained_manifest_written
        ? retained_manifest
        : std::filesystem::path{};
}

PresenterMetricsSnapshot VulkanPresenter::collect_metrics_snapshot() const {
    PresenterMetricsSnapshot snapshot{};
    snapshot.frame = frame_count_;
    snapshot.guest_fps_valid = guest_fps_sample_valid_;
    snapshot.guest_fps = last_guest_fps_;
    snapshot.guest_fps_sample_flips =
        last_guest_fps_sample_completed_flips_;
    snapshot.guest_fps_sample_seconds = last_guest_fps_sample_seconds_;
    snapshot.guest_completed_flips = current_manifest_guest_flip_count_;
    snapshot.presenter_frame_us = last_frame_elapsed_us_;
    snapshot.guest_instructions = current_manifest_guest_steps_;
    snapshot.compiled_blocks = current_manifest_guest_compiled_blocks_;
    snapshot.invalidations = current_manifest_guest_invalidations_;
    snapshot.push_buffer_commands = interpreted_method_count_;
    snapshot.draws = draw_count_;
    snapshot.triangles = triangle_count_;
    snapshot.pipeline_creations = pipeline_creation_count_;
    snapshot.pipeline_cache_misses = pipeline_cache_miss_count_;
    snapshot.descriptor_allocations = descriptor_allocation_count_;
    snapshot.command_buffer_allocations = command_buffer_allocation_count_;
    snapshot.queue_submissions = queue_submission_count_;
    snapshot.barriers = barrier_count_;
    snapshot.upload_bytes = upload_bytes_;
    snapshot.readback_bytes = readback_bytes_;
    snapshot.cpu_render_us = last_cpu_render_us_;
    snapshot.platform_poll_us = last_window_message_pump_us_;
    snapshot.controller_poll_us = last_controller_poll_us_;
    snapshot.keyboard_latch_us = last_keyboard_latch_us_;
    snapshot.audio_submit_us = last_audio_submit_us_;
    snapshot.reload_probe_us = last_reload_probe_us_;
    snapshot.pre_render_unattributed_us = last_pre_render_unattributed_us_;
    snapshot.gpu_frame_time_valid = gpu_frame_time_valid_;
    snapshot.gpu_frame_ms = last_gpu_frame_ms_;
    snapshot.fence_wait_us = last_fence_wait_us_;
    snapshot.draw_fence_wait_us = last_draw_fence_wait_us_;
    snapshot.image_acquire_us = last_acquire_us_;
    snapshot.queue_submit_us = last_submit_us_;
    snapshot.readback_wait_us = last_readback_wait_us_;
    snapshot.queue_present_us = last_present_us_;
    snapshot.draw_unattributed_us = last_draw_unattributed_us_;
    return snapshot;
}

void VulkanPresenter::write_metrics_report() {
    const std::filesystem::path output_path = metrics_reporter_.write(
        options_.metrics_report_directory, collect_metrics_snapshot());
    log_.emit(
        "metrics_report_written",
        {
            {"output", json_string(output_path.string())},
            {"frame", std::to_string(frame_count_)},
            {"guest_steps", std::to_string(current_manifest_guest_steps_)},
        });
}

std::filesystem::path VulkanPresenter::next_hotkey_screenshot_path() {
    SYSTEMTIME now{};
    GetLocalTime(&now);
    ++hotkey_screenshot_count_;
    std::wostringstream name;
    name << L"b2-recomp-"
         << std::setfill(L'0')
         << std::setw(4) << now.wYear
         << std::setw(2) << now.wMonth
         << std::setw(2) << now.wDay << L'-'
         << std::setw(2) << now.wHour
         << std::setw(2) << now.wMinute
         << std::setw(2) << now.wSecond << L'-'
         << std::setw(3) << now.wMilliseconds
         << L"-frame-" << frame_count_ + 1u
         << L"-" << hotkey_screenshot_count_ << L".bmp";
    return options_.hotkey_screenshot_directory / name.str();
}

FrameReadback VulkanPresenter::read_frame() {
    void* mapped = nullptr;
    vk_check(vkMapMemory(device_, readback_memory_, 0, readback_size_, 0, &mapped), "vkMapMemory(readback)");
    const auto* source = static_cast<const uint8_t*>(mapped);
    FrameReadback readback;
    readback.bgra.resize(static_cast<size_t>(readback_size_));
    readback.pixel_count = swapchain_extent_.width * swapchain_extent_.height;
    std::unordered_map<uint32_t, uint32_t> color_counts;
    color_counts.reserve(std::min<uint32_t>(readback.pixel_count, 65536u));
    const bool source_is_rgba = swapchain_format_ == VK_FORMAT_R8G8B8A8_SRGB
        || swapchain_format_ == VK_FORMAT_R8G8B8A8_UNORM;
    for (size_t offset = 0; offset < readback.bgra.size(); offset += 4) {
        const uint8_t red = source_is_rgba ? source[offset] : source[offset + 2];
        const uint8_t green = source[offset + 1];
        const uint8_t blue = source_is_rgba ? source[offset + 2] : source[offset];
        const uint8_t alpha = source[offset + 3];
        readback.bgra[offset] = blue;
        readback.bgra[offset + 1] = green;
        readback.bgra[offset + 2] = red;
        readback.bgra[offset + 3] = alpha;
        for (const uint8_t channel : {blue, green, red, alpha}) {
            readback.pixel_fingerprint ^= channel;
            readback.pixel_fingerprint *= 1099511628211ull;
        }
        const uint32_t rgba = (static_cast<uint32_t>(red) << 24u)
            | (static_cast<uint32_t>(green) << 16u)
            | (static_cast<uint32_t>(blue) << 8u)
            | static_cast<uint32_t>(alpha);
        ++color_counts[rgba];
        if (red >= 245u && green >= 245u && blue >= 245u) {
            ++readback.bright_count;
        }
        if (red <= 10u && green <= 10u && blue <= 10u) {
            ++readback.dark_count;
        }
    }
    vkUnmapMemory(device_, readback_memory_);

    readback.unique_colors = static_cast<uint32_t>(color_counts.size());
    for (const auto& entry : color_counts) {
        if (entry.second > readback.dominant_count
            || (entry.second == readback.dominant_count
                && entry.first < readback.dominant_rgba)) {
            readback.dominant_rgba = entry.first;
            readback.dominant_count = entry.second;
        }
    }
    const uint64_t threshold = static_cast<uint64_t>(readback.pixel_count) * 98u;
    readback.near_solid = static_cast<uint64_t>(readback.dominant_count) * 100u >= threshold;
    readback.whiteout = static_cast<uint64_t>(readback.bright_count) * 100u >= threshold;
    readback.low_information = readback.unique_colors < 16u;
    return readback;
}

void VulkanPresenter::write_screenshot(
    const std::filesystem::path& screenshot_path,
    const FrameReadback& readback,
    const char* trigger) {
    if (screenshot_path.has_parent_path()) {
        std::filesystem::create_directories(screenshot_path.parent_path());
    }
    std::ofstream output(screenshot_path, std::ios::binary | std::ios::trunc);
    if (!output) {
        throw std::runtime_error("unable to open screenshot output: " + screenshot_path.string());
    }
    constexpr uint32_t header_size = 14u + 40u;
    write_u16(output, 0x4d42u);
    write_u32(output, header_size + static_cast<uint32_t>(readback.bgra.size()));
    write_u16(output, 0);
    write_u16(output, 0);
    write_u32(output, header_size);
    write_u32(output, 40);
    write_u32(output, swapchain_extent_.width);
    write_u32(output, static_cast<uint32_t>(-static_cast<int32_t>(swapchain_extent_.height)));
    write_u16(output, 1);
    write_u16(output, 32);
    write_u32(output, 0);
    write_u32(output, static_cast<uint32_t>(readback.bgra.size()));
    write_u32(output, 2835);
    write_u32(output, 2835);
    write_u32(output, 0);
    write_u32(output, 0);
    output.write(
        reinterpret_cast<const char*>(readback.bgra.data()),
        static_cast<std::streamsize>(readback.bgra.size()));
    if (!output) {
        throw std::runtime_error("failed while writing screenshot: " + screenshot_path.string());
    }
    const bool f12_capture = std::strcmp(trigger, "f12") == 0;
    const bool offscreen_depth_isolated = std::all_of(
        offscreen_render_targets_.begin(),
        offscreen_render_targets_.end(),
        [](const OffscreenRenderTarget& target) {
            return target.depth_view != VK_NULL_HANDLE
                && target.depth_image != VK_NULL_HANDLE
                && target.depth_memory != VK_NULL_HANDLE;
        });
    log_.emit(
        "frame_readback_captured",
        {
            {"output", json_string(screenshot_path.string())},
            {"trigger", json_string(trigger)},
            {"width", std::to_string(swapchain_extent_.width)},
            {"height", std::to_string(swapchain_extent_.height)},
            {"manifest_guest_flip_count", std::to_string(current_manifest_guest_flip_count_)},
            {"manifest_guest_steps", std::to_string(current_manifest_guest_steps_)},
            {"pixel_count", std::to_string(readback.pixel_count)},
            {"unique_colors", std::to_string(readback.unique_colors)},
            {"dominant_rgba", std::to_string(readback.dominant_rgba)},
            {"dominant_count", std::to_string(readback.dominant_count)},
            {"bright_count", std::to_string(readback.bright_count)},
            {"dark_count", std::to_string(readback.dark_count)},
            {"pixel_fingerprint", std::to_string(
                readback.pixel_fingerprint)},
            {"near_solid", json_bool(readback.near_solid)},
            {"whiteout", json_bool(readback.whiteout)},
            {"low_information", json_bool(readback.low_information)},
            {"presented_surface_color_offset", std::to_string(
                presented_surface_color_offset_)},
            {"command_snapshot_base_record_count", std::to_string(
                current_manifest_command_base_count_)},
            {"command_stream_generation", live_command_generation_.empty()
                ? "null" : json_string(live_command_generation_)},
            {"resource_stream_generation", live_resource_generation_.empty()
                ? "null" : json_string(live_resource_generation_)},
            {"offscreen_render_target_count", std::to_string(
                offscreen_render_targets_.size())},
            {"offscreen_depth_isolated", json_bool(
                offscreen_depth_isolated)},
            {"render_capture_manifest", f12_capture
                && !pending_hotkey_render_capture_manifest_.empty()
                ? json_string(
                    pending_hotkey_render_capture_manifest_.string())
                : "null"},
        });
}

void VulkanPresenter::capture_screenshot(
    const std::filesystem::path& screenshot_path,
    const char* trigger) {
    write_screenshot(screenshot_path, read_frame(), trigger);
}

}  // namespace b2r::host::vulkan_detail
