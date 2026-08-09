#include "vulkan_presenter_internal.h"

namespace b2r::host::vulkan_detail {

std::string narrow(const std::wstring& value) {
    if (value.empty()) {
        return {};
    }
    const int size = WideCharToMultiByte(
        CP_UTF8, 0, value.data(), static_cast<int>(value.size()), nullptr, 0, nullptr, nullptr);
    if (size <= 0) {
        return {};
    }
    std::string result(static_cast<size_t>(size), '\0');
    WideCharToMultiByte(
        CP_UTF8, 0, value.data(), static_cast<int>(value.size()), result.data(), size, nullptr, nullptr);
    return result;
}


std::wstring widen(const std::string& value) {
    if (value.empty()) {
        return {};
    }
    const int size = MultiByteToWideChar(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), nullptr, 0);
    if (size <= 0) {
        return {};
    }
    std::wstring result(static_cast<size_t>(size), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), result.data(), size);
    return result;
}


std::optional<std::string> read_text_handle_shared(HANDLE file) {
    LARGE_INTEGER start{};
    if (file == INVALID_HANDLE_VALUE
        || !SetFilePointerEx(file, start, nullptr, FILE_BEGIN)) {
        return std::nullopt;
    }
    LARGE_INTEGER size{};
    if (!GetFileSizeEx(file, &size) || size.QuadPart < 0
        || static_cast<uint64_t>(size.QuadPart) > std::numeric_limits<DWORD>::max()) {
        return std::nullopt;
    }
    std::string payload(static_cast<size_t>(size.QuadPart), '\0');
    DWORD bytes_read = 0;
    const BOOL read_ok = payload.empty()
        || ReadFile(
            file,
            payload.data(),
            static_cast<DWORD>(payload.size()),
            &bytes_read,
            nullptr);
    if (!read_ok || bytes_read != payload.size()) {
        return std::nullopt;
    }
    return payload;
}


std::optional<std::string> read_text_file_shared(
    const std::filesystem::path& path) {
    const HANDLE file = CreateFileW(
        path.c_str(),
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        nullptr,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        nullptr);
    if (file == INVALID_HANDLE_VALUE) {
        return std::nullopt;
    }
    const auto payload = read_text_handle_shared(file);
    CloseHandle(file);
    return payload;
}


std::string escape_json(const std::string& value) {
    std::ostringstream out;
    for (const unsigned char ch : value) {
        switch (ch) {
            case '\\':
                out << "\\\\";
                break;
            case '"':
                out << "\\\"";
                break;
            case '\b':
                out << "\\b";
                break;
            case '\f':
                out << "\\f";
                break;
            case '\n':
                out << "\\n";
                break;
            case '\r':
                out << "\\r";
                break;
            case '\t':
                out << "\\t";
                break;
            default:
                if (ch < 0x20) {
                    out << "\\u" << std::hex << std::setw(4) << std::setfill('0')
                        << static_cast<int>(ch);
                } else {
                    out << ch;
                }
                break;
        }
    }
    return out.str();
}


std::string json_string(const std::string& value) {
    return "\"" + escape_json(value) + "\"";
}


std::string json_bool(bool value) {
    return value ? "true" : "false";
}


std::string json_float(float value) {
    return std::isfinite(value) ? std::to_string(value) : "null";
}


bool push_buffer_word_starts_run(
    const std::vector<PushBufferWord>& words,
    size_t index) {
    return index == 0u || words[index].run_id != words[index - 1u].run_id;
}


Nv2aVertexAttributes default_nv2a_vertex_attributes() {
    Nv2aVertexAttributes attributes{};
    for (auto& attribute : attributes) {
        attribute = {0.0f, 0.0f, 0.0f, 1.0f};
    }
    attributes[3] = {1.0f, 1.0f, 1.0f, 1.0f};
    return attributes;
}


bool host_texture_matches_draw(
    const HostTexture& texture,
    const NativeDraw& draw) {
    if (!draw.texture_enabled) {
        return texture.guest_address == 0u;
    }
    return host_texture_matches_stage(texture, draw, draw.texture_stage);
}


bool host_texture_matches_stage(
    const HostTexture& texture,
    const NativeDraw& draw,
    uint32_t stage) {
    if (stage >= draw.texture_formats.size()
        || nv2a_canonical_resource_address(texture.guest_address)
            != nv2a_canonical_resource_address(
                draw.texture_offsets[stage])) {
        return false;
    }
    const uint32_t format_raw = draw.texture_formats[stage];
    const auto [width, height] = nv2a_texture_extent(
        format_raw,
        draw.texture_image_rects[stage]);
    return texture.width == width
        && texture.height == height
        && texture.cubemap == nv2a_texture_format_is_cubemap(format_raw)
        && (nv2a_texture_format_matches(texture.format, format_raw)
            || (texture.render_target_feedback
                && b2r::nv2a::nv2a_render_target_feedback_format_matches(
                    texture.format,
                    format_raw)));
}


VkSamplerAddressMode nv2a_sampler_address_mode(uint32_t value) {
    switch (value & 7u) {
    case 1u: return VK_SAMPLER_ADDRESS_MODE_REPEAT;
    case 2u: return VK_SAMPLER_ADDRESS_MODE_MIRRORED_REPEAT;
    case 3u: return VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
    case 4u: return VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_BORDER;
    case 5u: return VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
    default: return VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
    }
}


std::string nv2a_sampler_address_name(uint32_t value) {
    switch (value & 7u) {
    case 1u: return "repeat";
    case 2u: return "mirrored_repeat";
    case 3u: return "clamp_to_edge";
    case 4u: return "clamp_to_border";
    case 5u: return "clamp_ogl";
    default: return "invalid_default_clamp";
    }
}


NativePipelineState pipeline_state_for_draw(const NativeDraw& draw) {
    NativePipelineState state{
        draw.primitive,
        draw.blend_enable,
        draw.blend_source_factor,
        draw.blend_destination_factor,
        draw.blend_equation,
        draw.color_mask,
        draw.depth_test_enable,
        draw.depth_function,
        draw.depth_write_enable,
        draw.cull_face_enable,
        draw.cull_face,
        draw.front_face,
        draw.gpu_raw_attribute_fetch,
    };
    return state;
}


NativePipelineState frontend_text_pipeline_state() {
    NativePipelineState state{};
    state.primitive = 5u;
    state.blend_enable = 1u;
    state.blend_source_factor = 0x0302u;
    state.blend_destination_factor = 0x0303u;
    state.depth_write_enable = 0u;
    return state;
}


bool default_fixed_function_texture_combiner_recovery_required(
    const NativeDraw& draw) {
    if (!draw.texture_enabled || draw.texture_stage != 0u
        || (draw.shader_stage_program & 0x1Fu) == 0u
        || draw.combiner_control != 1u) {
        return false;
    }
    return draw.combiner_color_inputs[0] == 0x04200000u
        && draw.combiner_color_outputs[0] == 0x00000C00u
        && draw.combiner_alpha_inputs[0] == 0x14200000u
        && draw.combiner_alpha_outputs[0] == 0x00000C00u
        && draw.final_combiner_inputs0 == 0x0000000Cu
        && draw.final_combiner_inputs1 == 0x00001C80u;
}


NativeFragmentState fragment_state_for_draw(const NativeDraw& draw) {
    NativeFragmentState state{};
    state.alpha_test_enable = draw.alpha_test_enable;
    state.alpha_function = draw.alpha_function;
    state.alpha_reference = draw.alpha_reference & 0xFFu;
    for (uint32_t stage = 0u; stage < state.texture_modes.size(); ++stage) {
        const uint32_t programmed_texture_mode =
            (draw.shader_stage_program >> (stage * 5u)) & 0x1Fu;
        const bool enabled =
            (draw.texture_controls[stage] & (1u << 30u)) != 0u;
        state.texture_modes[stage] = enabled
            ? programmed_texture_mode
            : 0u;
        if (state.texture_modes[stage] == 0u) {
            continue;
        }
        state.texture_alpha_kill_mask |=
            (draw.texture_controls[stage] & (1u << 2u)) != 0u
            ? 1u << stage
            : 0u;
        const uint32_t texture_color_format =
            (draw.texture_formats[stage] >> 8u) & 0xFFu;
        state.texture_opaque_alpha_mask |=
            texture_color_format == 0x07u
                    || texture_color_format == 0x1Eu
                ? 1u << stage
                : 0u;
        state.texture_cubemap_mask |=
            nv2a_texture_format_is_cubemap(draw.texture_formats[stage])
                ? 1u << stage
                : 0u;
    }
    state.combiner_control = draw.combiner_control;
    state.shader_stage_program = draw.shader_stage_program;
    state.combiner_color_inputs = draw.combiner_color_inputs;
    state.combiner_color_outputs = draw.combiner_color_outputs;
    state.combiner_alpha_inputs = draw.combiner_alpha_inputs;
    state.combiner_alpha_outputs = draw.combiner_alpha_outputs;
    state.combiner_factors0 = draw.combiner_factors0;
    state.combiner_factors1 = draw.combiner_factors1;
    state.final_combiner_inputs0 = draw.final_combiner_inputs0;
    state.final_combiner_inputs1 = draw.final_combiner_inputs1;
    state.final_combiner_factor0 = draw.final_combiner_factors[0];
    state.final_combiner_factor1 = draw.final_combiner_factors[1];
    if (default_fixed_function_texture_combiner_recovery_required(draw)) {
        // The title leaves the canonical diffuse-only NV2A defaults active
        // for its fixed-function D3D texture draws. Recover the equivalent
        // stage-zero MODULATE operation so the sampled texture and its alpha
        // mask are combined with the vertex color/fade alpha.
        state.combiner_color_inputs[0] = 0x08040000u;
        state.combiner_alpha_inputs[0] = 0x18140000u;
    }
    state.fog_color = draw.fog_color;
    state.fog_enable = draw.fog_enable;
    return state;
}


NativeFragmentState frontend_text_fragment_state() {
    NativeFragmentState state{};
    state.combiner_control = 1u;
    state.combiner_color_inputs[0] = 0x04200000u;
    state.combiner_color_outputs[0] = 0x00000C00u;
    state.combiner_alpha_inputs[0] = 0x14200000u;
    state.combiner_alpha_outputs[0] = 0x00000C00u;
    state.final_combiner_inputs0 = 0x0000000Cu;
    state.final_combiner_inputs1 = 0x00001C80u;
    return state;
}


bool gpu_vertex_program_compatible(const NativeDraw& draw) {
    if ((draw.transform_execution_mode & 3u) != 2u
        || !draw.indexed_array
        || draw.texture_stage >= draw.texture_formats.size()
        || draw.transform_program_start >= draw.transform_program.size()) {
        return false;
    }
    // The indexed materializer produces a complete, immutable 16-register
    // input bank. Inline arrays currently reconstruct missing attributes in
    // the CPU executor, so they remain on that exact path until the shader
    // receives an equivalent validity/fallback contract.
    // Preserve the CPU path for the title's recovered half-surface and
    // overscan quad fixups. They inspect post-program output before modifying
    // the final vertices and are intentionally outside the generic program.
    const size_t effective_vertex_count = draw.vertex_count != 0u
        ? draw.vertex_count
        : draw.vertex_indices.size();
    if (draw.primitive == 6u && effective_vertex_count == 4u
        && draw.texture_address != 0u) {
        return false;
    }
    const auto temporary_source_valid = [](uint32_t type, uint32_t index) {
        return type != 1u || index == 12u || index < 12u;
    };
    for (uint32_t instruction = draw.transform_program_start;
         instruction < draw.transform_program.size();
         ++instruction) {
        const auto& token = draw.transform_program[instruction];
        const uint32_t mac_opcode = nv2a_vsh::field(token[1], 21, 4);
        const uint32_t ilu_opcode = nv2a_vsh::field(token[1], 25, 3);
        if (mac_opcode > 13u || ilu_opcode > 7u) {
            return false;
        }
        const uint32_t a_type = nv2a_vsh::field(token[2], 26, 2);
        const uint32_t a_register = nv2a_vsh::field(token[2], 28, 4);
        const uint32_t b_type = nv2a_vsh::field(token[2], 11, 2);
        const uint32_t b_register = nv2a_vsh::field(token[2], 13, 4);
        const uint32_t c_type = nv2a_vsh::field(token[3], 28, 2);
        const uint32_t c_register = nv2a_vsh::field(token[2], 0, 2) * 4u
            + nv2a_vsh::field(token[3], 30, 2);
        if (!temporary_source_valid(a_type, a_register)
            || !temporary_source_valid(b_type, b_register)
            || !temporary_source_valid(c_type, c_register)) {
            return false;
        }
        const uint32_t output_mask = nv2a_vsh::field(token[3], 12, 4);
        if (output_mask != 0u) {
            // Context-constant writes are legal NV2A behavior, but the first
            // GPU slice keeps constants immutable. Such programs retain the
            // exact CPU implementation until a writable GPU context exists.
            if (nv2a_vsh::field(token[3], 11, 1) == 0u
                || nv2a_vsh::field(token[3], 3, 8) >= 13u) {
                return false;
            }
        }
        if (nv2a_vsh::field(token[3], 0, 1) != 0u) {
            return true;
        }
    }
    return false;
}


NativeVertexProgramState vertex_program_state_for_draw(
    const NativeDraw& draw,
    VkExtent2D target_extent,
    bool enable_gpu_program) {
    NativeVertexProgramState state{};
    state.enabled = enable_gpu_program && gpu_vertex_program_compatible(draw)
        ? 1u
        : 0u;
    state.transform_program_start = draw.transform_program_start;
    state.target_width = target_extent.width;
    state.target_height = target_extent.height;
    state.specular_enable = draw.specular_enable;
    state.fog_mode = draw.fog_mode;
    state.fog_enable = draw.fog_enable;
    state.fog_parameter0 = draw.fog_params[0];
    state.fog_parameter1 = draw.fog_params[1];
    state.texture_output = 9u + draw.texture_stage;
    state.raw_attribute_fetch = draw.gpu_raw_attribute_fetch ? 1u : 0u;
    state.raw_source_index_base = draw.gpu_raw_source_index_base;
    for (uint32_t slot = 0u; slot < 16u; ++slot) {
        const uint32_t row = slot / 4u;
        const uint32_t lane = slot % 4u;
        state.raw_attribute_formats[row][lane] = draw.vertex_formats[slot];
        state.raw_attribute_base_offsets[row][lane] =
            draw.gpu_raw_attribute_base_offsets[slot];
        for (uint32_t component = 0u; component < 4u; ++component) {
            std::memcpy(
                &state.current_vertex_attributes[slot][component],
                &draw.current_vertex_attributes[slot][component],
                sizeof(uint32_t));
        }
    }
    for (uint32_t stage = 0u; stage < draw.texture_formats.size(); ++stage) {
        const uint32_t format = draw.texture_formats[stage];
        const auto [width, height] = nv2a_texture_extent(
            format,
            draw.texture_image_rects[stage]);
        state.texture_linears[stage] =
            nv2a_texture_format_is_linear(format) ? 1u : 0u;
        state.texture_widths[stage] = width;
        state.texture_heights[stage] = height;
        if (stage == draw.texture_stage) {
            state.texture_linear = state.texture_linears[stage];
            state.texture_width = width;
            state.texture_height = height;
        }
    }
    if (state.enabled != 0u) {
        state.transform_program = draw.transform_program;
        state.transform_constants = draw.transform_constants;
    }
    return state;
}


VkCompareOp nv2a_depth_compare_op(uint32_t function) {
    switch (function) {
    case 0x0200u: return VK_COMPARE_OP_NEVER;
    case 0x0201u: return VK_COMPARE_OP_LESS;
    case 0x0202u: return VK_COMPARE_OP_EQUAL;
    case 0x0203u: return VK_COMPARE_OP_LESS_OR_EQUAL;
    case 0x0204u: return VK_COMPARE_OP_GREATER;
    case 0x0205u: return VK_COMPARE_OP_NOT_EQUAL;
    case 0x0206u: return VK_COMPARE_OP_GREATER_OR_EQUAL;
    case 0x0207u: return VK_COMPARE_OP_ALWAYS;
    default: return VK_COMPARE_OP_LESS_OR_EQUAL;
    }
}


VkCullModeFlags nv2a_cull_mode(uint32_t enabled, uint32_t face) {
    if (!enabled) {
        return VK_CULL_MODE_NONE;
    }
    switch (face) {
    case 0x0404u: return VK_CULL_MODE_FRONT_BIT;
    case 0x0405u: return VK_CULL_MODE_BACK_BIT;
    case 0x0408u: return VK_CULL_MODE_FRONT_AND_BACK;
    default: return VK_CULL_MODE_BACK_BIT;
    }
}


VkFrontFace nv2a_front_face(uint32_t face) {
    // Guest screen-space coordinates and the positive-height Vulkan viewport
    // both use a top-left origin, so preserve the NV2A winding declaration.
    return face == 0x0900u
        ? VK_FRONT_FACE_CLOCKWISE
        : VK_FRONT_FACE_COUNTER_CLOCKWISE;
}


VkBlendFactor nv2a_blend_factor(
    uint32_t factor,
    VkBlendFactor fallback) {
    switch (factor) {
    case 0u: return VK_BLEND_FACTOR_ZERO;
    case 1u: return VK_BLEND_FACTOR_ONE;
    case 0x0300u: return VK_BLEND_FACTOR_SRC_COLOR;
    case 0x0301u: return VK_BLEND_FACTOR_ONE_MINUS_SRC_COLOR;
    case 0x0302u: return VK_BLEND_FACTOR_SRC_ALPHA;
    case 0x0303u: return VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA;
    case 0x0304u: return VK_BLEND_FACTOR_DST_ALPHA;
    case 0x0305u: return VK_BLEND_FACTOR_ONE_MINUS_DST_ALPHA;
    case 0x0306u: return VK_BLEND_FACTOR_DST_COLOR;
    case 0x0307u: return VK_BLEND_FACTOR_ONE_MINUS_DST_COLOR;
    case 0x0308u: return VK_BLEND_FACTOR_SRC_ALPHA_SATURATE;
    default: return fallback;
    }
}


VkBlendOp nv2a_blend_op(uint32_t equation) {
    switch (equation) {
    case 0x8006u: return VK_BLEND_OP_ADD;
    case 0x8007u: return VK_BLEND_OP_MIN;
    case 0x8008u: return VK_BLEND_OP_MAX;
    case 0x800Au: return VK_BLEND_OP_SUBTRACT;
    case 0x800Bu: return VK_BLEND_OP_REVERSE_SUBTRACT;
    default: return VK_BLEND_OP_ADD;
    }
}


VkColorComponentFlags nv2a_color_write_mask(uint32_t mask) {
    VkColorComponentFlags result = 0;
    if (mask & 0x00000001u) result |= VK_COLOR_COMPONENT_B_BIT;
    if (mask & 0x00000100u) result |= VK_COLOR_COMPONENT_G_BIT;
    if (mask & 0x00010000u) result |= VK_COLOR_COMPONENT_R_BIT;
    if (mask & 0x01000000u) result |= VK_COLOR_COMPONENT_A_BIT;
    return result;
}


std::vector<uint32_t> referenced_vertex_program_constants(
    const NativeDraw& draw) {
    std::array<bool, 192> referenced{};
    const auto mark_source = [&](uint32_t source_type, uint32_t constant_index) {
        if (source_type == 3u && constant_index < referenced.size()) {
            referenced[constant_index] = true;
        }
    };
    for (uint32_t instruction = draw.transform_program_start;
         instruction < draw.transform_program.size();
         ++instruction) {
        const auto& token = draw.transform_program[instruction];
        const uint32_t mac_opcode = nv2a_vsh::field(token[1], 21, 4);
        const uint32_t ilu_opcode = nv2a_vsh::field(token[1], 25, 3);
        const uint32_t constant_index = nv2a_vsh::field(token[1], 13, 8);
        const uint32_t a_type = nv2a_vsh::field(token[2], 26, 2);
        const uint32_t b_type = nv2a_vsh::field(token[2], 11, 2);
        const uint32_t c_type = nv2a_vsh::field(token[3], 28, 2);
        if (mac_opcode != 0u) {
            mark_source(a_type, constant_index);
        }
        if (mac_opcode == 2u || (mac_opcode >= 4u && mac_opcode <= 12u)) {
            mark_source(b_type, constant_index);
        }
        if (mac_opcode == 3u || mac_opcode == 4u || ilu_opcode != 0u) {
            mark_source(c_type, constant_index);
        }
        if ((token[3] & 1u) != 0u) {
            break;
        }
    }
    std::vector<uint32_t> result;
    for (uint32_t index = 0; index < referenced.size(); ++index) {
        if (referenced[index]) {
            result.push_back(index);
        }
    }
    return result;
}


float nv2a_programmable_fog_factor(
    const NativeDraw& draw,
    float fog_distance) {
    if (draw.fog_enable == 0u
        || (draw.fog_params[0] == 0u && draw.fog_params[1] == 0u)) {
        return 1.0f;
    }
    float parameter0 = 0.0f;
    float parameter1 = 0.0f;
    std::memcpy(&parameter0, &draw.fog_params[0], sizeof(parameter0));
    std::memcpy(&parameter1, &draw.fog_params[1], sizeof(parameter1));
    if (!std::isfinite(fog_distance)
        || !std::isfinite(parameter0)
        || !std::isfinite(parameter1)) {
        return 1.0f;
    }
    float factor = 1.0f;
    switch (draw.fog_mode) {
    case 0x2601u:
    case 0x0804u:
        factor = parameter0 + fog_distance * parameter1 - 1.0f;
        break;
    case 0x0800u:
    case 0x0802u:
        factor = parameter0
            + std::exp2(fog_distance * parameter1 * 16.0f) - 1.5f;
        break;
    case 0x0801u:
    case 0x0803u:
        factor = parameter0
            + std::exp2(
                -fog_distance * fog_distance
                * parameter1 * parameter1 * 32.0f)
            - 1.5f;
        break;
    default:
        return 1.0f;
    }
    if (draw.fog_mode == 0x0802u
        || draw.fog_mode == 0x0803u
        || draw.fog_mode == 0x0804u) {
        factor = std::abs(factor);
    }
    return std::isfinite(factor) ? factor : 1.0f;
}


uint32_t execute_presented_vertex_program(
    std::vector<NativeVertex>& vertices,
    const NativeDraw& draw,
    PresentedVertexTransformDiagnostics* diagnostics) {
    if ((draw.transform_execution_mode & 3u) != 2u
        || draw.first_vertex + draw.vertex_count > vertices.size()) {
        return 0;
    }
    PresentedVertexTransformDiagnostics local{};
    local.eligible_vertex_count = draw.vertex_count;
    uint32_t transformed = 0;
    for (uint32_t index = 0; index < draw.vertex_count; ++index) {
        NativeVertex& vertex = vertices[draw.first_vertex + index];
        local.input_position_bounds.include(vertex.x, vertex.y);
        local.input_z_bounds.include(vertex.z);
        local.input_w_bounds.include(vertex.w);
        std::array<std::array<float, 4>, 16> inputs = vertex.program_inputs;
        if (!vertex.program_inputs_valid) {
            for (auto& input : inputs) input[3] = 1.0f;
            inputs[0] = {vertex.x, vertex.y, vertex.z, vertex.w};
            inputs[3] = {vertex.r, vertex.g, vertex.b, vertex.a};
            inputs[4] = {
                vertex.secondary_r,
                vertex.secondary_g,
                vertex.secondary_b,
                vertex.secondary_a,
            };
            const uint32_t texture_input = std::min<uint32_t>(
                9u + draw.texture_stage, 15u);
            inputs[texture_input] = {
                vertex.u,
                vertex.v,
                vertex.texture_r,
                vertex.texture_q,
            };
        }
        const Nv2aVertexProgramResult result = execute_nv2a_vertex_program(
            draw.transform_program,
            draw.transform_constants,
            draw.transform_program_start,
            inputs);
        if (!result.valid) {
            ++local.invalid_program_vertex_count;
            continue;
        }
        ++local.valid_program_vertex_count;
        const auto& position = result.outputs[0];
        local.position_output_mask_union |= result.output_masks[0];
        const bool position_xyz_written =
            (result.output_masks[0] & 14u) == 14u;
        const bool position_w_written =
            (result.output_masks[0] & 1u) != 0u;
        const bool position_w_usable = position_w_written
            && std::isfinite(position[3])
            && std::abs(position[3]) > 0.000001f;
        if ((result.output_masks[0] & 12u) == 12u
            && std::isfinite(position[0]) && std::isfinite(position[1])) {
            ++local.position_output_vertex_count;
            local.clip_position_bounds.include(position[0], position[1]);
            if ((result.output_masks[0] & 2u) != 0u) {
                local.program_output_z_bounds.include(position[2]);
            }
            if (position_w_written) {
                local.program_output_w_bounds.include(position[3]);
                if (!std::isfinite(position[3])) {
                    ++local.non_finite_position_w_vertex_count;
                }
            }
            if (position_w_written && std::isfinite(position[3])) {
                ++local.homogeneous_position_vertex_count;
                if (std::abs(position[3]) <= 0.000001f) {
                    ++local.near_zero_w_vertex_count;
                }
            }
            // NV2A vertex programs leave oPos in screen space. The host's
            // final upload pass converts these coordinates to Vulkan NDC.
            ++local.screen_space_position_vertex_count;
            vertex.x = position[0];
            vertex.y = position[1];
            local.output_pixel_bounds.include(vertex.x, vertex.y);
            if ((result.output_masks[0] & 2u) != 0u) {
                float viewport_depth_scale = 0.0f;
                const uint32_t viewport_depth_scale_bits =
                    draw.transform_constants[58][2];
                std::memcpy(
                    &viewport_depth_scale,
                    &viewport_depth_scale_bits,
                    sizeof(viewport_depth_scale));
                vertex.z = std::isfinite(viewport_depth_scale)
                    && std::abs(viewport_depth_scale) > 0.000001f
                    ? position[2] / viewport_depth_scale
                    : position[2];
            }
            if (position_w_usable) {
                vertex.w = position[3];
            } else if (position_xyz_written) {
                // NV2A programs used by the title can emit screen-space
                // oPos.xyz with an absent or unusable oPos.w. Complete the
                // homogeneous component instead of rejecting the valid xyz
                // output and forcing the fallback near depth.
                vertex.w = 1.0f;
                ++local.defaulted_position_w_vertex_count;
                if (position_w_written) {
                    ++local.invalid_position_w_vertex_count;
                } else {
                    ++local.implicit_position_w_vertex_count;
                }
            }
            vertex.program_position_valid =
                position_xyz_written
                && std::isfinite(vertex.z)
                && std::isfinite(vertex.w);
        } else if ((result.output_masks[0] & 12u) != 0u) {
            ++local.non_finite_position_vertex_count;
        }
        vertex.r = 0.0f;
        vertex.g = 0.0f;
        vertex.b = 0.0f;
        vertex.a = 1.0f;
        if (result.output_masks[3]) {
            ++local.diffuse_output_vertex_count;
            const auto& diffuse = result.outputs[3];
            if (result.output_masks[3] & 8u) vertex.r = diffuse[0];
            if (result.output_masks[3] & 4u) vertex.g = diffuse[1];
            if (result.output_masks[3] & 2u) vertex.b = diffuse[2];
            if (result.output_masks[3] & 1u) vertex.a = diffuse[3];
        }
        vertex.secondary_r = 0.0f;
        vertex.secondary_g = 0.0f;
        vertex.secondary_b = 0.0f;
        vertex.secondary_a = 1.0f;
        if (draw.specular_enable != 0u && result.output_masks[4]) {
            const auto& secondary = result.outputs[4];
            if (result.output_masks[4] & 8u) {
                vertex.secondary_r = secondary[0];
            }
            if (result.output_masks[4] & 4u) {
                vertex.secondary_g = secondary[1];
            }
            if (result.output_masks[4] & 2u) {
                vertex.secondary_b = secondary[2];
            }
            if (result.output_masks[4] & 1u) {
                vertex.secondary_a = secondary[3];
            }
        }
        if (draw.fog_enable != 0u) {
            const uint32_t fog_mask = result.output_masks[5];
            const auto& fog_output = result.outputs[5];
            const float fog_distance = (fog_mask & 8u) != 0u
                ? fog_output[0]
                : (fog_mask & 4u) != 0u
                    ? fog_output[1]
                    : (fog_mask & 2u) != 0u
                        ? fog_output[2]
                        : (fog_mask & 1u) != 0u
                            ? fog_output[3]
                            : 0.0f;
            vertex.fog = nv2a_programmable_fog_factor(
                draw, fog_distance);
        }
        for (uint32_t stage = 0u; stage < 4u; ++stage) {
            const uint32_t texture_output = 9u + stage;
            auto& coordinates = vertex.texture_coordinates[stage];
            coordinates = vertex.program_inputs[texture_output];
            if (result.output_masks[texture_output]) {
                ++local.texture_output_vertex_count;
                nv2a_vsh::write_mask(
                    coordinates,
                    result.outputs[texture_output],
                    result.output_masks[texture_output]);
            }
        }
        const auto& texture = vertex.texture_coordinates[
            std::min<uint32_t>(draw.texture_stage, 3u)];
        vertex.u = texture[0];
        vertex.v = texture[1];
        vertex.texture_r = texture[2];
        vertex.texture_q = texture[3];
        local.include_output_color(vertex.r, vertex.g, vertex.b, vertex.a);
        ++transformed;
    }
    if (diagnostics != nullptr) {
        diagnostics->merge(local);
    }
    return transformed;
}


bool fixed_function_composite_matrix_valid(const NativeDraw& draw) {
    if (draw.skin_mode != 0u) {
        return false;
    }
    bool any_non_zero = false;
    for (uint32_t row = 0; row < 4u; ++row) {
        for (uint32_t lane = 0; lane < 4u; ++lane) {
            float value = 0.0f;
            const uint32_t bits = draw.transform_constants[row][lane];
            std::memcpy(&value, &bits, sizeof(value));
            if (!std::isfinite(value)) {
                return false;
            }
            any_non_zero |= value != 0.0f;
        }
    }
    return any_non_zero;
}


uint32_t execute_presented_fixed_function_transform(
    std::vector<NativeVertex>& vertices,
    const NativeDraw& draw,
    PresentedVertexTransformDiagnostics* diagnostics) {
    if ((draw.transform_execution_mode & 3u) != 0u
        || draw.first_vertex + draw.vertex_count > vertices.size()) {
        return 0u;
    }
    PresentedVertexTransformDiagnostics local{};
    local.eligible_vertex_count = draw.vertex_count;
    local.fixed_function_vertex_count = draw.vertex_count;
    if (!fixed_function_composite_matrix_valid(draw)) {
        local.invalid_fixed_function_vertex_count = draw.vertex_count;
        local.raw_position_fallback_vertex_count = draw.vertex_count;
        if (diagnostics != nullptr) {
            diagnostics->merge(local);
        }
        return 0u;
    }
    auto constant = [&](uint32_t row, uint32_t lane) {
        float value = 0.0f;
        const uint32_t bits = draw.transform_constants[row][lane];
        std::memcpy(&value, &bits, sizeof(value));
        return value;
    };
    const float viewport_offset_x = constant(59u, 0u);
    const float viewport_offset_y = constant(59u, 1u);
    const float viewport_depth_scale = constant(58u, 2u);
    constexpr float minimum_w = 0x1p-64f;
    constexpr float maximum_w = 0x1p64f;
    uint32_t transformed = 0u;
    for (uint32_t index = 0; index < draw.vertex_count; ++index) {
        NativeVertex& vertex = vertices[draw.first_vertex + index];
        local.input_position_bounds.include(vertex.x, vertex.y);
        local.input_z_bounds.include(vertex.z);
        local.input_w_bounds.include(vertex.w);
        const std::array<float, 4> position{
            vertex.x,
            vertex.y,
            vertex.z,
            vertex.w,
        };
        std::array<float, 4> clip{};
        for (uint32_t output = 0u; output < 4u; ++output) {
            for (uint32_t input = 0u; input < 4u; ++input) {
                clip[output] += position[input] * constant(output, input);
            }
        }
        if (!std::all_of(
                clip.begin(), clip.end(),
                [](float value) { return std::isfinite(value); })) {
            ++local.invalid_fixed_function_vertex_count;
            ++local.raw_position_fallback_vertex_count;
            continue;
        }
        float homogeneous_w = clip[3];
        if (homogeneous_w >= 0.0f) {
            homogeneous_w = std::clamp(
                homogeneous_w, minimum_w, maximum_w);
        } else {
            homogeneous_w = std::clamp(
                homogeneous_w, -maximum_w, -minimum_w);
        }
        const float screen_x = std::trunc(
            (clip[0] / homogeneous_w + viewport_offset_x) * 16.0f) / 16.0f;
        const float screen_y = std::trunc(
            (clip[1] / homogeneous_w + viewport_offset_y) * 16.0f) / 16.0f;
        if (!std::isfinite(screen_x) || !std::isfinite(screen_y)) {
            ++local.invalid_fixed_function_vertex_count;
            ++local.raw_position_fallback_vertex_count;
            continue;
        }
        local.clip_position_bounds.include(clip[0], clip[1]);
        local.program_output_z_bounds.include(clip[2]);
        local.program_output_w_bounds.include(homogeneous_w);
        local.output_pixel_bounds.include(screen_x, screen_y);
        ++local.position_output_vertex_count;
        ++local.homogeneous_position_vertex_count;
        ++local.screen_space_position_vertex_count;
        ++local.fixed_function_transformed_vertex_count;
        vertex.x = screen_x;
        vertex.y = screen_y;
        vertex.w = homogeneous_w;
        const float normalized_depth =
            std::isfinite(viewport_depth_scale)
                && std::abs(viewport_depth_scale) > 0.000001f
            ? clip[2] / viewport_depth_scale
            : clip[2];
        vertex.z = normalized_depth / homogeneous_w;
        vertex.program_position_valid = std::isfinite(vertex.z);
        local.include_output_color(vertex.r, vertex.g, vertex.b, vertex.a);
        ++transformed;
    }
    if (diagnostics != nullptr) {
        diagnostics->merge(local);
    }
    return transformed;
}


uint32_t normalize_presented_linear_texture_coordinates(
    std::vector<NativeVertex>& vertices,
    const NativeDraw& draw) {
    if (!draw.texture_enabled
        || draw.texture_stage >= draw.texture_formats.size()
        || draw.first_vertex + draw.vertex_count > vertices.size()) {
        return 0u;
    }
    bool normalized = false;
    for (uint32_t stage = 0u; stage < draw.texture_formats.size(); ++stage) {
        const uint32_t format_raw = draw.texture_formats[stage];
        if ((draw.texture_controls[stage] & (1u << 30u)) == 0u
            || !nv2a_texture_format_is_linear(format_raw)) {
            continue;
        }
        const auto [width, height] = nv2a_texture_extent(
            format_raw,
            draw.texture_image_rects[stage]);
        if (width == 0u || height == 0u) {
            continue;
        }
        for (uint32_t index = 0; index < draw.vertex_count; ++index) {
            NativeVertex& vertex = vertices[draw.first_vertex + index];
            auto& coordinates = vertex.texture_coordinates[stage];
            coordinates[0] /= static_cast<float>(width);
            coordinates[1] /= static_cast<float>(height);
            if (stage == draw.texture_stage) {
                vertex.u = coordinates[0];
                vertex.v = coordinates[1];
            }
        }
        normalized = true;
    }
    return normalized ? draw.vertex_count : 0u;
}


std::pair<size_t, size_t> presented_vertex_span(
    const InterpretedD3DStream& stream) {
    const size_t first_draw = std::min<size_t>(
        stream.presented_draw_begin,
        stream.draws.size());
    const size_t end_draw = std::min<size_t>(
        first_draw + stream.presented_draw_count,
        stream.draws.size());
    size_t first_vertex = stream.vertices.size();
    size_t end_vertex = 0;
    for (size_t draw_index = first_draw; draw_index < end_draw; ++draw_index) {
        const NativeDraw& draw = stream.draws[draw_index];
        if (draw.gpu_raw_attribute_fetch) {
            continue;
        }
        const size_t draw_first = draw.first_vertex;
        const size_t draw_end = draw_first + draw.vertex_count;
        if (draw.vertex_count == 0u || draw_end > stream.vertices.size()) {
            continue;
        }
        first_vertex = std::min(first_vertex, draw_first);
        end_vertex = std::max(end_vertex, draw_end);
    }
    if (first_vertex >= end_vertex) {
        return {0u, 0u};
    }
    return {first_vertex, end_vertex};
}


bool recover_presented_half_surface_quad(
    std::vector<NativeVertex>& vertices,
    const NativeDraw& draw,
    uint32_t output_width,
    uint32_t output_height,
    bool& overscan_height_recovered) {
    if (draw.primitive != 6u || draw.vertex_count != 4u || draw.texture_address == 0u ||
        draw.first_vertex + draw.vertex_count > vertices.size()) {
        return false;
    }
    float min_x = vertices[draw.first_vertex].x;
    float max_x = min_x;
    float min_u = vertices[draw.first_vertex].u;
    float max_u = min_u;
    float min_v = vertices[draw.first_vertex].v;
    float max_v = min_v;
    float min_y = vertices[draw.first_vertex].y;
    float max_y = min_y;
    for (uint32_t index = 1; index < draw.vertex_count; ++index) {
        const NativeVertex& vertex = vertices[draw.first_vertex + index];
        min_x = std::min(min_x, vertex.x);
        max_x = std::max(max_x, vertex.x);
        min_u = std::min(min_u, vertex.u);
        max_u = std::max(max_u, vertex.u);
        min_v = std::min(min_v, vertex.v);
        max_v = std::max(max_v, vertex.v);
        min_y = std::min(min_y, vertex.y);
        max_y = std::max(max_y, vertex.y);
    }
    const auto nearly_equal = [](float lhs, float rhs) { return std::abs(lhs - rhs) <= 0.0001f; };
    if (!nearly_equal(min_x, 0.0f) ||
        !nearly_equal(max_x, static_cast<float>(output_width) * 0.5f) ||
        !nearly_equal(min_u, 0.5f) || !nearly_equal(max_u, 1.0f) ||
        !nearly_equal(min_v, 0.0f) || !nearly_equal(max_v, 1.0f)) {
        return false;
    }
    for (uint32_t index = 0; index < draw.vertex_count; ++index) {
        NativeVertex& vertex = vertices[draw.first_vertex + index];
        vertex.x *= 2.0f;
        vertex.u = (vertex.u - 0.5f) * 2.0f;
    }
    if (output_height == 480u && nearly_equal(min_y, 0.0f) && nearly_equal(max_y, 448.0f)) {
        const float height_scale = static_cast<float>(output_height) / max_y;
        for (uint32_t index = 0; index < draw.vertex_count; ++index) {
            vertices[draw.first_vertex + index].y *= height_scale;
        }
        overscan_height_recovered = true;
    }
    return true;
}


std::vector<RecoveredD3DCommand> build_recovered_d3d_command_stream() {
    return {
        {RecoveredD3DCommandKind::PushBufferWrite, 0x80000080u, 0x00041810u, 4},
        {RecoveredD3DCommandKind::PushBufferWrite, 0x80000084u, 0x804020FFu, 4},
        {RecoveredD3DCommandKind::MmioWrite, 0xFED00048u, 0x00001200u},
        {RecoveredD3DCommandKind::MmioWrite, 0xFED0004Cu, 0x00000000u},
        {RecoveredD3DCommandKind::MmioWrite, 0xFED00050u, 0x80000000u},
        {RecoveredD3DCommandKind::MmioWrite, 0xFED00008u, 0x00000001u},
    };
}


uint32_t parse_json_u32_text(const std::string& value, const char* label) {
    size_t consumed = 0;
    const unsigned long long parsed = std::stoull(value, &consumed, 0);
    if (consumed != value.size() || parsed > UINT32_MAX) {
        throw std::runtime_error(std::string("invalid render stream ") + label + ": " + value);
    }
    return static_cast<uint32_t>(parsed);
}


uint64_t parse_json_u64_text(const std::string& value, const char* label) {
    size_t consumed = 0;
    const unsigned long long parsed = std::stoull(value, &consumed, 0);
    if (consumed != value.size()) {
        throw std::runtime_error(std::string("invalid render stream ") + label + ": " + value);
    }
    return static_cast<uint64_t>(parsed);
}


uint32_t parse_json_u32_wrapping_text(const std::string& value, const char* label) {
    size_t consumed = 0;
    const unsigned long long parsed = std::stoull(value, &consumed, 0);
    if (consumed != value.size()) {
        throw std::runtime_error(std::string("invalid render stream ") + label + ": " + value);
    }
    return static_cast<uint32_t>(parsed & UINT32_MAX);
}


uint8_t parse_hex_byte(const std::string& value, size_t offset) {
    const auto nibble = [&](char ch) -> uint8_t {
        if (ch >= '0' && ch <= '9') {
            return static_cast<uint8_t>(ch - '0');
        }
        if (ch >= 'a' && ch <= 'f') {
            return static_cast<uint8_t>(10 + ch - 'a');
        }
        if (ch >= 'A' && ch <= 'F') {
            return static_cast<uint8_t>(10 + ch - 'A');
        }
        throw std::runtime_error("invalid render stream bytes_hex digit");
    };
    return static_cast<uint8_t>((nibble(value[offset]) << 4u) | nibble(value[offset + 1u]));
}


std::vector<uint8_t> parse_json_bytes_hex(const std::string& value) {
    std::string compact;
    compact.reserve(value.size());
    for (const unsigned char ch : value) {
        if (std::isspace(ch)) {
            continue;
        }
        compact.push_back(static_cast<char>(ch));
    }
    if (compact.size() % 2u != 0u) {
        throw std::runtime_error("invalid render stream bytes_hex length");
    }
    std::vector<uint8_t> bytes;
    bytes.reserve(compact.size() / 2u);
    for (size_t offset = 0; offset < compact.size(); offset += 2u) {
        bytes.push_back(parse_hex_byte(compact, offset));
    }
    return bytes;
}


std::vector<uint8_t> payload_from_value(uint32_t value, uint32_t size) {
    const uint32_t byte_count = std::max(1u, std::min(size, 4u));
    std::vector<uint8_t> payload;
    payload.reserve(byte_count);
    for (uint32_t byte_index = 0; byte_index < byte_count; ++byte_index) {
        payload.push_back(static_cast<uint8_t>((value >> (byte_index * 8u)) & 0xFFu));
    }
    return payload;
}


RecoveredD3DCommand make_recovered_d3d_command(
    RecoveredD3DCommandKind kind,
    uint32_t address,
    uint32_t value,
    uint32_t size,
    const uint8_t* payload,
    size_t payload_size) {
    RecoveredD3DCommand command{kind, address, value, size};
    if (payload_size > command.payload.size()) {
        throw std::runtime_error("render command payload exceeds the 8-byte stream record");
    }
    if (payload_size != 0u) {
        std::memcpy(command.payload.data(), payload, payload_size);
        command.payload_size = static_cast<uint8_t>(payload_size);
    }
    return command;
}


RecoveredD3DCommand recovered_d3d_command_from_packed_record(
    const uint8_t* record) {
    RecoveredD3DCommand command{};
    command.kind = record[0] == 0u
        ? RecoveredD3DCommandKind::MmioWrite
        : RecoveredD3DCommandKind::PushBufferWrite;
    command.size = record[1];
    command.payload_size = record[1];
    std::memcpy(&command.address, record + 4u, sizeof(command.address));
    std::memcpy(
        &command.value,
        record + 8u,
        std::min<size_t>(command.size, sizeof(command.value)));
    std::memcpy(command.payload.data(), record + 8u, command.payload_size);
    return command;
}


std::optional<std::string> json_object_field_text(
    const std::string& object,
    const std::string& field) {
    const std::string needle = "\"" + field + "\"";
    size_t cursor = object.find(needle);
    if (cursor == std::string::npos) {
        return std::nullopt;
    }
    cursor = object.find(':', cursor + needle.size());
    if (cursor == std::string::npos) {
        return std::nullopt;
    }
    ++cursor;
    while (cursor < object.size() && std::isspace(static_cast<unsigned char>(object[cursor]))) {
        ++cursor;
    }
    if (cursor >= object.size()) {
        return std::nullopt;
    }
    if (object[cursor] == '"') {
        const size_t start = ++cursor;
        while (cursor < object.size()) {
            if (object[cursor] == '"' && (cursor == start || object[cursor - 1] != '\\')) {
                return object.substr(start, cursor - start);
            }
            ++cursor;
        }
        return std::nullopt;
    }
    const size_t start = cursor;
    if (object[cursor] == '-') {
        ++cursor;
    }
    if (cursor + 1 < object.size() && object[cursor] == '0'
        && (object[cursor + 1] == 'x' || object[cursor + 1] == 'X')) {
        cursor += 2;
        while (cursor < object.size()
            && std::isxdigit(static_cast<unsigned char>(object[cursor]))) {
            ++cursor;
        }
    } else {
        while (cursor < object.size()
            && std::isdigit(static_cast<unsigned char>(object[cursor]))) {
            ++cursor;
        }
    }
    return cursor > start ? std::optional<std::string>(object.substr(start, cursor - start))
                          : std::nullopt;
}


std::vector<RecoveredD3DCommand> load_recovered_d3d_command_stream(
    const std::filesystem::path& path) {
    std::ifstream file(path);
    if (!file) {
        throw std::runtime_error("cannot open render stream JSON: " + path.string());
    }
    std::ostringstream buffer;
    buffer << file.rdbuf();
    const std::string text = buffer.str();
    const std::regex object_regex("\\{[^{}]*\\}");
    std::vector<RecoveredD3DCommand> commands;
    for (std::sregex_iterator it(text.begin(), text.end(), object_regex), end; it != end; ++it) {
        const std::string object = it->str();
        const std::optional<std::string> kind_text = json_object_field_text(object, "kind");
        if (!kind_text.has_value()) {
            continue;
        }
        RecoveredD3DCommandKind kind{};
        if (*kind_text == "d3d_mmio") {
            kind = RecoveredD3DCommandKind::MmioWrite;
        } else if (*kind_text == "d3d_push_buffer") {
            kind = RecoveredD3DCommandKind::PushBufferWrite;
        } else {
            continue;
        }
        std::optional<std::string> address_text = json_object_field_text(object, "address");
        if (!address_text.has_value()) {
            address_text = json_object_field_text(object, "address_hex");
        }
        std::optional<std::string> value_text = json_object_field_text(object, "value");
        if (!value_text.has_value()) {
            value_text = json_object_field_text(object, "value_hex");
        }
        if (!address_text.has_value() || !value_text.has_value()) {
            continue;
        }
        const std::optional<std::string> size_text = json_object_field_text(object, "size");
        const uint32_t size = size_text.has_value() ? parse_json_u32_text(*size_text, "size") : 4u;
        const uint32_t address = parse_json_u32_text(*address_text, "address");
        const uint32_t value = parse_json_u32_wrapping_text(*value_text, "value");
        const std::optional<std::string> bytes_hex_text = json_object_field_text(object, "bytes_hex");
        std::vector<uint8_t> payload =
            bytes_hex_text.has_value() ? parse_json_bytes_hex(*bytes_hex_text) : payload_from_value(value, size);
        commands.push_back(make_recovered_d3d_command(
            kind, address, value, size, payload.data(), payload.size()));
    }
    if (commands.empty()) {
        throw std::runtime_error("render stream JSON contained no D3D MMIO or push-buffer writes: " + path.string());
    }
    return commands;
}


std::vector<RecoveredD3DCommand> load_recovered_d3d_binary_stream(
    const std::filesystem::path& path) {
    std::ifstream file(path, std::ios::binary);
    if (!file) {
        throw std::runtime_error("cannot open live command snapshot: " + path.string());
    }
    std::array<char, 8> magic{};
    file.read(magic.data(), static_cast<std::streamsize>(magic.size()));
    const std::string magic_text(magic.data(), magic.size());
    if (magic_text != "B2RLIVE1" && magic_text != "B2RING01"
        && magic_text != "B2APPND1" && magic_text != "B2SPAN01") {
        throw std::runtime_error("invalid live command snapshot header");
    }
    if (magic_text == "B2SPAN01") {
        std::vector<RecoveredD3DCommand> commands;
        for (;;) {
            std::array<uint8_t, kRecoveredD3DCommandSpanHeaderSize> header{};
            file.read(
                reinterpret_cast<char*>(header.data()),
                static_cast<std::streamsize>(header.size()));
            if (file.eof() && file.gcount() == 0) {
                break;
            }
            if (!file || header[0] > 1u || (header[1] & ~1u) != 0u) {
                throw std::runtime_error("invalid live command span header");
            }
            uint32_t address = 0u;
            uint32_t payload_size = 0u;
            uint32_t logical_write_count = 0u;
            std::memcpy(&address, header.data() + 4u, sizeof(address));
            std::memcpy(&payload_size, header.data() + 8u, sizeof(payload_size));
            std::memcpy(
                &logical_write_count,
                header.data() + 12u,
                sizeof(logical_write_count));
            if (payload_size == 0u || payload_size % sizeof(uint32_t) != 0u
                || logical_write_count != payload_size / sizeof(uint32_t)) {
                throw std::runtime_error("invalid live command span size");
            }
            std::vector<uint8_t> payload(payload_size);
            file.read(
                reinterpret_cast<char*>(payload.data()),
                static_cast<std::streamsize>(payload.size()));
            if (!file) {
                throw std::runtime_error("truncated live command span payload");
            }
            for (size_t offset = 0u;
                 offset < payload.size();
                 offset += sizeof(uint32_t)) {
                const size_t size = sizeof(uint32_t);
                uint32_t value = 0u;
                std::memcpy(
                    &value,
                    payload.data() + offset,
                    std::min<size_t>(size, sizeof(value)));
                commands.push_back(make_recovered_d3d_command(
                    header[0] == 0u
                        ? RecoveredD3DCommandKind::MmioWrite
                        : RecoveredD3DCommandKind::PushBufferWrite,
                    address + static_cast<uint32_t>(offset),
                    value,
                    static_cast<uint32_t>(size),
                    payload.data() + offset,
                    size));
            }
        }
        if (commands.empty()) {
            throw std::runtime_error("live command span snapshot is empty");
        }
        return commands;
    }
    uint32_t count = 0;
    if (magic_text == "B2APPND1") {
        file.seekg(0, std::ios::end);
        const std::streamoff byte_size = file.tellg();
        constexpr std::streamoff header_size = 8;
        constexpr std::streamoff record_size = 16;
        if (byte_size < header_size || (byte_size - header_size) % record_size != 0) {
            throw std::runtime_error("invalid append-only live command stream size");
        }
        count = static_cast<uint32_t>((byte_size - header_size) / record_size);
        file.seekg(header_size, std::ios::beg);
    } else {
        file.read(reinterpret_cast<char*>(&count), sizeof(count));
    }
    std::vector<RecoveredD3DCommand> commands;
    commands.reserve(count);
    for (uint32_t index = 0; index < count; ++index) {
        uint8_t kind_raw = 0;
        uint8_t size = 0;
        uint16_t reserved = 0;
        uint32_t address = 0;
        std::array<uint8_t, 8> bytes{};
        file.read(reinterpret_cast<char*>(&kind_raw), sizeof(kind_raw));
        file.read(reinterpret_cast<char*>(&size), sizeof(size));
        file.read(reinterpret_cast<char*>(&reserved), sizeof(reserved));
        file.read(reinterpret_cast<char*>(&address), sizeof(address));
        file.read(reinterpret_cast<char*>(bytes.data()), static_cast<std::streamsize>(bytes.size()));
        if (!file || size == 0 || size > bytes.size() || kind_raw > 1u) {
            throw std::runtime_error("invalid live command snapshot record");
        }
        uint32_t value = 0;
        std::memcpy(&value, bytes.data(), std::min<size_t>(size, sizeof(value)));
        commands.push_back(make_recovered_d3d_command(
            kind_raw == 0u ? RecoveredD3DCommandKind::MmioWrite
                           : RecoveredD3DCommandKind::PushBufferWrite,
            address,
            value,
            size,
            bytes.data(),
            size));
    }
    if (magic_text == "B2RING01") {
        std::array<uint8_t, 0x10000> valid{};
        std::array<uint8_t, 0x10000> ring{};
        file.read(reinterpret_cast<char*>(valid.data()), static_cast<std::streamsize>(valid.size()));
        file.read(reinterpret_cast<char*>(ring.data()), static_cast<std::streamsize>(ring.size()));
        if (!file) {
            throw std::runtime_error("truncated live push-buffer ring snapshot");
        }
        size_t offset = 0;
        while (offset < valid.size()) {
            while (offset < valid.size() && valid[offset] == 0u) ++offset;
            if (offset >= valid.size()) break;
            const size_t start = offset;
            while (offset < valid.size() && valid[offset] != 0u && offset - start < 8u) ++offset;
            const uint8_t size = static_cast<uint8_t>(offset - start);
            uint32_t value = 0;
            std::memcpy(&value, ring.data() + start, std::min<size_t>(size, sizeof(value)));
            commands.push_back(make_recovered_d3d_command(
                RecoveredD3DCommandKind::PushBufferWrite,
                static_cast<uint32_t>(0x80000000u + start),
                value,
                size,
                ring.data() + start,
                size));
        }
    }
    return commands;
}


std::vector<RecoveredTextureResource> load_recovered_texture_resources_stream(
    std::istream& file,
    std::vector<RecoveredTextureResource>* reusable_resources,
    uint64_t* reused_resource_count,
    uint64_t* reused_payload_bytes) {
    if (reused_resource_count != nullptr) {
        *reused_resource_count = 0u;
    }
    if (reused_payload_bytes != nullptr) {
        *reused_payload_bytes = 0u;
    }
    std::array<char, 8> magic{};
    file.read(magic.data(), static_cast<std::streamsize>(magic.size()));
    if (file && std::string(magic.data(), magic.size()) == "B2TEX001") {
        const std::streampos body_begin = file.tellg();
        file.seekg(0, std::ios::end);
        const std::streampos file_end = file.tellg();
        file.seekg(body_begin);
        if (!file || body_begin < 0 || file_end < body_begin) {
            throw std::runtime_error("invalid binary live texture resource size");
        }
        std::unordered_map<uint32_t, std::vector<size_t>> reusable_by_address;
        std::vector<bool> reusable_claimed;
        if (reusable_resources != nullptr) {
            reusable_claimed.resize(reusable_resources->size());
            for (size_t index = 0u; index < reusable_resources->size(); ++index) {
                reusable_by_address[(*reusable_resources)[index].address]
                    .push_back(index);
            }
        }
        uint32_t resource_count = 0;
        file.read(reinterpret_cast<char*>(&resource_count), sizeof(resource_count));
        std::vector<RecoveredTextureResource> resources;
        resources.reserve(resource_count);
        for (uint32_t index = 0; index < resource_count; ++index) {
            uint32_t stage = 0;
            uint32_t address = 0;
            uint32_t width = 0;
            uint32_t height = 0;
            uint32_t format_size = 0;
            uint32_t payload_size = 0;
            std::array<uint8_t, 32> content_hash{};
            file.read(reinterpret_cast<char*>(&stage), sizeof(stage));
            file.read(reinterpret_cast<char*>(&address), sizeof(address));
            file.read(reinterpret_cast<char*>(&width), sizeof(width));
            file.read(reinterpret_cast<char*>(&height), sizeof(height));
            file.read(reinterpret_cast<char*>(&format_size), sizeof(format_size));
            file.read(reinterpret_cast<char*>(&payload_size), sizeof(payload_size));
            file.read(
                reinterpret_cast<char*>(content_hash.data()),
                static_cast<std::streamsize>(content_hash.size()));
            if (!file || format_size == 0u || format_size > 32u
                || payload_size > 256u * 1024u * 1024u) {
                throw std::runtime_error("invalid binary live texture resource header");
            }
            std::string format(format_size, '\0');
            file.read(format.data(), static_cast<std::streamsize>(format_size));
            if (!file) {
                throw std::runtime_error("truncated binary live texture resource");
            }
            const std::streampos payload_begin = file.tellg();
            if (payload_begin < 0
                || static_cast<uint64_t>(file_end - payload_begin)
                    < payload_size) {
                throw std::runtime_error("truncated binary live texture resource");
            }
            constexpr char kHexDigits[] = "0123456789ABCDEF";
            std::string hash(content_hash.size() * 2u, '0');
            for (size_t byte_index = 0u;
                 byte_index < content_hash.size();
                 ++byte_index) {
                const uint8_t byte = content_hash[byte_index];
                hash[byte_index * 2u] = kHexDigits[byte >> 4u];
                hash[byte_index * 2u + 1u] = kHexDigits[byte & 0xFu];
            }

            RecoveredTextureResource resource{};
            resource.address = address;
            resource.width = width;
            resource.height = height;
            resource.format = std::move(format);
            resource.content_hash = std::move(hash);
            bool reused_payload = false;
            const auto candidates = reusable_by_address.find(address);
            if (candidates != reusable_by_address.end()) {
                for (const size_t candidate_index : candidates->second) {
                    RecoveredTextureResource& candidate =
                        (*reusable_resources)[candidate_index];
                    if (reusable_claimed[candidate_index]
                        || candidate.width != width
                        || candidate.height != height
                        || candidate.format != resource.format
                        || candidate.content_hash != resource.content_hash
                        || candidate.payload.size() != payload_size) {
                        continue;
                    }
                    file.seekg(
                        static_cast<std::streamoff>(payload_size),
                        std::ios::cur);
                    if (!file) {
                        throw std::runtime_error(
                            "truncated binary live texture resource");
                    }
                    resource.payload = std::move(candidate.payload);
                    reusable_claimed[candidate_index] = true;
                    reused_payload = true;
                    if (reused_resource_count != nullptr) {
                        ++*reused_resource_count;
                    }
                    if (reused_payload_bytes != nullptr) {
                        *reused_payload_bytes += payload_size;
                    }
                    break;
                }
            }
            if (!reused_payload) {
                resource.payload.resize(payload_size);
                file.read(
                    reinterpret_cast<char*>(resource.payload.data()),
                    static_cast<std::streamsize>(payload_size));
                if (!file) {
                    throw std::runtime_error(
                        "truncated binary live texture resource");
                }
            }
            resources.push_back(std::move(resource));
            (void)stage;
        }
        return resources;
    }
    file.clear();
    file.seekg(0, std::ios::beg);
    std::ostringstream buffer;
    buffer << file.rdbuf();
    const std::string text = buffer.str();
    const std::regex object_regex("\\{[^{}]*\\}");
    std::vector<RecoveredTextureResource> resources;
    for (std::sregex_iterator it(text.begin(), text.end(), object_regex), end; it != end; ++it) {
        const std::string object = it->str();
        const auto address = json_object_field_text(object, "address");
        const auto width = json_object_field_text(object, "width");
        const auto height = json_object_field_text(object, "height");
        const auto format = json_object_field_text(object, "format");
        const auto content_hash = json_object_field_text(object, "sha256");
        const auto bytes_hex = json_object_field_text(object, "bytes_hex");
        if (!address || !width || !height || !format || !bytes_hex) {
            continue;
        }
        RecoveredTextureResource resource{};
        resource.address = parse_json_u32_text(*address, "texture address");
        resource.width = parse_json_u32_text(*width, "texture width");
        resource.height = parse_json_u32_text(*height, "texture height");
        resource.format = *format;
        resource.content_hash = content_hash.value_or(std::string{});
        resource.payload = parse_json_bytes_hex(*bytes_hex);
        resources.push_back(std::move(resource));
    }
    return resources;
}


std::vector<RecoveredTextureResource> load_recovered_texture_resources(
    const std::filesystem::path& path,
    bool required,
    std::vector<RecoveredTextureResource>* reusable_resources,
    uint64_t* reused_resource_count,
    uint64_t* reused_payload_bytes) {
    std::ifstream file(path, std::ios::binary);
    if (!file) {
        if (reused_resource_count != nullptr) {
            *reused_resource_count = 0u;
        }
        if (reused_payload_bytes != nullptr) {
            *reused_payload_bytes = 0u;
        }
        if (required) {
            throw std::runtime_error(
                "cannot open live resource snapshot: " + path.string());
        }
        return {};
    }
    return load_recovered_texture_resources_stream(
        file,
        reusable_resources,
        reused_resource_count,
        reused_payload_bytes);
}


std::vector<RecoveredTextureResource> load_recovered_texture_resources_bytes(
    const std::vector<uint8_t>& bytes,
    std::vector<RecoveredTextureResource>* reusable_resources,
    uint64_t* reused_resource_count,
    uint64_t* reused_payload_bytes) {
    if (bytes.size() >= 8u
        && std::memcmp(bytes.data(), "B2TEX001", 8u) == 0) {
        if (reused_resource_count != nullptr) {
            *reused_resource_count = 0u;
        }
        if (reused_payload_bytes != nullptr) {
            *reused_payload_bytes = 0u;
        }
        size_t cursor = 8u;
        const auto read_u32 = [&bytes, &cursor]() {
            if (cursor > bytes.size() || bytes.size() - cursor < 4u) {
                throw std::runtime_error(
                    "truncated binary live texture resource");
            }
            uint32_t value = 0u;
            std::memcpy(&value, bytes.data() + cursor, sizeof(value));
            cursor += sizeof(value);
            return value;
        };
        const uint32_t resource_count = read_u32();
        if (resource_count > (bytes.size() - cursor) / 56u) {
            throw std::runtime_error(
                "invalid binary live texture resource count");
        }
        std::unordered_map<uint32_t, std::vector<size_t>> reusable_by_address;
        std::vector<bool> reusable_claimed;
        if (reusable_resources != nullptr) {
            reusable_claimed.resize(reusable_resources->size());
            for (size_t index = 0u; index < reusable_resources->size(); ++index) {
                reusable_by_address[(*reusable_resources)[index].address]
                    .push_back(index);
            }
        }
        std::vector<RecoveredTextureResource> resources;
        resources.reserve(resource_count);
        for (uint32_t index = 0u; index < resource_count; ++index) {
            const uint32_t stage = read_u32();
            const uint32_t address = read_u32();
            const uint32_t width = read_u32();
            const uint32_t height = read_u32();
            const uint32_t format_size = read_u32();
            const uint32_t payload_size = read_u32();
            if (format_size == 0u || format_size > 32u
                || payload_size > 256u * 1024u * 1024u
                || cursor > bytes.size()
                || bytes.size() - cursor < 32u + format_size
                || bytes.size() - cursor - 32u - format_size < payload_size) {
                throw std::runtime_error(
                    "invalid binary live texture resource header");
            }
            constexpr char kHexDigits[] = "0123456789ABCDEF";
            std::string hash(64u, '0');
            for (size_t byte_index = 0u; byte_index < 32u; ++byte_index) {
                const uint8_t byte = bytes[cursor + byte_index];
                hash[byte_index * 2u] = kHexDigits[byte >> 4u];
                hash[byte_index * 2u + 1u] = kHexDigits[byte & 0xFu];
            }
            cursor += 32u;
            std::string format(
                reinterpret_cast<const char*>(bytes.data() + cursor),
                format_size);
            cursor += format_size;

            RecoveredTextureResource resource{};
            resource.address = address;
            resource.width = width;
            resource.height = height;
            resource.format = std::move(format);
            resource.content_hash = std::move(hash);
            bool reused_payload = false;
            const auto candidates = reusable_by_address.find(address);
            if (candidates != reusable_by_address.end()) {
                for (const size_t candidate_index : candidates->second) {
                    RecoveredTextureResource& candidate =
                        (*reusable_resources)[candidate_index];
                    if (reusable_claimed[candidate_index]
                        || candidate.width != width
                        || candidate.height != height
                        || candidate.format != resource.format
                        || candidate.content_hash != resource.content_hash
                        || candidate.payload.size() != payload_size) {
                        continue;
                    }
                    resource.payload = std::move(candidate.payload);
                    reusable_claimed[candidate_index] = true;
                    reused_payload = true;
                    if (reused_resource_count != nullptr) {
                        ++*reused_resource_count;
                    }
                    if (reused_payload_bytes != nullptr) {
                        *reused_payload_bytes += payload_size;
                    }
                    break;
                }
            }
            if (!reused_payload) {
                resource.payload.resize(payload_size);
                if (payload_size != 0u) {
                    std::memcpy(
                        resource.payload.data(),
                        bytes.data() + cursor,
                        payload_size);
                }
            }
            cursor += payload_size;
            resources.push_back(std::move(resource));
            (void)stage;
        }
        return resources;
    }
    std::istringstream stream(
        std::string(
            reinterpret_cast<const char*>(bytes.data()),
            bytes.size()),
        std::ios::in | std::ios::binary);
    return load_recovered_texture_resources_stream(
        stream,
        reusable_resources,
        reused_resource_count,
        reused_payload_bytes);
}


RecoveredD3DStreamSource load_or_build_recovered_d3d_command_stream(
    const std::filesystem::path& render_stream_json,
    bool load_textures,
    const std::string* supplied_manifest_text,
    bool load_commands,
    bool require_resource_source,
    std::vector<RecoveredTextureResource>* reusable_resources,
    uint64_t* reused_resource_count,
    uint64_t* reused_payload_bytes) {
    if (!render_stream_json.empty()) {
        std::string owned_manifest_text;
        if (supplied_manifest_text == nullptr) {
            owned_manifest_text = read_text_file_shared(render_stream_json).value_or(
                std::string{});
            supplied_manifest_text = &owned_manifest_text;
        }
        const std::string& manifest_text = *supplied_manifest_text;
        const bool resources_unchanged =
            manifest_text.find("\"resource_snapshots_unchanged\":true")
            != std::string::npos;
        const auto resource_field = json_object_field_text(
            manifest_text, "resource_snapshot_path");
        const auto command_field = json_object_field_text(
            manifest_text, "command_snapshot_path");
        const auto frontend_field = json_object_field_text(
            manifest_text, "frontend_text");
        const auto frontend_x_field = json_object_field_text(
            manifest_text, "frontend_text_x_bits");
        const auto frontend_y_field = json_object_field_text(
            manifest_text, "frontend_text_y_bits");
        const auto frontend_size_field = json_object_field_text(
            manifest_text, "frontend_text_size_bits");
        const auto frontend_color_field = json_object_field_text(
            manifest_text, "frontend_text_color_argb");
        const auto interpreter_bootstrap_field = json_object_field_text(
            manifest_text, "interpreter_bootstrap_path");
        const std::filesystem::path resource_source = resource_field.has_value()
            ? std::filesystem::path(*resource_field)
            : render_stream_json;
        const std::filesystem::path command_source = command_field.has_value()
            ? std::filesystem::path(*command_field)
            : std::filesystem::path{};
        const std::filesystem::path interpreter_bootstrap_source =
            interpreter_bootstrap_field.has_value()
            ? std::filesystem::path(*interpreter_bootstrap_field)
            : std::filesystem::path{};
        std::vector<RecoveredD3DCommand> commands = load_commands
            ? (command_source.empty()
                ? load_recovered_d3d_command_stream(render_stream_json)
                : load_recovered_d3d_binary_stream(command_source))
            : std::vector<RecoveredD3DCommand>{};
        const auto command_record_count_field = json_object_field_text(
            manifest_text, "command_snapshot_record_count");
        if (load_commands && command_record_count_field.has_value()) {
            const uint64_t declared_record_count = parse_json_u64_text(
                *command_record_count_field,
                "command snapshot record count");
            if (declared_record_count > commands.size()) {
                std::ostringstream message;
                message << "command snapshot is short: manifest declares "
                        << declared_record_count << " records but file contains "
                        << commands.size();
                throw std::runtime_error(message.str());
            }
            // A retained append-only sidecar can be copied while the guest is
            // already writing the next frame. The manifest count is the
            // validated completed-flip boundary; never interpret a trailing,
            // unpresented record merely because it exists in the file.
            commands.resize(static_cast<size_t>(declared_record_count));
        }
        return {
            std::move(commands),
             load_textures ? load_recovered_texture_resources(
                                 resource_source,
                                 require_resource_source,
                                 reusable_resources,
                                 reused_resource_count,
                                 reused_payload_bytes)
                           : std::vector<RecoveredTextureResource>{},
            render_stream_json.string(),
            frontend_field.value_or(std::string{}),
            frontend_x_field.has_value()
                ? parse_json_u32_text(*frontend_x_field, "frontend text x")
                : 0x43A00000u,
            frontend_y_field.has_value()
                ? parse_json_u32_text(*frontend_y_field, "frontend text y")
                : 0x43AA0000u,
            frontend_size_field.has_value()
                ? parse_json_u32_text(
                    *frontend_size_field, "frontend text size")
                : 0x41C80000u,
            frontend_color_field.has_value()
                ? parse_json_u32_text(
                    *frontend_color_field, "frontend text color")
                : 0xFFFFFFFFu,
            resources_unchanged,
            resource_source,
            interpreter_bootstrap_source,
        };
    }
    return {
        build_recovered_d3d_command_stream(),
        {},
        "built_in_seed",
        {},
        0x43A00000u,
        0x43AA0000u,
        0x41C80000u,
        0xFFFFFFFFu,
        false,
        {},
        {},
    };
}


const char* recovered_d3d_command_kind_name(RecoveredD3DCommandKind kind) {
    switch (kind) {
        case RecoveredD3DCommandKind::MmioWrite:
            return "d3d_mmio";
        case RecoveredD3DCommandKind::PushBufferWrite:
            return "d3d_push_buffer";
        default:
            return "unknown";
    }
}


uint32_t mix_d3d_state_seed(uint32_t seed, uint32_t value) {
    seed ^= value + 0x9E3779B9u + (seed << 6u) + (seed >> 2u);
    return seed;
}


SurfacePayloadCandidate dominant_surface_payload_from_words(
    const std::vector<PushBufferWord>& words) {
    SurfacePayloadCandidate candidate{};
    candidate.sample_count = static_cast<uint32_t>(words.size());
    if (words.empty()) {
        return candidate;
    }

    std::unordered_map<uint32_t, uint32_t> counts;
    counts.reserve(words.size());
    for (const PushBufferWord& word : words) {
        ++counts[word.value];
    }
    for (const auto& [value, count] : counts) {
        if (count > candidate.dominant_count
            || (count == candidate.dominant_count && value < candidate.argb)) {
            candidate.argb = value;
            candidate.dominant_count = count;
        }
    }
    candidate.valid = candidate.dominant_count >= 2u;
    return candidate;
}


float float_from_u32(uint32_t value) {
    float result = 0.0f;
    std::memcpy(&result, &value, sizeof(result));
    return result;
}


uint32_t u32_from_float(float value) {
    uint32_t result = 0u;
    std::memcpy(&result, &value, sizeof(result));
    return result;
}


float float_from_i16_bits(uint32_t value) {
    const uint16_t bits = static_cast<uint16_t>(value);
    int16_t result = 0;
    std::memcpy(&result, &bits, sizeof(result));
    return static_cast<float>(result);
}


NativeDraw native_draw_from_interpreted_state(
    const InterpretedD3DStream& interpreted) {
    NativeDraw draw{};
    draw.primitive = interpreted.active_primitive;
    draw.texture_offsets = interpreted.texture_offsets;
    draw.texture_addresses = interpreted.texture_addresses;
    draw.texture_formats = interpreted.texture_formats;
    draw.texture_image_rects = interpreted.texture_image_rects;
    draw.texture_controls = interpreted.texture_controls;
    draw.texture_filters = interpreted.texture_filters;
    draw.vertex_offsets = interpreted.vertex_offsets;
    draw.vertex_formats = interpreted.vertex_formats;
    draw.current_vertex_attributes = interpreted.current_vertex_attributes;
    draw.blend_enable = interpreted.blend_enable;
    draw.blend_source_factor = interpreted.blend_source_factor;
    draw.blend_destination_factor = interpreted.blend_destination_factor;
    draw.blend_equation = interpreted.blend_equation;
    draw.alpha_test_enable = interpreted.alpha_test_enable;
    draw.alpha_function = interpreted.alpha_function;
    draw.alpha_reference = interpreted.alpha_reference;
    draw.surface_format = interpreted.surface_format;
    draw.surface_pitch = interpreted.surface_pitch;
    draw.surface_color_offset = interpreted.surface_color_offset;
    draw.surface_clip_horizontal = interpreted.surface_clip_horizontal;
    draw.surface_clip_vertical = interpreted.surface_clip_vertical;
    draw.color_mask = interpreted.color_mask;
    draw.depth_test_enable = interpreted.depth_test_enable;
    draw.depth_function = interpreted.depth_function;
    draw.depth_write_enable = interpreted.depth_write_enable;
    draw.polygon_offset_fill_enable = interpreted.polygon_offset_fill_enable;
    draw.polygon_offset_scale_factor = interpreted.polygon_offset_scale_factor;
    draw.polygon_offset_bias = interpreted.polygon_offset_bias;
    draw.skin_mode = interpreted.skin_mode;
    draw.specular_enable = interpreted.specular_enable;
    draw.cull_face_enable = interpreted.cull_face_enable;
    draw.cull_face = interpreted.cull_face;
    draw.front_face = interpreted.front_face;
    draw.shader_stage_program = interpreted.shader_stage_program;
    draw.combiner_control = interpreted.combiner_control;
    draw.combiner_color_inputs = interpreted.combiner_color_inputs;
    draw.combiner_color_outputs = interpreted.combiner_color_outputs;
    draw.combiner_alpha_inputs = interpreted.combiner_alpha_inputs;
    draw.combiner_alpha_outputs = interpreted.combiner_alpha_outputs;
    draw.combiner_factors0 = interpreted.combiner_factors0;
    draw.combiner_factors1 = interpreted.combiner_factors1;
    draw.final_combiner_inputs0 = interpreted.final_combiner_inputs0;
    draw.final_combiner_inputs1 = interpreted.final_combiner_inputs1;
    draw.final_combiner_factors = interpreted.final_combiner_factors;
    draw.fog_mode = interpreted.fog_mode;
    draw.fog_generation_mode = interpreted.fog_generation_mode;
    draw.fog_enable = interpreted.fog_enable;
    draw.fog_color = interpreted.fog_color;
    draw.fog_params = interpreted.fog_params;
    draw.transform_execution_mode = interpreted.transform_execution_mode;
    draw.transform_program_start = interpreted.transform_program_start;
    draw.transform_program = interpreted.transform_program;
    draw.transform_constants = interpreted.transform_constants;
    for (uint32_t stage = 0; stage < interpreted.texture_controls.size(); ++stage) {
        if ((interpreted.texture_controls[stage] & (1u << 30u)) == 0u) {
            continue;
        }
        draw.texture_enabled = true;
        draw.texture_stage = stage;
        draw.texture_address = interpreted.texture_offsets[stage];
        break;
    }
    return draw;
}


void finish_inline_draw(InterpretedD3DStream& interpreted) {
    if (interpreted.active_primitive == 0 || interpreted.inline_words.empty()) {
        interpreted.inline_words.clear();
        return;
    }
    uint32_t stride_words = 0;
    for (uint32_t format : interpreted.vertex_formats) {
        const uint32_t type = format & 0xFu;
        const uint32_t components = (format >> 4u) & 0xFu;
        if (type == 2u) {
            stride_words += components;
        } else if ((type == 0u || type == 4u || type == 6u) && components != 0u) {
            stride_words += 1u;
        } else {
            stride_words += (components + 1u) / 2u;
        }
    }
    if (stride_words == 0) {
        interpreted.inline_words.clear();
        return;
    }
    NativeDraw draw = native_draw_from_interpreted_state(interpreted);
    std::vector<NativeVertex> decoded_vertices;
    decoded_vertices.reserve(interpreted.inline_words.size() / stride_words);
    for (size_t base = 0; base + stride_words <= interpreted.inline_words.size(); base += stride_words) {
        size_t cursor = base;
        NativeVertex vertex{};
        vertex.program_inputs = draw.current_vertex_attributes;
        const auto& current_position = vertex.program_inputs[0];
        vertex.x = current_position[0];
        vertex.y = current_position[1];
        vertex.z = current_position[2];
        vertex.w = current_position[3];
        const auto& current_diffuse = vertex.program_inputs[3];
        vertex.r = current_diffuse[0];
        vertex.g = current_diffuse[1];
        vertex.b = current_diffuse[2];
        vertex.a = current_diffuse[3];
        const auto& current_secondary = vertex.program_inputs[4];
        vertex.secondary_r = current_secondary[0];
        vertex.secondary_g = current_secondary[1];
        vertex.secondary_b = current_secondary[2];
        vertex.secondary_a = current_secondary[3];
        const auto& current_texture = vertex.program_inputs[
            std::min<uint32_t>(9u + draw.texture_stage, 15u)];
        vertex.u = current_texture[0];
        vertex.v = current_texture[1];
        vertex.texture_r = current_texture[2];
        vertex.texture_q = current_texture[3];
        for (uint32_t stage = 0u; stage < 4u; ++stage) {
            vertex.texture_coordinates[stage] =
                vertex.program_inputs[9u + stage];
        }
        for (uint32_t slot = 0; slot < interpreted.vertex_formats.size(); ++slot) {
            const uint32_t format = interpreted.vertex_formats[slot];
            const uint32_t type = format & 0xFu;
            const uint32_t components = (format >> 4u) & 0xFu;
            uint32_t word_count = 0;
            if (type == 2u) {
                word_count = components;
            } else if ((type == 0u || type == 4u || type == 6u) && components != 0u) {
                word_count = 1u;
            } else {
                word_count = (components + 1u) / 2u;
            }
            if (cursor + word_count > interpreted.inline_words.size()) {
                break;
            }
            if (slot == 0 && type == 2u && components >= 2u) {
                vertex.raw_x_bits = interpreted.inline_words[cursor];
                vertex.raw_y_bits = interpreted.inline_words[cursor + 1u];
                vertex.x = float_from_u32(interpreted.inline_words[cursor]);
                vertex.y = float_from_u32(interpreted.inline_words[cursor + 1u]);
                if (components >= 3u) {
                    vertex.z = float_from_u32(interpreted.inline_words[cursor + 2u]);
                }
                if (components >= 4u) {
                    vertex.w = float_from_u32(interpreted.inline_words[cursor + 3u]);
                }
            } else if (slot == 3 && word_count >= 1u) {
                const uint32_t argb = interpreted.inline_words[cursor];
                vertex.a = static_cast<float>((argb >> 24u) & 0xFFu) / 255.0f;
                vertex.r = static_cast<float>((argb >> 16u) & 0xFFu) / 255.0f;
                vertex.g = static_cast<float>((argb >> 8u) & 0xFFu) / 255.0f;
                vertex.b = static_cast<float>(argb & 0xFFu) / 255.0f;
            } else if (slot == 4 && word_count >= 1u) {
                const uint32_t argb = interpreted.inline_words[cursor];
                vertex.secondary_a = static_cast<float>(
                    (argb >> 24u) & 0xFFu) / 255.0f;
                vertex.secondary_r = static_cast<float>(
                    (argb >> 16u) & 0xFFu) / 255.0f;
                vertex.secondary_g = static_cast<float>(
                    (argb >> 8u) & 0xFFu) / 255.0f;
                vertex.secondary_b = static_cast<float>(
                    argb & 0xFFu) / 255.0f;
            } else if (slot == std::min<uint32_t>(9u + draw.texture_stage, 15u)
                       && type == 2u && components >= 2u) {
                vertex.u = float_from_u32(interpreted.inline_words[cursor]);
                vertex.v = float_from_u32(interpreted.inline_words[cursor + 1u]);
                if (components >= 3u) {
                    vertex.texture_r = float_from_u32(
                        interpreted.inline_words[cursor + 2u]);
                }
                if (components >= 4u) {
                    vertex.texture_q = float_from_u32(
                        interpreted.inline_words[cursor + 3u]);
                }
            }
            cursor += word_count;
        }
        vertex.texture_coordinates[
            std::min<uint32_t>(draw.texture_stage, 3u)] = {
                vertex.u,
                vertex.v,
                vertex.texture_r,
                vertex.texture_q,
            };
        decoded_vertices.push_back(vertex);
    }
    if (draw.primitive == 8u) {
        constexpr std::array<size_t, 4> quad_strip_order{0u, 1u, 3u, 2u};
        const size_t quad_count = decoded_vertices.size() / 4u;
        for (size_t quad_index = 0; quad_index < quad_count; ++quad_index) {
            NativeDraw converted = draw;
            converted.first_vertex = static_cast<uint32_t>(
                interpreted.vertices.size());
            converted.vertex_count = 4u;
            converted.primitive = 6u;
            const size_t quad_begin = quad_index * 4u;
            for (const size_t vertex_index : quad_strip_order) {
                interpreted.vertices.push_back(
                    decoded_vertices[quad_begin + vertex_index]);
            }
            interpreted.draws.push_back(std::move(converted));
        }
        interpreted.converted_quad_count += static_cast<uint32_t>(quad_count);
        interpreted.discarded_quad_vertex_count += static_cast<uint32_t>(
            decoded_vertices.size() - quad_count * 4u);
    } else if (!decoded_vertices.empty()) {
        draw.first_vertex = static_cast<uint32_t>(interpreted.vertices.size());
        draw.vertex_count = static_cast<uint32_t>(decoded_vertices.size());
        interpreted.vertices.insert(
            interpreted.vertices.end(),
            decoded_vertices.begin(),
            decoded_vertices.end());
        interpreted.draws.push_back(std::move(draw));
    }
    interpreted.inline_words.clear();
}


void finish_indexed_draw(InterpretedD3DStream& interpreted) {
    if (interpreted.active_primitive == 0
        || interpreted.active_vertex_indices.empty()) {
        interpreted.active_vertex_indices.clear();
        return;
    }
    NativeDraw draw = native_draw_from_interpreted_state(interpreted);
    draw.indexed_array = true;
    draw.vertex_indices = std::move(interpreted.active_vertex_indices);
    interpreted.indexed_array_element_count += static_cast<uint32_t>(
        draw.vertex_indices.size());
    ++interpreted.indexed_array_draw_count;
    interpreted.draws.push_back(std::move(draw));
    interpreted.active_vertex_indices.clear();
}


bool nv2a_method_is_batchable_state(uint32_t method) {
    if ((method >= 0x0680u && method <= 0x06BCu)
        || (method >= 0x0A20u && method <= 0x0A2Cu)
        || (method >= 0x0AF0u && method <= 0x0AFCu)
        || (method >= 0x0B00u && method <= 0x0BFCu)
        || (method >= 0x1720u && method <= 0x179Cu)
        || (method >= 0x1880u && method <= 0x1AFCu)
        || (method >= 0x1B00u && method < 0x1C00u)
        || (method >= 0x0260u && method <= 0x027Cu)
        || (method >= 0x09C0u && method <= 0x09C8u)
        || (method >= 0x0A60u && method <= 0x0ADCu)
        || (method >= 0x1E20u && method <= 0x1E24u)
        || (method >= 0x1E40u && method <= 0x1E5Cu)
        || (method >= 0x1E80u && method <= 0x1E8Cu)) {
        return (method & 3u) == 0u;
    }
    switch (method) {
    case 0x0200u:
    case 0x0204u:
    case 0x0208u:
    case 0x020Cu:
    case 0x0210u:
    case 0x0288u:
    case 0x028Cu:
    case 0x029Cu:
    case 0x02A0u:
    case 0x02A4u:
    case 0x02A8u:
    case 0x0300u:
    case 0x0304u:
    case 0x0308u:
    case 0x030Cu:
    case 0x0328u:
    case 0x0338u:
    case 0x033Cu:
    case 0x0340u:
    case 0x0344u:
    case 0x0348u:
    case 0x0350u:
    case 0x0354u:
    case 0x0358u:
    case 0x035Cu:
    case 0x0384u:
    case 0x0388u:
    case 0x039Cu:
    case 0x03A0u:
    case 0x03B8u:
    case 0x1D90u:
    case 0x1E60u:
    case 0x1E70u:
    case 0x1E94u:
    case 0x1E9Cu:
    case 0x1EA0u:
    case 0x1EA4u:
        return true;
    default:
        return false;
    }
}


bool nv2a_state_method_is_unchanged(
    uint32_t method,
    uint32_t data,
    const InterpretedD3DStream& interpreted) {
    if (method >= 0x1720u && method <= 0x175Cu) {
        return interpreted.vertex_offsets[(method - 0x1720u) / 4u] == data;
    }
    if (method >= 0x1760u && method <= 0x179Cu) {
        return interpreted.vertex_formats[(method - 0x1760u) / 4u] == data;
    }
    if (method >= 0x1880u && method <= 0x1AFCu) {
        // The interpreted state stores these values as floats. Re-applying
        // preserves signed-zero and NaN payload bits that a float comparison
        // cannot distinguish, so they are batched but never elided.
        return false;
    }
    if (method >= 0x0680u && method <= 0x06BCu) {
        const uint32_t slot = (method - 0x0680u) / 4u;
        return interpreted.transform_constants[slot / 4u][slot % 4u]
            == data;
    }
    if (method >= 0x0A20u && method <= 0x0A2Cu) {
        return interpreted.transform_constants[59u][
            (method - 0x0A20u) / 4u] == data;
    }
    if (method >= 0x0AF0u && method <= 0x0AFCu) {
        return interpreted.transform_constants[58u][
            (method - 0x0AF0u) / 4u] == data;
    }
    if (method >= 0x0B00u && method <= 0x0BFCu) {
        // These writes advance loader cursors even when the payload is equal.
        return false;
    }
    if (method >= 0x1E80u && method <= 0x1E8Cu) {
        return interpreted.transform_data[(method - 0x1E80u) / 4u] == data;
    }
    if (method >= 0x0260u && method <= 0x027Cu) {
        return interpreted.combiner_alpha_inputs[(method - 0x0260u) / 4u]
            == data;
    }
    if (method >= 0x09C0u && method <= 0x09C8u) {
        return interpreted.fog_params[(method - 0x09C0u) / 4u] == data;
    }
    if (method >= 0x0A60u && method <= 0x0A7Cu) {
        return interpreted.combiner_factors0[(method - 0x0A60u) / 4u]
            == data;
    }
    if (method >= 0x0A80u && method <= 0x0A9Cu) {
        return interpreted.combiner_factors1[(method - 0x0A80u) / 4u]
            == data;
    }
    if (method >= 0x0AA0u && method <= 0x0ABCu) {
        return interpreted.combiner_alpha_outputs[(method - 0x0AA0u) / 4u]
            == data;
    }
    if (method >= 0x0AC0u && method <= 0x0ADCu) {
        return interpreted.combiner_color_inputs[(method - 0x0AC0u) / 4u]
            == data;
    }
    if (method >= 0x1E20u && method <= 0x1E24u) {
        return interpreted.final_combiner_factors[(method - 0x1E20u) / 4u]
            == data;
    }
    if (method >= 0x1E40u && method <= 0x1E5Cu) {
        return interpreted.combiner_color_outputs[(method - 0x1E40u) / 4u]
            == data;
    }
    if (method >= 0x1B00u && method < 0x1C00u) {
        const uint32_t stage = (method - 0x1B00u) / 0x40u;
        const uint32_t offset = (method - 0x1B00u) % 0x40u;
        if (offset == 0x00u) return interpreted.texture_offsets[stage] == data;
        if (offset == 0x04u) return interpreted.texture_formats[stage] == data;
        if (offset == 0x08u) return interpreted.texture_addresses[stage] == data;
        if (offset == 0x0Cu) return interpreted.texture_controls[stage] == data;
        if (offset == 0x14u) return interpreted.texture_filters[stage] == data;
        if (offset == 0x1Cu) return interpreted.texture_image_rects[stage] == data;
        return true;
    }
    switch (method) {
    case 0x0200u: return interpreted.surface_clip_horizontal == data;
    case 0x0204u: return interpreted.surface_clip_vertical == data;
    case 0x0208u: return interpreted.surface_format == data;
    case 0x020Cu: return interpreted.surface_pitch == data;
    case 0x0210u: return interpreted.surface_color_offset == data;
    case 0x0288u: return interpreted.final_combiner_inputs0 == data;
    case 0x028Cu: return interpreted.final_combiner_inputs1 == data;
    case 0x029Cu: return interpreted.fog_mode == data;
    case 0x02A0u: return interpreted.fog_generation_mode == data;
    case 0x02A4u: return interpreted.fog_enable == data;
    case 0x02A8u: return interpreted.fog_color == data;
    case 0x0300u: return interpreted.alpha_test_enable == data;
    case 0x0304u: return interpreted.blend_enable == data;
    case 0x0308u: return interpreted.cull_face_enable == data;
    case 0x030Cu: return interpreted.depth_test_enable == data;
    case 0x0328u: return interpreted.skin_mode == data;
    case 0x0338u: return interpreted.polygon_offset_fill_enable == data;
    case 0x033Cu: return interpreted.alpha_function == data;
    case 0x0340u: return interpreted.alpha_reference == data;
    case 0x0344u: return interpreted.blend_source_factor == data;
    case 0x0348u: return interpreted.blend_destination_factor == data;
    case 0x0350u: return interpreted.blend_equation == data;
    case 0x0354u: return interpreted.depth_function == data;
    case 0x0358u: return interpreted.color_mask == data;
    case 0x035Cu: return interpreted.depth_write_enable == data;
    case 0x0384u: return interpreted.polygon_offset_scale_factor == data;
    case 0x0388u: return interpreted.polygon_offset_bias == data;
    case 0x039Cu: return interpreted.cull_face == data;
    case 0x03A0u: return interpreted.front_face == data;
    case 0x03B8u: return interpreted.specular_enable == data;
    case 0x1D90u:
        return interpreted.clear_color_valid
            && interpreted.clear_color_argb == data;
    case 0x1E60u: return interpreted.combiner_control == data;
    case 0x1E70u: return interpreted.shader_stage_program == data;
    case 0x1E94u: return interpreted.transform_execution_mode == data;
    case 0x1E9Cu: return interpreted.transform_program_load == data;
    case 0x1EA0u: return interpreted.transform_program_start == data;
    case 0x1EA4u: return interpreted.transform_constant_load == data;
    default: return false;
    }
}


void interpret_nv2a_method(
    uint32_t method,
    uint32_t data,
    InterpretedD3DStream& interpreted) {
    if (method == 0x012Cu) {
        finish_inline_draw(interpreted);
        finish_indexed_draw(interpreted);
        interpreted.presented_draw_begin = interpreted.frame_draw_begin;
        interpreted.presented_draw_count =
            static_cast<uint32_t>(interpreted.draws.size()) - interpreted.frame_draw_begin;
        interpreted.frame_draw_begin = static_cast<uint32_t>(interpreted.draws.size());
        interpreted.presented_surface_clears =
            std::move(interpreted.frame_surface_clears);
        interpreted.frame_surface_clears.clear();
        ++interpreted.flip_count;
    } else if (method == 0x1810u) {
        if (interpreted.active_primitive == 0u) {
            ++interpreted.unsupported_draw_arrays_count;
        } else {
            const uint32_t first = data & 0x00FFFFFFu;
            const uint32_t count = ((data >> 24u) & 0xFFu) + 1u;
            for (uint32_t offset = 0; offset < count; ++offset) {
                interpreted.active_vertex_indices.push_back(first + offset);
            }
        }
    } else if (method == 0x17FCu) {
        finish_inline_draw(interpreted);
        finish_indexed_draw(interpreted);
        interpreted.active_primitive = data;
    } else if (method == 0x1800u && interpreted.active_primitive != 0u) {
        interpreted.active_vertex_indices.push_back(data & 0xFFFFu);
        interpreted.active_vertex_indices.push_back((data >> 16u) & 0xFFFFu);
    } else if (method == 0x1808u && interpreted.active_primitive != 0u) {
        interpreted.active_vertex_indices.push_back(data);
    } else if (method == 0x1818u && interpreted.active_primitive != 0u) {
        interpreted.inline_words.push_back(data);
    } else if (method >= 0x1720u && method <= 0x175Cu && (method - 0x1720u) % 4u == 0u) {
        interpreted.vertex_offsets[(method - 0x1720u) / 4u] = data;
    } else if (method >= 0x1760u && method <= 0x179Cu && (method - 0x1760u) % 4u == 0u) {
        interpreted.vertex_formats[(method - 0x1760u) / 4u] = data;
    } else if (method >= 0x1880u && method <= 0x18FCu
               && (method - 0x1880u) % 4u == 0u) {
        const uint32_t slot = (method - 0x1880u) / 4u;
        const uint32_t attribute = slot / 2u;
        const uint32_t lane = slot % 2u;
        interpreted.current_vertex_attributes[attribute][lane] =
            float_from_u32(data);
        interpreted.current_vertex_attributes[attribute][2] = 0.0f;
        interpreted.current_vertex_attributes[attribute][3] = 1.0f;
    } else if (method >= 0x1900u && method <= 0x193Cu
               && (method - 0x1900u) % 4u == 0u) {
        const uint32_t attribute = (method - 0x1900u) / 4u;
        interpreted.current_vertex_attributes[attribute] = {
            float_from_i16_bits(data),
            float_from_i16_bits(data >> 16u),
            0.0f,
            1.0f,
        };
    } else if (method >= 0x1940u && method <= 0x197Cu
               && (method - 0x1940u) % 4u == 0u) {
        const uint32_t attribute = (method - 0x1940u) / 4u;
        interpreted.current_vertex_attributes[attribute] = {
            static_cast<float>(data & 0xFFu) / 255.0f,
            static_cast<float>((data >> 8u) & 0xFFu) / 255.0f,
            static_cast<float>((data >> 16u) & 0xFFu) / 255.0f,
            static_cast<float>((data >> 24u) & 0xFFu) / 255.0f,
        };
    } else if (method >= 0x1980u && method <= 0x19FCu
               && (method - 0x1980u) % 4u == 0u) {
        const uint32_t slot = (method - 0x1980u) / 4u;
        const uint32_t attribute = slot / 2u;
        const uint32_t lane = (slot % 2u) * 2u;
        interpreted.current_vertex_attributes[attribute][lane] =
            float_from_i16_bits(data);
        interpreted.current_vertex_attributes[attribute][lane + 1u] =
            float_from_i16_bits(data >> 16u);
    } else if (method >= 0x1A00u && method <= 0x1AFCu
               && (method - 0x1A00u) % 4u == 0u) {
        const uint32_t slot = (method - 0x1A00u) / 4u;
        interpreted.current_vertex_attributes[slot / 4u][slot % 4u] =
            float_from_u32(data);
    } else if (method >= 0x0680u && method <= 0x06BCu && (method - 0x0680u) % 4u == 0u) {
        // NV097_SET_COMPOSITE_MATRIX aliases fixed-function constants 0-3.
        const uint32_t slot = (method - 0x0680u) / 4u;
        interpreted.transform_constants[slot / 4u][slot % 4u] = data;
    } else if (method >= 0x0A20u && method <= 0x0A2Cu && (method - 0x0A20u) % 4u == 0u) {
        // NV097_SET_VIEWPORT_OFFSET aliases vertex constant 59.
        interpreted.transform_constants[59u][(method - 0x0A20u) / 4u] = data;
    } else if (method >= 0x0AF0u && method <= 0x0AFCu && (method - 0x0AF0u) % 4u == 0u) {
        // NV097_SET_VIEWPORT_SCALE aliases vertex constant 58.
        interpreted.transform_constants[58u][(method - 0x0AF0u) / 4u] = data;
    } else if (method >= 0x0B00u && method <= 0x0B7Cu && (method - 0x0B00u) % 4u == 0u) {
        const uint32_t slot = (method - 0x0B00u) / 4u;
        if (interpreted.transform_program_load < interpreted.transform_program.size()) {
            interpreted.transform_program[interpreted.transform_program_load][slot % 4u] = data;
            if (slot % 4u == 3u) {
                ++interpreted.transform_program_load;
            }
        }
    } else if (method >= 0x0B80u && method <= 0x0BFCu && (method - 0x0B80u) % 4u == 0u) {
        const uint32_t slot = (method - 0x0B80u) / 4u;
        if (interpreted.transform_constant_load < interpreted.transform_constants.size()) {
            interpreted.transform_constants[interpreted.transform_constant_load][slot % 4u] = data;
            if (slot % 4u == 3u) {
                ++interpreted.transform_constant_load;
            }
        }
    } else if (method == 0x1E94u) {
        interpreted.transform_execution_mode = data;
    } else if (method >= 0x1E80u && method <= 0x1E8Cu && (method - 0x1E80u) % 4u == 0u) {
        interpreted.transform_data[(method - 0x1E80u) / 4u] = data;
    } else if (method == 0x1E90u) {
        std::array<std::array<float, 4>, 16> inputs{};
        for (uint32_t lane = 0; lane < 4u; ++lane) {
            inputs[0][lane] = float_from_u32(interpreted.transform_data[lane]);
        }
        std::array<std::array<uint32_t, 4>, 192> updated_constants{};
        const Nv2aVertexProgramResult launch = execute_nv2a_vertex_program(
            interpreted.transform_program,
            interpreted.transform_constants,
            data,
            inputs,
            &updated_constants);
        if (launch.valid) {
            interpreted.transform_constants = std::move(updated_constants);
            ++interpreted.launch_transform_program_count;
        } else {
            ++interpreted.failed_launch_transform_program_count;
        }
    } else if (method == 0x1E9Cu) {
        interpreted.transform_program_load = data;
    } else if (method == 0x1EA0u) {
        interpreted.transform_program_start = data;
    } else if (method == 0x1EA4u) {
        interpreted.transform_constant_load = data;
    } else if (method == 0x1D94u) {
        ++interpreted.clear_surface_method_count;
        interpreted.frame_surface_clears.push_back({
            interpreted.surface_color_offset,
            data,
            interpreted.clear_color_argb,
            static_cast<uint32_t>(interpreted.draws.size())
                - interpreted.frame_draw_begin,
        });
    } else if (method == 0x0304u) {
        interpreted.blend_enable = data;
    } else if (method == 0x0300u) {
        interpreted.alpha_test_enable = data;
    } else if (method == 0x033Cu) {
        interpreted.alpha_function = data;
    } else if (method == 0x0340u) {
        interpreted.alpha_reference = data;
    } else if (method == 0x0344u) {
        interpreted.blend_source_factor = data;
    } else if (method == 0x0348u) {
        interpreted.blend_destination_factor = data;
    } else if (method == 0x0350u) {
        interpreted.blend_equation = data;
    } else if (method == 0x0200u) {
        interpreted.surface_clip_horizontal = data;
    } else if (method == 0x0204u) {
        interpreted.surface_clip_vertical = data;
    } else if (method == 0x0208u) {
        interpreted.surface_format = data;
    } else if (method == 0x020Cu) {
        interpreted.surface_pitch = data;
    } else if (method == 0x0210u) {
        interpreted.surface_color_offset = data;
    } else if (method >= 0x0260u && method <= 0x027Cu
               && (method - 0x0260u) % 4u == 0u) {
        interpreted.combiner_alpha_inputs[(method - 0x0260u) / 4u] = data;
    } else if (method == 0x0288u) {
        interpreted.final_combiner_inputs0 = data;
    } else if (method == 0x028Cu) {
        interpreted.final_combiner_inputs1 = data;
    } else if (method == 0x029Cu) {
        interpreted.fog_mode = data;
    } else if (method == 0x02A0u) {
        interpreted.fog_generation_mode = data;
    } else if (method == 0x02A4u) {
        interpreted.fog_enable = data;
    } else if (method == 0x02A8u) {
        interpreted.fog_color = data;
    } else if (method == 0x0358u) {
        interpreted.color_mask = data;
    } else if (method == 0x030Cu) {
        interpreted.depth_test_enable = data;
    } else if (method == 0x0308u) {
        interpreted.cull_face_enable = data;
    } else if (method == 0x0354u) {
        interpreted.depth_function = data;
    } else if (method == 0x035Cu) {
        interpreted.depth_write_enable = data;
    } else if (method == 0x0338u) {
        interpreted.polygon_offset_fill_enable = data;
    } else if (method == 0x0384u) {
        interpreted.polygon_offset_scale_factor = data;
    } else if (method == 0x0388u) {
        interpreted.polygon_offset_bias = data;
    } else if (method == 0x0328u) {
        interpreted.skin_mode = data;
    } else if (method == 0x03B8u) {
        interpreted.specular_enable = data;
    } else if (method == 0x039Cu) {
        interpreted.cull_face = data;
    } else if (method == 0x03A0u) {
        interpreted.front_face = data;
    } else if (method >= 0x09C0u && method <= 0x09C8u
               && (method - 0x09C0u) % 4u == 0u) {
        interpreted.fog_params[(method - 0x09C0u) / 4u] = data;
    } else if (method >= 0x0A60u && method <= 0x0A7Cu
               && (method - 0x0A60u) % 4u == 0u) {
        interpreted.combiner_factors0[(method - 0x0A60u) / 4u] = data;
    } else if (method >= 0x0A80u && method <= 0x0A9Cu
               && (method - 0x0A80u) % 4u == 0u) {
        interpreted.combiner_factors1[(method - 0x0A80u) / 4u] = data;
    } else if (method >= 0x0AA0u && method <= 0x0ABCu
               && (method - 0x0AA0u) % 4u == 0u) {
        interpreted.combiner_alpha_outputs[(method - 0x0AA0u) / 4u] = data;
    } else if (method >= 0x0AC0u && method <= 0x0ADCu
               && (method - 0x0AC0u) % 4u == 0u) {
        interpreted.combiner_color_inputs[(method - 0x0AC0u) / 4u] = data;
    } else if (method >= 0x1E20u && method <= 0x1E24u
               && (method - 0x1E20u) % 4u == 0u) {
        interpreted.final_combiner_factors[(method - 0x1E20u) / 4u] = data;
    } else if (method >= 0x1E40u && method <= 0x1E5Cu
               && (method - 0x1E40u) % 4u == 0u) {
        interpreted.combiner_color_outputs[(method - 0x1E40u) / 4u] = data;
    } else if (method == 0x1E70u) {
        interpreted.shader_stage_program = data;
    } else if (method == 0x1E60u) {
        interpreted.combiner_control = data;
    } else if (method >= 0x1B00u && method < 0x1C00u) {
        const uint32_t stage = (method - 0x1B00u) / 0x40u;
        const uint32_t register_offset = (method - 0x1B00u) % 0x40u;
        if (stage < 4u) {
            if (register_offset == 0x00u) {
                interpreted.texture_offsets[stage] = data;
            } else if (register_offset == 0x04u) {
                interpreted.texture_formats[stage] = data;
            } else if (register_offset == 0x08u) {
                interpreted.texture_addresses[stage] = data;
            } else if (register_offset == 0x0Cu) {
                interpreted.texture_controls[stage] = data;
            } else if (register_offset == 0x1Cu) {
                interpreted.texture_image_rects[stage] = data;
            } else if (register_offset == 0x14u) {
                interpreted.texture_filters[stage] = data;
            }
        }
    }
    if (method == 0x1D90u) {
        interpreted.clear_color_valid = true;
        interpreted.clear_color_argb = data;
    }
}


std::vector<Nv2aBootstrapMethod> native_draw_interpreter_bootstrap_methods(
    const NativeDraw& draw,
    bool clear_color_valid,
    uint32_t clear_color_argb) {
    std::vector<Nv2aBootstrapMethod> methods;
    methods.reserve(1900u);
    auto append = [&](uint32_t method, uint32_t value) {
        methods.emplace_back(method, value);
    };
    for (size_t index = 0; index < draw.vertex_offsets.size(); ++index) {
        append(0x1720u + static_cast<uint32_t>(index) * 4u,
               draw.vertex_offsets[index]);
        append(0x1760u + static_cast<uint32_t>(index) * 4u,
               draw.vertex_formats[index]);
    }
    for (size_t attribute = 0;
         attribute < draw.current_vertex_attributes.size();
         ++attribute) {
        for (size_t lane = 0;
             lane < draw.current_vertex_attributes[attribute].size();
             ++lane) {
            append(
                0x1A00u + static_cast<uint32_t>(attribute) * 16u
                    + static_cast<uint32_t>(lane) * 4u,
                u32_from_float(draw.current_vertex_attributes[attribute][lane]));
        }
    }
    for (size_t stage = 0; stage < draw.texture_offsets.size(); ++stage) {
        const uint32_t base = 0x1B00u + static_cast<uint32_t>(stage) * 0x40u;
        append(base + 0x00u, draw.texture_offsets[stage]);
        append(base + 0x04u, draw.texture_formats[stage]);
        append(base + 0x08u, draw.texture_addresses[stage]);
        append(base + 0x0Cu, draw.texture_controls[stage]);
        append(base + 0x14u, draw.texture_filters[stage]);
        append(base + 0x1Cu, draw.texture_image_rects[stage]);
    }
    append(0x0304u, draw.blend_enable);
    append(0x0300u, draw.alpha_test_enable);
    append(0x033Cu, draw.alpha_function);
    append(0x0340u, draw.alpha_reference);
    append(0x0344u, draw.blend_source_factor);
    append(0x0348u, draw.blend_destination_factor);
    append(0x0350u, draw.blend_equation);
    append(0x0200u, draw.surface_clip_horizontal);
    append(0x0204u, draw.surface_clip_vertical);
    append(0x0208u, draw.surface_format);
    append(0x020Cu, draw.surface_pitch);
    append(0x0210u, draw.surface_color_offset);
    append(0x0358u, draw.color_mask);
    append(0x030Cu, draw.depth_test_enable);
    append(0x0308u, draw.cull_face_enable);
    append(0x0354u, draw.depth_function);
    append(0x035Cu, draw.depth_write_enable);
    append(0x0338u, draw.polygon_offset_fill_enable);
    append(0x0384u, draw.polygon_offset_scale_factor);
    append(0x0388u, draw.polygon_offset_bias);
    append(0x0328u, draw.skin_mode);
    append(0x03B8u, draw.specular_enable);
    append(0x039Cu, draw.cull_face);
    append(0x03A0u, draw.front_face);
    for (size_t index = 0; index < draw.combiner_alpha_inputs.size(); ++index) {
        append(0x0260u + static_cast<uint32_t>(index) * 4u,
               draw.combiner_alpha_inputs[index]);
        append(0x0A60u + static_cast<uint32_t>(index) * 4u,
               draw.combiner_factors0[index]);
        append(0x0A80u + static_cast<uint32_t>(index) * 4u,
               draw.combiner_factors1[index]);
        append(0x0AA0u + static_cast<uint32_t>(index) * 4u,
               draw.combiner_alpha_outputs[index]);
        append(0x0AC0u + static_cast<uint32_t>(index) * 4u,
               draw.combiner_color_inputs[index]);
        append(0x1E40u + static_cast<uint32_t>(index) * 4u,
               draw.combiner_color_outputs[index]);
    }
    append(0x0288u, draw.final_combiner_inputs0);
    append(0x028Cu, draw.final_combiner_inputs1);
    for (size_t index = 0; index < draw.final_combiner_factors.size(); ++index) {
        append(0x1E20u + static_cast<uint32_t>(index) * 4u,
               draw.final_combiner_factors[index]);
    }
    append(0x029Cu, draw.fog_mode);
    append(0x02A0u, draw.fog_generation_mode);
    append(0x02A4u, draw.fog_enable);
    append(0x02A8u, draw.fog_color);
    for (size_t index = 0; index < draw.fog_params.size(); ++index) {
        append(0x09C0u + static_cast<uint32_t>(index) * 4u,
               draw.fog_params[index]);
    }
    append(0x1E60u, draw.combiner_control);
    append(0x1E70u, draw.shader_stage_program);
    for (size_t row = 0; row < draw.transform_program.size(); ++row) {
        append(0x1E9Cu, static_cast<uint32_t>(row));
        for (size_t lane = 0; lane < draw.transform_program[row].size(); ++lane) {
            append(0x0B00u + static_cast<uint32_t>(lane) * 4u,
                   draw.transform_program[row][lane]);
        }
    }
    for (size_t row = 0; row < draw.transform_constants.size(); ++row) {
        append(0x1EA4u, static_cast<uint32_t>(row));
        for (size_t lane = 0; lane < draw.transform_constants[row].size(); ++lane) {
            append(0x0B80u + static_cast<uint32_t>(lane) * 4u,
                   draw.transform_constants[row][lane]);
        }
    }
    // The retained epoch already re-establishes any non-default loader cursor
    // it depends on. Restore the normal initial cursors after materializing the
    // full inherited program and constant contents.
    append(0x1E9Cu, 0u);
    append(0x1EA4u, 0u);
    append(0x1EA0u, draw.transform_program_start);
    append(0x1E94u, draw.transform_execution_mode);
    if (clear_color_valid) {
        append(0x1D90u, clear_color_argb);
    }
    return methods;
}


void load_interpreter_bootstrap_state(
    const std::filesystem::path& path,
    InterpretedD3DStream& interpreted) {
    std::ifstream input(path, std::ios::binary);
    if (!input) {
        throw std::runtime_error(
            "cannot open interpreter bootstrap state: " + path.string());
    }
    std::array<char, 8> magic{};
    input.read(magic.data(), static_cast<std::streamsize>(magic.size()));
    if (std::string(magic.data(), magic.size()) != "B2NVST01") {
        throw std::runtime_error("invalid interpreter bootstrap state header");
    }
    uint32_t method_count = 0u;
    input.read(reinterpret_cast<char*>(&method_count), sizeof(method_count));
    if (!input || method_count > 10000u) {
        throw std::runtime_error("invalid interpreter bootstrap method count");
    }
    for (uint32_t index = 0; index < method_count; ++index) {
        uint32_t method = 0u;
        uint32_t value = 0u;
        input.read(reinterpret_cast<char*>(&method), sizeof(method));
        input.read(reinterpret_cast<char*>(&value), sizeof(value));
        if (!input) {
            throw std::runtime_error("interpreter bootstrap state is truncated");
        }
        interpret_nv2a_method(method, value, interpreted);
    }
}


void interpret_push_buffer_method_packet(
    const std::vector<PushBufferWord>& words,
    size_t& index,
    InterpretedD3DStream& interpreted,
    bool non_increasing) {
    const uint32_t command = words[index].value;
    const uint32_t method_count = (command >> 18u) & 0x7FFu;
    const uint32_t first_method = ((command >> 2u) & 0x7FFu) * 4u;
    const uint32_t subchannel = (command >> 13u) & 0x7u;
    ++index;
    if (method_count == 0) {
        ++interpreted.zero_count_method_word_count;
        // NV4/NV10 DMA-pusher method headers with a zero count are legal
        // no-ops. Burnout 2 emits one before each populated ARRAY_ELEMENT16
        // packet, so retain the telemetry without treating it as a lost draw.
        if (interpreted.active_primitive != 0u
            && (first_method == 0x1800u || first_method == 0x1808u)) {
            ++interpreted.zero_count_indexed_array_noop_packet_count;
        }
        update_d3d_state_seed(interpreted, command);
        return;
    }
    ++interpreted.method_packet_count;
    update_d3d_state_seed(interpreted, command);
    const bool bulk_indexed = non_increasing
        && first_method == 0x1800u
        && interpreted.active_primitive != 0u;
    const bool bulk_inline = non_increasing
        && first_method == 0x1818u
        && interpreted.active_primitive != 0u;
    const bool complete_packet = method_count <= words.size() - index;
    bool bulk_state = complete_packet && !bulk_indexed && !bulk_inline;
    if (bulk_state) {
        for (uint32_t method_index = 0u;
             method_index < method_count;
             ++method_index) {
            const uint32_t method = non_increasing
                ? first_method
                : first_method + method_index * 4u;
            if (!nv2a_method_is_batchable_state(method)) {
                bulk_state = false;
                break;
            }
        }
    }
    if ((bulk_indexed || bulk_inline || bulk_state) && complete_packet) {
        const auto packet_begin = words.begin()
            + static_cast<std::ptrdiff_t>(index);
        const auto packet_end = packet_begin
            + static_cast<std::ptrdiff_t>(method_count);
        const bool one_contiguous_run =
            words[index - 1u].run_id == packet_begin->run_id
            && packet_begin->run_id == (packet_end - 1)->run_id;
        if (one_contiguous_run) {
            if (bulk_indexed) {
                const size_t output_begin =
                    interpreted.active_vertex_indices.size();
                interpreted.active_vertex_indices.resize(
                    output_begin + static_cast<size_t>(method_count) * 2u);
                if (interpreted.state_seed_updates_required) {
                    for (uint32_t method_index = 0u;
                         method_index < method_count;
                         ++method_index) {
                        update_d3d_state_seed(interpreted, first_method);
                        update_d3d_state_seed(
                            interpreted, words[index + method_index].value);
                    }
                }
                for (uint32_t method_index = 0u;
                     method_index < method_count;
                     ++method_index) {
                    const uint32_t data = words[index + method_index].value;
                    interpreted.active_vertex_indices[
                        output_begin + method_index * 2u] =
                            data & 0xFFFFu;
                    interpreted.active_vertex_indices[
                        output_begin + method_index * 2u + 1u] =
                            (data >> 16u) & 0xFFFFu;
                }
                interpreted.bulk_indexed_method_count += method_count;
                interpreted.last_bulk_indexed_method_count += method_count;
            } else if (bulk_inline) {
                const size_t output_begin = interpreted.inline_words.size();
                interpreted.inline_words.resize(output_begin + method_count);
                if (interpreted.state_seed_updates_required) {
                    for (uint32_t method_index = 0u;
                         method_index < method_count;
                         ++method_index) {
                        update_d3d_state_seed(interpreted, first_method);
                        update_d3d_state_seed(
                            interpreted, words[index + method_index].value);
                    }
                }
                for (uint32_t method_index = 0u;
                     method_index < method_count;
                     ++method_index) {
                    const uint32_t data = words[index + method_index].value;
                    interpreted.inline_words[output_begin + method_index] = data;
                }
                interpreted.bulk_inline_method_count += method_count;
                interpreted.last_bulk_inline_method_count += method_count;
            } else {
                for (uint32_t method_index = 0u;
                     method_index < method_count;
                     ++method_index) {
                    const uint32_t method = non_increasing
                        ? first_method
                        : first_method + method_index * 4u;
                    const uint32_t data =
                        words[index + method_index].value;
                    update_d3d_state_seed(interpreted, method);
                    update_d3d_state_seed(interpreted, data);
                    if (nv2a_state_method_is_unchanged(
                            method, data, interpreted)) {
                        ++interpreted.state_method_noop_count;
                        ++interpreted.last_state_method_noop_count;
                    } else {
                        interpret_nv2a_method(method, data, interpreted);
                    }
                }
                interpreted.bulk_state_method_count += method_count;
                interpreted.last_bulk_state_method_count += method_count;
            }
            interpreted.interpreted_method_count += method_count;
            index += method_count;
            return;
        }
    }
    for (uint32_t method_index = 0; method_index < method_count; ++method_index) {
        if (index >= words.size()) {
            interpreted.pending_method_packet = true;
            interpreted.pending_long_count_word = false;
            interpreted.pending_method_non_increasing = non_increasing;
            interpreted.pending_first_method = first_method;
            interpreted.pending_method_count = method_count;
            interpreted.pending_method_index = method_index;
            interpreted.pending_next_address = words[index - 1u].address + 4u;
            interpreted.pending_method_subchannel = subchannel;
            return;
        }
        if (push_buffer_word_starts_run(words, index)) {
            ++interpreted.truncated_packet_count;
            return;
        }
        const uint32_t method = non_increasing ? first_method : first_method + method_index * 4u;
        const uint32_t data = words[index].value;
        update_d3d_state_seed(interpreted, method);
        update_d3d_state_seed(interpreted, data);
        interpret_nv2a_method(method, data, interpreted);
        ++interpreted.interpreted_method_count;
        ++index;
    }
}


void interpret_long_non_increasing_packet(
    const std::vector<PushBufferWord>& words,
    size_t& index,
    InterpretedD3DStream& interpreted) {
    const uint32_t command = words[index].value;
    const uint32_t first_method = ((command >> 2u) & 0x7FFu) * 4u;
    const uint32_t subchannel = (command >> 13u) & 0x7u;
    update_d3d_state_seed(interpreted, command);
    ++index;
    if (index >= words.size()) {
        interpreted.pending_method_packet = true;
        interpreted.pending_long_count_word = true;
        interpreted.pending_method_non_increasing = true;
        interpreted.pending_first_method = first_method;
        interpreted.pending_method_count = 0u;
        interpreted.pending_method_index = 0u;
        interpreted.pending_next_address = words[index - 1u].address + 4u;
        interpreted.pending_method_subchannel = subchannel;
        return;
    }
    if (push_buffer_word_starts_run(words, index)) {
        ++interpreted.truncated_packet_count;
        return;
    }
    const uint32_t method_count = words[index].value & 0x00FFFFFFu;
    update_d3d_state_seed(interpreted, method_count);
    ++index;
    if (method_count == 0) {
        ++interpreted.zero_count_method_word_count;
        return;
    }
    ++interpreted.method_packet_count;
    for (uint32_t method_index = 0; method_index < method_count; ++method_index) {
        if (index >= words.size()) {
            interpreted.pending_method_packet = true;
            interpreted.pending_long_count_word = false;
            interpreted.pending_method_non_increasing = true;
            interpreted.pending_first_method = first_method;
            interpreted.pending_method_count = method_count;
            interpreted.pending_method_index = method_index;
            interpreted.pending_next_address = words[index - 1u].address + 4u;
            interpreted.pending_method_subchannel = subchannel;
            return;
        }
        if (push_buffer_word_starts_run(words, index)) {
            ++interpreted.truncated_packet_count;
            return;
        }
        interpret_nv2a_method(first_method, words[index].value, interpreted);
        update_d3d_state_seed(interpreted, first_method);
        update_d3d_state_seed(interpreted, words[index].value);
        ++interpreted.interpreted_method_count;
        ++index;
    }
}


void interpret_pending_push_buffer_method_packet(
    const std::vector<PushBufferWord>& words,
    size_t& index,
    InterpretedD3DStream& interpreted) {
    if (!interpreted.pending_method_packet || index >= words.size()) {
        return;
    }
    if (words[index].address != interpreted.pending_next_address) {
        ++interpreted.truncated_packet_count;
        interpreted.pending_method_packet = false;
        interpreted.pending_long_count_word = false;
        return;
    }
    if (interpreted.pending_long_count_word) {
        const uint32_t method_count = words[index].value & 0x00FFFFFFu;
        update_d3d_state_seed(interpreted, method_count);
        interpreted.pending_long_count_word = false;
        interpreted.pending_method_count = method_count;
        interpreted.pending_method_index = 0u;
        interpreted.pending_next_address = words[index].address + 4u;
        ++index;
        if (method_count == 0u) {
            ++interpreted.zero_count_method_word_count;
            interpreted.pending_method_packet = false;
            return;
        }
        ++interpreted.method_packet_count;
    }
    while (interpreted.pending_method_index < interpreted.pending_method_count) {
        if (index >= words.size()) {
            return;
        }
        if (words[index].address != interpreted.pending_next_address) {
            ++interpreted.truncated_packet_count;
            interpreted.pending_method_packet = false;
            return;
        }
        const uint32_t method_index = interpreted.pending_method_index;
        const uint32_t method = interpreted.pending_method_non_increasing
            ? interpreted.pending_first_method
            : interpreted.pending_first_method + method_index * 4u;
        const uint32_t data = words[index].value;
        update_d3d_state_seed(interpreted, method);
        update_d3d_state_seed(interpreted, data);
        interpret_nv2a_method(method, data, interpreted);
        ++interpreted.interpreted_method_count;
        ++interpreted.pending_method_index;
        interpreted.pending_next_address = words[index].address + 4u;
        ++index;
    }
    interpreted.pending_method_packet = false;
}


RecoveredVertexResourceIndex build_recovered_vertex_resource_index(
    const std::vector<RecoveredTextureResource>& resources) {
    RecoveredVertexResourceIndex index;
    for (const RecoveredTextureResource& resource : resources) {
        if (resource.format != "VERTEX_BUFFER" || resource.payload.empty()) {
            continue;
        }
        const uint64_t resource_begin = resource.address;
        const uint64_t resource_end = resource_begin + resource.payload.size();
        const uint64_t first_page = resource_begin >> 12u;
        const uint64_t last_page = (resource_end - 1u) >> 12u;
        for (uint64_t page = first_page; page <= last_page; ++page) {
            index[static_cast<uint32_t>(page)].push_back(&resource);
        }
    }
    return index;
}


const RecoveredTextureResource* recovered_vertex_resource(
    const RecoveredVertexResourceIndex& resource_index,
    uint32_t address,
    uint32_t size) {
    const uint64_t requested_begin = address;
    const uint64_t requested_end = requested_begin + size;
    const auto candidates = resource_index.find(address >> 12u);
    if (candidates == resource_index.end()) {
        return nullptr;
    }
    for (const RecoveredTextureResource* resource : candidates->second) {
        const uint64_t resource_begin = resource->address;
        const uint64_t resource_end = resource_begin + resource->payload.size();
        if (requested_begin < resource_begin || requested_end > resource_end) {
            continue;
        }
        return resource;
    }
    return nullptr;
}


const uint8_t* recovered_vertex_bytes(
    const RecoveredVertexResourceIndex& resource_index,
    uint32_t address,
    uint32_t size) {
    const RecoveredTextureResource* resource = recovered_vertex_resource(
        resource_index, address, size);
    return resource == nullptr
        ? nullptr
        : resource->payload.data()
            + static_cast<size_t>(address - resource->address);
}


int32_t sign_extend_vertex_component(uint32_t value, uint32_t bits) {
    const uint32_t shift = 32u - bits;
    return static_cast<int32_t>(value << shift) >> shift;
}


bool decode_indexed_vertex_attribute_payload(
    const uint8_t* payload,
    uint32_t format_raw,
    std::array<float, 4>& output) {
    const uint32_t type = format_raw & 0xFu;
    const uint32_t components = (format_raw >> 4u) & 0xFu;
    if (payload == nullptr || components == 0u) {
        return false;
    }
    if (type == 2u) {
        for (uint32_t component = 0; component < std::min(components, 4u); ++component) {
            std::memcpy(&output[component], payload + component * 4u, sizeof(float));
        }
        return true;
    }
    if (type == 0u) {
        output = {
            static_cast<float>(payload[2]) / 255.0f,
            static_cast<float>(payload[1]) / 255.0f,
            static_cast<float>(payload[0]) / 255.0f,
            static_cast<float>(payload[3]) / 255.0f,
        };
        return true;
    }
    if (type == 4u) {
        for (uint32_t component = 0; component < std::min(components, 4u); ++component) {
            output[component] = static_cast<float>(payload[component]) / 255.0f;
        }
        return true;
    }
    if (type == 1u || type == 5u) {
        for (uint32_t component = 0; component < std::min(components, 4u); ++component) {
            int16_t value = 0;
            std::memcpy(&value, payload + component * 2u, sizeof(value));
            output[component] = std::max(-1.0f, static_cast<float>(value) / 32767.0f);
        }
        return true;
    }
    if (type == 6u) {
        uint32_t packed = 0;
        std::memcpy(&packed, payload, sizeof(packed));
        output[0] = std::max(
            -1.0f,
            static_cast<float>(sign_extend_vertex_component(packed & 0x7FFu, 11u))
                / 1023.0f);
        output[1] = std::max(
            -1.0f,
            static_cast<float>(sign_extend_vertex_component((packed >> 11u) & 0x7FFu, 11u))
                / 1023.0f);
        output[2] = std::max(
            -1.0f,
            static_cast<float>(sign_extend_vertex_component((packed >> 22u) & 0x3FFu, 10u))
                / 511.0f);
        return true;
    }
    return false;
}


bool decode_indexed_vertex_attribute(
    const RecoveredVertexResourceIndex& resource_index,
    uint32_t address,
    uint32_t format_raw,
    std::array<float, 4>& output) {
    return decode_indexed_vertex_attribute_payload(
        recovered_vertex_bytes(
            resource_index,
            address,
            nv2a_vertex_element_size(format_raw)),
        format_raw,
        output);
}


bool prepare_gpu_raw_attribute_fetch(
    NativeDraw& draw,
    const RecoveredVertexResourceIndex& resource_index,
    const std::unordered_map<const RecoveredTextureResource*, uint32_t>&
        packed_resource_offsets,
    uint32_t raw_index_word_base,
    std::vector<uint32_t>& raw_indices) {
    if (!gpu_vertex_program_compatible(draw)
        || draw.vertex_indices.empty()
        || static_cast<uint64_t>(raw_index_word_base)
                + raw_indices.size() + draw.vertex_indices.size()
            > std::numeric_limits<uint32_t>::max()) {
        return false;
    }
    const auto [minimum_index, maximum_index] = std::minmax_element(
        draw.vertex_indices.begin(), draw.vertex_indices.end());
    std::array<uint32_t, 16> base_offsets{};
    for (uint32_t slot = 0; slot < draw.vertex_formats.size(); ++slot) {
        const uint32_t format_raw = draw.vertex_formats[slot];
        const uint32_t type = format_raw & 0xFu;
        const uint32_t element_size = nv2a_vertex_element_size(format_raw);
        if (element_size == 0u) {
            continue;
        }
        if ((type != 0u && type != 1u && type != 2u && type != 4u
                && type != 5u && type != 6u)
            || draw.vertex_offsets[slot] == 0u
            || (format_raw >> 8u) == 0u) {
            return false;
        }
        const uint64_t minimum_address = static_cast<uint64_t>(
            draw.vertex_offsets[slot])
            + static_cast<uint64_t>(*minimum_index) * (format_raw >> 8u);
        const uint64_t maximum_address = static_cast<uint64_t>(
            draw.vertex_offsets[slot])
            + static_cast<uint64_t>(*maximum_index) * (format_raw >> 8u);
        if (maximum_address > std::numeric_limits<uint32_t>::max()) {
            return false;
        }
        // A vertex resource is one contiguous address interval. If both index
        // extrema, including the final element width, resolve to the same
        // resource, every linearly-strided index between them is resident too.
        const RecoveredTextureResource* selected_resource =
            recovered_vertex_resource(
                resource_index,
                static_cast<uint32_t>(minimum_address),
                element_size);
        const RecoveredTextureResource* maximum_resource =
            recovered_vertex_resource(
                resource_index,
                static_cast<uint32_t>(maximum_address),
                element_size);
        if (selected_resource == nullptr
            || maximum_resource != selected_resource) {
            return false;
        }
        const auto packed = packed_resource_offsets.find(selected_resource);
        if (packed == packed_resource_offsets.end()) {
            return false;
        }
        // Unsigned wrap preserves packed + (array base - guest resource base)
        // for captures whose resource begins at the first referenced vertex
        // instead of the array's logical index-zero address.
        base_offsets[slot] = packed->second
            + draw.vertex_offsets[slot]
            - selected_resource->address;
    }
    draw.gpu_raw_attribute_fetch = true;
    draw.gpu_raw_source_index_base = raw_index_word_base
        + static_cast<uint32_t>(raw_indices.size());
    draw.gpu_raw_attribute_base_offsets = base_offsets;
    draw.first_vertex = 0u;
    draw.vertex_count = static_cast<uint32_t>(draw.vertex_indices.size());
    raw_indices.insert(
        raw_indices.end(),
        draw.vertex_indices.begin(),
        draw.vertex_indices.end());
    return true;
}


bool refresh_gpu_raw_vertex_resource_cache(
    const std::vector<RecoveredTextureResource>& resources,
    bool resources_unchanged,
    GpuRawVertexResourceCache& cache,
    std::unordered_map<const RecoveredTextureResource*, uint32_t>&
        packed_resource_offsets) {
    const auto refresh_begin = std::chrono::steady_clock::now();
    std::vector<const RecoveredTextureResource*> vertex_resources;
    std::vector<GpuRawVertexResourceLayout> next_layout;
    uint64_t packed_size = 0u;
    for (const RecoveredTextureResource& resource : resources) {
        if (resource.format != "VERTEX_BUFFER" || resource.payload.empty()) {
            continue;
        }
        packed_size = (packed_size + 3u) & ~uint64_t{3u};
        if (packed_size > std::numeric_limits<uint32_t>::max()
            || resource.payload.size()
                > std::numeric_limits<uint32_t>::max() - packed_size) {
            cache = GpuRawVertexResourceCache{};
            cache.refresh_us = std::chrono::duration_cast<
                std::chrono::microseconds>(
                    std::chrono::steady_clock::now() - refresh_begin).count();
            packed_resource_offsets.clear();
            return false;
        }
        vertex_resources.push_back(&resource);
        next_layout.push_back({
            resource.address,
            static_cast<uint32_t>(packed_size),
            static_cast<uint32_t>(resource.payload.size()),
        });
        packed_size += resource.payload.size();
    }
    packed_size = (packed_size + 3u) & ~uint64_t{3u};
    if (packed_size > std::numeric_limits<uint32_t>::max()) {
        cache = GpuRawVertexResourceCache{};
        cache.refresh_us = std::chrono::duration_cast<
            std::chrono::microseconds>(
                std::chrono::steady_clock::now() - refresh_begin).count();
        packed_resource_offsets.clear();
        return false;
    }

    ++cache.refresh_count;
    cache.layout_rebuilt = cache.layout.size() != next_layout.size()
        || cache.gpu_raw_vertex_bytes.size() != packed_size;
    if (!cache.layout_rebuilt) {
        for (size_t index = 0u; index < next_layout.size(); ++index) {
            const GpuRawVertexResourceLayout& current = cache.layout[index];
            const GpuRawVertexResourceLayout& next = next_layout[index];
            if (current.guest_address != next.guest_address
                || current.packed_offset != next.packed_offset
                || current.byte_size != next.byte_size) {
                cache.layout_rebuilt = true;
                break;
            }
        }
    }
    cache.resource_count = static_cast<uint32_t>(vertex_resources.size());
    cache.compared_resource_count = 0u;
    cache.changed_resource_count = 0u;
    cache.reused_resource_count = 0u;
    cache.compared_bytes = 0u;
    cache.dirty_bytes = 0u;
    cache.dirty_ranges.clear();

    if (cache.layout_rebuilt) {
        ++cache.layout_rebuild_count;
        cache.layout = next_layout;
        cache.gpu_raw_vertex_bytes.assign(
            static_cast<size_t>(packed_size), 0u);
        for (size_t index = 0u; index < vertex_resources.size(); ++index) {
            const RecoveredTextureResource& resource = *vertex_resources[index];
            std::memcpy(
                cache.gpu_raw_vertex_bytes.data()
                    + cache.layout[index].packed_offset,
                resource.payload.data(),
                resource.payload.size());
        }
        cache.changed_resource_count = cache.resource_count;
        if (packed_size != 0u) {
            append_dirty_range(
                cache.dirty_ranges, 0u, static_cast<uint32_t>(packed_size));
        }
    } else if (resources_unchanged) {
        // The live bridge retains the same immutable resource vector for an
        // unchanged generation, so neither hashing nor another byte scan is
        // needed to prove residency.
        cache.reused_resource_count = cache.resource_count;
    } else {
        constexpr size_t kCompareChunkBytes = 64u;
        for (size_t index = 0u; index < vertex_resources.size(); ++index) {
            const RecoveredTextureResource& resource = *vertex_resources[index];
            const GpuRawVertexResourceLayout& slot = cache.layout[index];
            ++cache.compared_resource_count;
            cache.compared_bytes += resource.payload.size();
            uint8_t* resident_payload = cache.gpu_raw_vertex_bytes.data()
                + slot.packed_offset;
            if (std::memcmp(
                    resident_payload,
                    resource.payload.data(),
                    resource.payload.size()) == 0) {
                ++cache.reused_resource_count;
                continue;
            }
            ++cache.changed_resource_count;
            for (size_t local_offset = 0u;
                 local_offset < resource.payload.size();
                 local_offset += kCompareChunkBytes) {
                const size_t chunk_size = std::min(
                    kCompareChunkBytes,
                    resource.payload.size() - local_offset);
                uint8_t* resident = resident_payload + local_offset;
                const uint8_t* current = resource.payload.data() + local_offset;
                if (std::memcmp(resident, current, chunk_size) == 0) {
                    continue;
                }
                std::memcpy(resident, current, chunk_size);
                append_dirty_range(
                    cache.dirty_ranges,
                    slot.packed_offset + static_cast<uint32_t>(local_offset),
                    static_cast<uint32_t>(chunk_size));
            }
        }
    }
    cache.dirty_bytes = dirty_range_bytes(cache.dirty_ranges);
    for (size_t index = 0u; index < vertex_resources.size(); ++index) {
        packed_resource_offsets.emplace(
            vertex_resources[index], cache.layout[index].packed_offset);
    }
    cache.refresh_us = std::chrono::duration_cast<std::chrono::microseconds>(
        std::chrono::steady_clock::now() - refresh_begin).count();
    return true;
}


void materialize_indexed_draws(
    InterpretedD3DStream& interpreted,
    const std::vector<RecoveredTextureResource>& resources,
    GpuRawVertexResourceCache& raw_resource_cache,
    bool enable_gpu_raw_attribute_fetch,
    bool resources_unchanged) {
    const RecoveredVertexResourceIndex resource_index =
        build_recovered_vertex_resource_index(resources);
    std::unordered_map<const RecoveredTextureResource*, uint32_t>
        packed_resource_offsets;
    if (enable_gpu_raw_attribute_fetch
        && !refresh_gpu_raw_vertex_resource_cache(
            resources,
            resources_unchanged,
            raw_resource_cache,
            packed_resource_offsets)) {
        enable_gpu_raw_attribute_fetch = false;
    }
    interpreted.gpu_raw_vertex_indices.clear();
    interpreted.gpu_raw_attribute_draw_count = 0u;
    interpreted.gpu_raw_attribute_vertex_count = 0u;
    for (NativeDraw& draw : interpreted.draws) {
        if (!draw.indexed_array || draw.vertex_indices.empty()) {
            continue;
        }
        const bool was_raw = draw.gpu_raw_attribute_fetch;
        const bool was_materialized = was_raw || draw.vertex_count != 0u;
        if (was_raw) {
            draw.vertex_count = 0u;
        }
        draw.gpu_raw_attribute_fetch = false;
        draw.gpu_raw_source_index_base = 0u;
        draw.gpu_raw_attribute_base_offsets = {};
        if (enable_gpu_raw_attribute_fetch
            && prepare_gpu_raw_attribute_fetch(
                draw,
                resource_index,
                packed_resource_offsets,
                static_cast<uint32_t>(
                    raw_resource_cache.gpu_raw_vertex_bytes.size()
                        / sizeof(uint32_t)),
                interpreted.gpu_raw_vertex_indices)) {
            ++interpreted.gpu_raw_attribute_draw_count;
            interpreted.gpu_raw_attribute_vertex_count += draw.vertex_count;
            if (!was_materialized) {
                ++interpreted.materialized_indexed_draw_count;
                interpreted.materialized_indexed_vertex_count +=
                    draw.vertex_count;
            }
            continue;
        }
        if (draw.vertex_count != 0u) {
            continue;
        }
        std::vector<NativeVertex> decoded;
        decoded.reserve(draw.vertex_indices.size());
        bool complete = true;
        for (const uint32_t vertex_index : draw.vertex_indices) {
            NativeVertex vertex{};
            vertex.program_inputs = draw.current_vertex_attributes;
            for (uint32_t slot = 0; slot < draw.vertex_formats.size(); ++slot) {
                const uint32_t format_raw = draw.vertex_formats[slot];
                const uint32_t element_size = nv2a_vertex_element_size(format_raw);
                const uint32_t stride = format_raw >> 8u;
                if (element_size == 0u) {
                    continue;
                }
                if (draw.vertex_offsets[slot] == 0u || stride == 0u) {
                    complete = false;
                    break;
                }
                const uint64_t attribute_address = static_cast<uint64_t>(
                    draw.vertex_offsets[slot])
                    + static_cast<uint64_t>(vertex_index) * stride;
                if (attribute_address > std::numeric_limits<uint32_t>::max()
                    || !decode_indexed_vertex_attribute(
                        resource_index,
                        static_cast<uint32_t>(attribute_address),
                        format_raw,
                        vertex.program_inputs[slot])) {
                    complete = false;
                    break;
                }
            }
            if (!complete) {
                break;
            }
            vertex.program_inputs_valid = true;
            const auto& position = vertex.program_inputs[0];
            vertex.x = position[0];
            vertex.y = position[1];
            vertex.z = position[2];
            vertex.w = position[3];
            const auto& diffuse = vertex.program_inputs[3];
            vertex.r = diffuse[0];
            vertex.g = diffuse[1];
            vertex.b = diffuse[2];
            vertex.a = diffuse[3];
            const auto& secondary = vertex.program_inputs[4];
            vertex.secondary_r = secondary[0];
            vertex.secondary_g = secondary[1];
            vertex.secondary_b = secondary[2];
            vertex.secondary_a = secondary[3];
            const auto& texture = vertex.program_inputs[
                std::min<uint32_t>(9u + draw.texture_stage, 15u)];
            vertex.u = texture[0];
            vertex.v = texture[1];
            vertex.texture_r = texture[2];
            vertex.texture_q = texture[3];
            for (uint32_t stage = 0u; stage < 4u; ++stage) {
                vertex.texture_coordinates[stage] =
                    vertex.program_inputs[9u + stage];
            }
            decoded.push_back(std::move(vertex));
        }
        if (!complete || decoded.empty()) {
            ++interpreted.missing_indexed_resource_draw_count;
            continue;
        }
        draw.first_vertex = static_cast<uint32_t>(interpreted.vertices.size());
        draw.vertex_count = static_cast<uint32_t>(decoded.size());
        interpreted.vertices.insert(
            interpreted.vertices.end(), decoded.begin(), decoded.end());
        ++interpreted.materialized_indexed_draw_count;
        interpreted.materialized_indexed_vertex_count += draw.vertex_count;
    }
}


GpuRawAttributeValidation validate_gpu_raw_attribute_fetch(
    const InterpretedD3DStream& cpu_stream,
    const std::vector<RecoveredTextureResource>& resources) {
    InterpretedD3DStream gpu_stream = cpu_stream;
    GpuRawVertexResourceCache raw_resource_cache;
    materialize_indexed_draws(
        gpu_stream, resources, raw_resource_cache, true, false);
    GpuRawAttributeValidation validation{};
    validation.packed_resource_bytes =
        raw_resource_cache.gpu_raw_vertex_bytes.size();
    const size_t first_draw = std::min<size_t>(
        cpu_stream.presented_draw_begin, cpu_stream.draws.size());
    const size_t end_draw = std::min<size_t>(
        first_draw + cpu_stream.presented_draw_count,
        cpu_stream.draws.size());
    for (size_t draw_index = first_draw;
         draw_index < end_draw;
         ++draw_index) {
        const NativeDraw& cpu_draw = cpu_stream.draws[draw_index];
        const NativeDraw& gpu_draw = gpu_stream.draws[draw_index];
        if (!gpu_vertex_program_compatible(cpu_draw)) {
            continue;
        }
        if (!gpu_draw.gpu_raw_attribute_fetch) {
            ++validation.mapping_fallback_draw_count;
            continue;
        }
        ++validation.eligible_draw_count;
        validation.eligible_vertex_count += gpu_draw.vertex_count;
        if (cpu_draw.first_vertex + cpu_draw.vertex_count
            > cpu_stream.vertices.size()) {
            validation.mismatch_vertex_count += gpu_draw.vertex_count;
            continue;
        }
        for (uint32_t vertex_index = 0u;
             vertex_index < gpu_draw.vertex_count;
             ++vertex_index) {
            Nv2aVertexAttributes attributes =
                cpu_draw.current_vertex_attributes;
            bool decoded = true;
            const uint64_t index_word = static_cast<uint64_t>(
                gpu_draw.gpu_raw_source_index_base) + vertex_index;
            const uint64_t first_index_word =
                raw_resource_cache.gpu_raw_vertex_bytes.size()
                    / sizeof(uint32_t);
            if (index_word < first_index_word
                || index_word - first_index_word
                    >= gpu_stream.gpu_raw_vertex_indices.size()) {
                ++validation.compared_vertex_count;
                ++validation.mismatch_vertex_count;
                continue;
            }
            const uint32_t source_index =
                gpu_stream.gpu_raw_vertex_indices[
                    static_cast<size_t>(index_word - first_index_word)];
            for (uint32_t slot = 0u; slot < 16u; ++slot) {
                const uint32_t format_raw = gpu_draw.vertex_formats[slot];
                const uint32_t element_size =
                    nv2a_vertex_element_size(format_raw);
                if (element_size == 0u) {
                    continue;
                }
                const uint32_t packed_offset =
                    gpu_draw.gpu_raw_attribute_base_offsets[slot]
                    + source_index * (format_raw >> 8u);
                if (static_cast<uint64_t>(packed_offset) + element_size
                        > raw_resource_cache.gpu_raw_vertex_bytes.size()
                    || !decode_indexed_vertex_attribute_payload(
                        raw_resource_cache.gpu_raw_vertex_bytes.data()
                            + packed_offset,
                        format_raw,
                        attributes[slot])) {
                    decoded = false;
                    break;
                }
                ++validation.decoded_attribute_count;
            }
            ++validation.compared_vertex_count;
            const NativeVertex& reference = cpu_stream.vertices[
                cpu_draw.first_vertex + vertex_index];
            if (!decoded
                || std::memcmp(
                    attributes.data(),
                    reference.program_inputs.data(),
                    sizeof(attributes)) != 0) {
                ++validation.mismatch_vertex_count;
            }
        }
    }
    return validation;
}


GpuRawDirtyRangeValidation validate_gpu_raw_dirty_range_uploads() {
    std::vector<RecoveredTextureResource> resources(2u);
    resources[0].address = 0x1000u;
    resources[0].format = "VERTEX_BUFFER";
    resources[0].payload.resize(192u);
    resources[1].address = 0x2000u;
    resources[1].format = "VERTEX_BUFFER";
    resources[1].payload.resize(192u);
    for (size_t index = 0u; index < resources[0].payload.size(); ++index) {
        resources[0].payload[index] = static_cast<uint8_t>(index * 13u + 7u);
        resources[1].payload[index] = static_cast<uint8_t>(index * 29u + 3u);
    }

    GpuRawVertexResourceCache cache;
    std::unordered_map<const RecoveredTextureResource*, uint32_t> offsets;
    refresh_gpu_raw_vertex_resource_cache(resources, false, cache, offsets);
    const std::vector<uint8_t> initial_bytes = cache.gpu_raw_vertex_bytes;
    cache.dirty_ranges.clear();
    offsets.clear();
    refresh_gpu_raw_vertex_resource_cache(resources, false, cache, offsets);

    GpuRawDirtyRangeValidation validation{};
    validation.unchanged_generation_passed = !cache.layout_rebuilt
        && cache.dirty_ranges.empty()
        && cache.changed_resource_count == 0u
        && cache.reused_resource_count == resources.size()
        && cache.gpu_raw_vertex_bytes == initial_bytes;

    resources[0].payload[3] ^= 0x5Au;
    resources[0].payload[130] ^= 0xA5u;
    resources[1].payload[20] ^= 0x3Cu;
    const std::vector<uint8_t> before_dirty_upload =
        cache.gpu_raw_vertex_bytes;
    offsets.clear();
    refresh_gpu_raw_vertex_resource_cache(resources, false, cache, offsets);
    const std::vector<GpuRawVertexDirtyRange> changed_ranges =
        cache.dirty_ranges;
    std::vector<uint8_t> replayed_bytes = before_dirty_upload;
    for (const GpuRawVertexDirtyRange& range : changed_ranges) {
        if (static_cast<uint64_t>(range.offset) + range.size
            > replayed_bytes.size()) {
            replayed_bytes.clear();
            break;
        }
        std::memcpy(
            replayed_bytes.data() + range.offset,
            cache.gpu_raw_vertex_bytes.data() + range.offset,
            range.size);
    }
    GpuRawVertexResourceCache clean_repack;
    std::unordered_map<const RecoveredTextureResource*, uint32_t>
        clean_offsets;
    refresh_gpu_raw_vertex_resource_cache(
        resources, false, clean_repack, clean_offsets);
    validation.changed_range_count = static_cast<uint32_t>(
        changed_ranges.size());
    validation.changed_upload_bytes = cache.dirty_bytes;
    validation.packed_resource_bytes = cache.gpu_raw_vertex_bytes.size();
    validation.changed_generation_passed = !cache.layout_rebuilt
        && cache.changed_resource_count == 2u
        && !changed_ranges.empty()
        && cache.dirty_bytes < cache.gpu_raw_vertex_bytes.size()
        && cache.gpu_raw_vertex_bytes == clean_repack.gpu_raw_vertex_bytes
        && replayed_bytes == cache.gpu_raw_vertex_bytes;

    resources[1].payload.push_back(0x91u);
    offsets.clear();
    refresh_gpu_raw_vertex_resource_cache(resources, false, cache, offsets);
    clean_repack = GpuRawVertexResourceCache{};
    clean_offsets.clear();
    refresh_gpu_raw_vertex_resource_cache(
        resources, false, clean_repack, clean_offsets);
    validation.layout_change_passed = cache.layout_rebuilt
        && cache.dirty_ranges.size() == 1u
        && cache.dirty_bytes == cache.gpu_raw_vertex_bytes.size()
        && cache.gpu_raw_vertex_bytes == clean_repack.gpu_raw_vertex_bytes;
    return validation;
}

}  // namespace b2r::host::vulkan_detail
