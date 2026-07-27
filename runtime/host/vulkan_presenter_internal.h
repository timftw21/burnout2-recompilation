#pragma once

#define NOMINMAX
#define WIN32_LEAN_AND_MEAN

#include <windows.h>
#include <vulkan/vulkan.h>

#include <algorithm>
#include <array>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstddef>
#include <cstring>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <optional>
#include <regex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <system_error>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

#include "dirty_ranges.h"
#include "frame_metrics.h"
#include "live_presenter_transport.h"
#include "live_transport_layout.h"
#include "native_pipeline_state.h"
#include "presenter_debug_log.h"
#include "presenter_metrics.h"
#include "presenter_options.h"
#include "vulkan_presenter.h"
#include "../nv2a/texture_layout.h"
#include "../platform/sdl/sdl_platform.h"
#include "nv2a_vertex_program.h"

namespace b2r::host::vulkan_detail {

using b2r::host::CompletedFlipFpsSampler;
using b2r::host::DirtyRange;
using b2r::host::NativePipelineState;
using b2r::host::NativePipelineStateHash;
using DebugLog = b2r::host::PresenterDebugLog;
using Options = b2r::host::PresenterOptions;
using b2r::host::LivePresenterTransport;
using b2r::host::PresenterMetricsReporter;
using b2r::host::PresenterMetricsSnapshot;
using b2r::platform::ControllerMetadata;
using b2r::platform::ControllerState;
using b2r::platform::PlatformEventType;
using b2r::platform::PlatformPollResult;
using b2r::platform::SdlPlatform;
using b2r::host::append_dirty_range;
using b2r::host::dirty_range_bytes;
using namespace b2r::live_transport;
using b2r::nv2a::nv2a_canonical_resource_address;
using b2r::nv2a::nv2a_texture_extent;
using b2r::nv2a::nv2a_texture_format_is_linear;
using b2r::nv2a::nv2a_texture_format_matches;
using b2r::nv2a::nv2a_unswizzle_texture_2d;

constexpr int64_t kTargetFrameUs = 16667;
constexpr auto kTargetFrameInterval = std::chrono::nanoseconds(16666667);
constexpr uint32_t kTargetGuestFrameRateHz = 60u;
constexpr DWORD kHighResolutionWaitableTimerFlag = 0x00000002u;
constexpr uint32_t kRecoveredPushBufferBase = 0x80000000u;
constexpr uint32_t kRecoveredPushBufferApertureSize = 0x01000000u;
constexpr uint32_t kRecoveredPushBufferEnd =
    kRecoveredPushBufferBase + kRecoveredPushBufferApertureSize;
std::string narrow(const std::wstring& value);

std::wstring widen(const std::string& value);

std::optional<std::string> read_text_handle_shared(HANDLE file);

std::optional<std::string> read_text_file_shared(
    const std::filesystem::path& path);

std::string escape_json(const std::string& value);

std::string json_string(const std::string& value);

std::string json_bool(bool value);

std::string json_float(float value);

template <size_t Size>
std::string json_u32_array(const std::array<uint32_t, Size>& values) {
    std::ostringstream out;
    out << '[';
    for (size_t index = 0; index < values.size(); ++index) {
        if (index != 0u) {
            out << ',';
        }
        out << values[index];
    }
    out << ']';
    return out.str();
}

inline std::string json_u32_vector(const std::vector<uint32_t>& values) {
    std::ostringstream out;
    out << '[';
    for (size_t index = 0; index < values.size(); ++index) {
        if (index != 0u) {
            out << ',';
        }
        out << values[index];
    }
    out << ']';
    return out.str();
}

inline void vk_check(VkResult result, const char* operation) {
    if (result != VK_SUCCESS) {
        std::ostringstream message;
        message << operation << " failed with VkResult " << result;
        throw std::runtime_error(message.str());
    }
}

struct QueueFamilySelection {
    uint32_t index = 0;
    bool supports_compute = false;
};

struct SwapchainSupport {
    VkSurfaceCapabilitiesKHR capabilities{};
    std::vector<VkSurfaceFormatKHR> formats;
    std::vector<VkPresentModeKHR> present_modes;
};

enum class RecoveredD3DCommandKind {
    MmioWrite,
    PushBufferWrite,
};

struct RecoveredD3DCommand {
    RecoveredD3DCommandKind kind = RecoveredD3DCommandKind::MmioWrite;
    uint32_t address = 0;
    uint32_t value = 0;
    uint32_t size = 4;
    std::array<uint8_t, 8> payload{};
    uint8_t payload_size = 0;
};

struct RecoveredD3DPayloadView {
    const uint8_t* bytes = nullptr;
    size_t length = 0u;

    const uint8_t* data() const { return bytes; }
    size_t size() const { return length; }
};

struct RecoveredD3DCommandSpan {
    RecoveredD3DCommandKind kind = RecoveredD3DCommandKind::MmioWrite;
    uint32_t address = 0u;
    uint32_t value = 0u;
    uint32_t size = 0u;
    RecoveredD3DPayloadView payload{};
    size_t payload_size = 0u;
    uint32_t logical_write_count = 0u;
    uint8_t flags = 0u;
};

constexpr size_t kRecoveredD3DCommandRecordSize = 16u;
constexpr size_t kRecoveredD3DCommandSpanHeaderSize = 16u;
constexpr size_t kRenderTargetFeedbackImageCacheCapacity = 8u;

struct PushBufferWord {
    uint32_t address = 0;
    uint32_t value = 0;
    uint32_t run_id = 0;
};

bool push_buffer_word_starts_run(
    const std::vector<PushBufferWord>& words,
    size_t index);

struct NativeVertex {
    uint32_t raw_x_bits = 0;
    uint32_t raw_y_bits = 0;
    float x = 0.0f;
    float y = 0.0f;
    float z = 0.0f;
    float w = 1.0f;
    float r = 1.0f;
    float g = 1.0f;
    float b = 1.0f;
    float a = 1.0f;
    float secondary_r = 0.0f;
    float secondary_g = 0.0f;
    float secondary_b = 0.0f;
    float secondary_a = 1.0f;
    float fog = 1.0f;
    float u = 0.0f;
    float v = 0.0f;
    float texture_r = 0.0f;
    float texture_q = 1.0f;
    bool program_position_valid = false;
    bool program_inputs_valid = false;
    std::array<std::array<float, 4>, 16> program_inputs{};
};

static_assert(sizeof(NativeVertex) % sizeof(uint32_t) == 0u);
static_assert(offsetof(NativeVertex, program_inputs) % 16u == 0u);

using Nv2aVertexAttributes = std::array<std::array<float, 4>, 16>;

Nv2aVertexAttributes default_nv2a_vertex_attributes();

struct NativeDraw {
    uint32_t first_vertex = 0;
    uint32_t vertex_count = 0;
    uint32_t primitive = 0;
    uint32_t texture_address = 0;
    bool texture_enabled = false;
    uint32_t texture_stage = 0;
    std::array<uint32_t, 4> texture_offsets{};
    std::array<uint32_t, 4> texture_addresses{};
    std::array<uint32_t, 4> texture_formats{};
    std::array<uint32_t, 4> texture_image_rects{};
    std::array<uint32_t, 4> texture_controls{};
    std::array<uint32_t, 4> texture_filters{};
    std::array<uint32_t, 16> vertex_offsets{};
    std::array<uint32_t, 16> vertex_formats{};
    Nv2aVertexAttributes current_vertex_attributes =
        default_nv2a_vertex_attributes();
    std::vector<uint32_t> vertex_indices;
    bool indexed_array = false;
    bool gpu_raw_attribute_fetch = false;
    uint32_t gpu_raw_source_index_base = 0;
    std::array<uint32_t, 16> gpu_raw_attribute_base_offsets{};
    uint32_t blend_enable = 0;
    uint32_t blend_source_factor = 1;
    uint32_t blend_destination_factor = 0;
    uint32_t blend_equation = 0x8006;
    uint32_t alpha_test_enable = 0;
    uint32_t alpha_function = 0x0207;
    uint32_t alpha_reference = 0;
    uint32_t surface_format = 0;
    uint32_t surface_pitch = 0;
    uint32_t surface_color_offset = 0;
    uint32_t surface_clip_horizontal = 0;
    uint32_t surface_clip_vertical = 0;
    uint32_t color_mask = 0x01010101;
    uint32_t depth_test_enable = 0;
    uint32_t depth_function = 0x0203;
    uint32_t depth_write_enable = 1;
    uint32_t polygon_offset_fill_enable = 0;
    uint32_t polygon_offset_scale_factor = 0;
    uint32_t polygon_offset_bias = 0;
    uint32_t skin_mode = 0;
    uint32_t specular_enable = 0;
    uint32_t cull_face_enable = 0;
    uint32_t cull_face = 0x0405;
    uint32_t front_face = 0x0900;
    uint32_t shader_stage_program = 0;
    uint32_t combiner_control = 0;
    std::array<uint32_t, 8> combiner_color_inputs{};
    std::array<uint32_t, 8> combiner_color_outputs{};
    std::array<uint32_t, 8> combiner_alpha_inputs{};
    std::array<uint32_t, 8> combiner_alpha_outputs{};
    std::array<uint32_t, 8> combiner_factors0{};
    std::array<uint32_t, 8> combiner_factors1{};
    uint32_t final_combiner_inputs0 = 0;
    uint32_t final_combiner_inputs1 = 0;
    std::array<uint32_t, 2> final_combiner_factors{};
    uint32_t fog_mode = 0;
    uint32_t fog_generation_mode = 0;
    uint32_t fog_enable = 0;
    uint32_t fog_color = 0;
    std::array<uint32_t, 3> fog_params{};
    uint32_t transform_execution_mode = 0;
    uint32_t transform_program_start = 0;
    std::array<std::array<uint32_t, 4>, 136> transform_program{};
    std::array<std::array<uint32_t, 4>, 192> transform_constants{};
};

struct PositionBounds {
    bool valid = false;
    float min_x = 0.0f;
    float max_x = 0.0f;
    float min_y = 0.0f;
    float max_y = 0.0f;

    void include(float x, float y) {
        if (!std::isfinite(x) || !std::isfinite(y)) {
            return;
        }
        if (!valid) {
            min_x = max_x = x;
            min_y = max_y = y;
            valid = true;
            return;
        }
        min_x = std::min(min_x, x);
        max_x = std::max(max_x, x);
        min_y = std::min(min_y, y);
        max_y = std::max(max_y, y);
    }

    void merge(const PositionBounds& other) {
        if (!other.valid) {
            return;
        }
        include(other.min_x, other.min_y);
        include(other.max_x, other.max_y);
    }
};

struct ScalarBounds {
    bool valid = false;
    float minimum = 0.0f;
    float maximum = 0.0f;

    void include(float value) {
        if (!std::isfinite(value)) {
            return;
        }
        if (!valid) {
            valid = true;
            minimum = maximum = value;
            return;
        }
        minimum = std::min(minimum, value);
        maximum = std::max(maximum, value);
    }

    void merge(const ScalarBounds& other) {
        if (!other.valid) {
            return;
        }
        include(other.minimum);
        include(other.maximum);
    }
};

struct PresentedVertexTransformDiagnostics {
    uint64_t eligible_vertex_count = 0;
    uint64_t valid_program_vertex_count = 0;
    uint64_t invalid_program_vertex_count = 0;
    uint64_t fixed_function_vertex_count = 0;
    uint64_t fixed_function_transformed_vertex_count = 0;
    uint64_t invalid_fixed_function_vertex_count = 0;
    uint64_t position_output_vertex_count = 0;
    uint64_t homogeneous_position_vertex_count = 0;
    uint64_t implicit_position_w_vertex_count = 0;
    uint64_t invalid_position_w_vertex_count = 0;
    uint64_t non_finite_position_w_vertex_count = 0;
    uint64_t defaulted_position_w_vertex_count = 0;
    uint32_t position_output_mask_union = 0;
    uint64_t screen_space_position_vertex_count = 0;
    uint64_t viewport_mapped_vertex_count = 0;
    uint64_t raw_position_fallback_vertex_count = 0;
    uint64_t near_zero_w_vertex_count = 0;
    uint64_t non_finite_position_vertex_count = 0;
    uint64_t ndc_outside_clip_vertex_count = 0;
    uint64_t ndc_extreme_vertex_count = 0;
    uint64_t diffuse_output_vertex_count = 0;
    uint64_t texture_output_vertex_count = 0;
    PositionBounds input_position_bounds{};
    ScalarBounds input_z_bounds{};
    ScalarBounds input_w_bounds{};
    PositionBounds clip_position_bounds{};
    ScalarBounds program_output_z_bounds{};
    ScalarBounds program_output_w_bounds{};
    PositionBounds ndc_position_bounds{};
    PositionBounds output_pixel_bounds{};
    bool output_color_bounds_valid = false;
    float output_min_r = 0.0f;
    float output_max_r = 0.0f;
    float output_min_g = 0.0f;
    float output_max_g = 0.0f;
    float output_min_b = 0.0f;
    float output_max_b = 0.0f;
    float output_min_a = 0.0f;
    float output_max_a = 0.0f;

    void include_output_color(float r, float g, float b, float a) {
        if (!std::isfinite(r) || !std::isfinite(g)
            || !std::isfinite(b) || !std::isfinite(a)) {
            return;
        }
        if (!output_color_bounds_valid) {
            output_color_bounds_valid = true;
            output_min_r = output_max_r = r;
            output_min_g = output_max_g = g;
            output_min_b = output_max_b = b;
            output_min_a = output_max_a = a;
            return;
        }
        output_min_r = std::min(output_min_r, r);
        output_max_r = std::max(output_max_r, r);
        output_min_g = std::min(output_min_g, g);
        output_max_g = std::max(output_max_g, g);
        output_min_b = std::min(output_min_b, b);
        output_max_b = std::max(output_max_b, b);
        output_min_a = std::min(output_min_a, a);
        output_max_a = std::max(output_max_a, a);
    }

    void merge(const PresentedVertexTransformDiagnostics& other) {
        eligible_vertex_count += other.eligible_vertex_count;
        valid_program_vertex_count += other.valid_program_vertex_count;
        invalid_program_vertex_count += other.invalid_program_vertex_count;
        fixed_function_vertex_count += other.fixed_function_vertex_count;
        fixed_function_transformed_vertex_count +=
            other.fixed_function_transformed_vertex_count;
        invalid_fixed_function_vertex_count +=
            other.invalid_fixed_function_vertex_count;
        position_output_vertex_count += other.position_output_vertex_count;
        homogeneous_position_vertex_count += other.homogeneous_position_vertex_count;
        implicit_position_w_vertex_count +=
            other.implicit_position_w_vertex_count;
        invalid_position_w_vertex_count +=
            other.invalid_position_w_vertex_count;
        non_finite_position_w_vertex_count +=
            other.non_finite_position_w_vertex_count;
        defaulted_position_w_vertex_count +=
            other.defaulted_position_w_vertex_count;
        position_output_mask_union |= other.position_output_mask_union;
        screen_space_position_vertex_count += other.screen_space_position_vertex_count;
        viewport_mapped_vertex_count += other.viewport_mapped_vertex_count;
        raw_position_fallback_vertex_count += other.raw_position_fallback_vertex_count;
        near_zero_w_vertex_count += other.near_zero_w_vertex_count;
        non_finite_position_vertex_count += other.non_finite_position_vertex_count;
        ndc_outside_clip_vertex_count += other.ndc_outside_clip_vertex_count;
        ndc_extreme_vertex_count += other.ndc_extreme_vertex_count;
        diffuse_output_vertex_count += other.diffuse_output_vertex_count;
        texture_output_vertex_count += other.texture_output_vertex_count;
        input_position_bounds.merge(other.input_position_bounds);
        input_z_bounds.merge(other.input_z_bounds);
        input_w_bounds.merge(other.input_w_bounds);
        clip_position_bounds.merge(other.clip_position_bounds);
        program_output_z_bounds.merge(other.program_output_z_bounds);
        program_output_w_bounds.merge(other.program_output_w_bounds);
        ndc_position_bounds.merge(other.ndc_position_bounds);
        output_pixel_bounds.merge(other.output_pixel_bounds);
        if (other.output_color_bounds_valid) {
            include_output_color(
                other.output_min_r,
                other.output_min_g,
                other.output_min_b,
                other.output_min_a);
            include_output_color(
                other.output_max_r,
                other.output_max_g,
                other.output_max_b,
                other.output_max_a);
        }
    }
};

struct PresentedDrawTransformDiagnostics {
    uint32_t presented_index = 0;
    uint32_t primitive = 0;
    uint32_t vertex_count = 0;
    uint32_t transform_execution_mode = 0;
    bool indexed_array = false;
    PresentedVertexTransformDiagnostics transform{};
    PositionBounds final_pixel_bounds{};
    ScalarBounds final_u_bounds{};
    ScalarBounds final_v_bounds{};
    ScalarBounds final_q_bounds{};
    ScalarBounds final_fog_bounds{};
    uint32_t positive_area_triangle_count = 0;
    uint32_t negative_area_triangle_count = 0;
    uint32_t degenerate_triangle_count = 0;
    std::vector<std::array<float, 6>> fixed_function_vertices;
};

struct FrameReadback {
    std::vector<uint8_t> bgra;
    uint64_t pixel_fingerprint = 14695981039346656037ull;
    uint32_t pixel_count = 0;
    uint32_t unique_colors = 0;
    uint32_t dominant_rgba = 0;
    uint32_t dominant_count = 0;
    uint32_t bright_count = 0;
    uint32_t dark_count = 0;
    bool near_solid = false;
    bool whiteout = false;
    bool low_information = false;

    bool visual_issue() const {
        return near_solid || whiteout || low_information;
    }
};

struct RecoveredTextureResource {
    uint32_t address = 0;
    uint32_t width = 0;
    uint32_t height = 0;
    std::string format;
    std::string content_hash;
    std::vector<uint8_t> payload;
};

struct HostTexture {
    uint32_t guest_address = 0;
    uint32_t width = 0;
    uint32_t height = 0;
    std::string format;
    std::string content_hash;
    VkFormat image_format = VK_FORMAT_R8G8B8A8_UNORM;
    uint32_t mip_levels = 1;
    bool render_target_feedback = false;
    VkImage image = VK_NULL_HANDLE;
    VkDeviceMemory memory = VK_NULL_HANDLE;
    VkImageView view = VK_NULL_HANDLE;
};

struct GpuTextureConversionMip {
    uint32_t width = 0;
    uint32_t height = 0;
    uint32_t input_byte_offset = 0;
    uint32_t output_byte_offset = 0;
    uint32_t source_output_byte_offset = 0;
    bool generated = false;
};

struct GpuTextureConversionJob {
    const RecoveredTextureResource* resource = nullptr;
    size_t host_texture_index = 0;
    uint32_t input_byte_offset = 0;
    std::vector<GpuTextureConversionMip> mips;
};

struct GpuTextureValidationCoverage {
    bool dxt1 = false;
    bool dxt5 = false;
    bool recovered_mips = false;
    bool generated_mips = false;

    bool complete() const {
        return dxt1 && dxt5 && recovered_mips && generated_mips;
    }
};

struct NativeTextureConvertPushConstants {
    uint32_t input_byte_offset = 0;
    uint32_t output_word_offset = 0;
    uint32_t source_word_offset = 0;
    uint32_t width = 0;
    uint32_t height = 0;
    uint32_t source_width = 0;
    uint32_t source_height = 0;
    uint32_t mode = 0;
};

static_assert(sizeof(NativeTextureConvertPushConstants) == 32u);

struct HostTextureBindingSpec {
    size_t texture_index = 0;
    uint32_t address = 0;
    uint32_t format = 0;
    uint32_t control = 0;
    uint32_t filter = 0;
    bool operator==(const HostTextureBindingSpec& other) const {
        return texture_index == other.texture_index
            && address == other.address
            && format == other.format
            && control == other.control
            && filter == other.filter;
    }
};

struct HostTextureBinding {
    size_t texture_index = 0;
    uint32_t address = 0;
    uint32_t format = 0;
    uint32_t control = 0;
    uint32_t filter = 0;
    uint32_t texture_mip_levels = 0;
    VkImageView texture_view = VK_NULL_HANDLE;
    VkSampler sampler = VK_NULL_HANDLE;
    VkDescriptorSet descriptor_set = VK_NULL_HANDLE;
};

struct NativeFragmentState {
    uint32_t alpha_test_enable = 0;
    uint32_t alpha_function = 0x0207;
    uint32_t alpha_reference = 0;
    uint32_t texture_mode = 0;
    uint32_t texture_alpha_kill = 0;
    uint32_t texture_opaque_alpha = 0;
    uint32_t texture_stage = 0;
    uint32_t combiner_control = 0;
    uint32_t shader_stage_program = 0;
    std::array<uint32_t, 8> combiner_color_inputs{};
    std::array<uint32_t, 8> combiner_color_outputs{};
    std::array<uint32_t, 8> combiner_alpha_inputs{};
    std::array<uint32_t, 8> combiner_alpha_outputs{};
    std::array<uint32_t, 8> combiner_factors0{};
    std::array<uint32_t, 8> combiner_factors1{};
    uint32_t final_combiner_inputs0 = 0;
    uint32_t final_combiner_inputs1 = 0;
    uint32_t final_combiner_factor0 = 0;
    uint32_t final_combiner_factor1 = 0;
    uint32_t fog_color = 0;
    uint32_t fog_enable = 0;
};

struct NativeFragmentPushConstants {
    uint32_t state_index = 0;
};

struct NativeVertexProgramState {
    uint32_t enabled = 0;
    uint32_t transform_program_start = 0;
    uint32_t target_width = 0;
    uint32_t target_height = 0;
    uint32_t texture_linear = 0;
    uint32_t texture_width = 0;
    uint32_t texture_height = 0;
    uint32_t specular_enable = 0;
    uint32_t fog_mode = 0;
    uint32_t fog_enable = 0;
    uint32_t fog_parameter0 = 0;
    uint32_t fog_parameter1 = 0;
    uint32_t vertex_stride_words = sizeof(NativeVertex) / sizeof(uint32_t);
    uint32_t program_input_word_offset =
        offsetof(NativeVertex, program_inputs) / sizeof(uint32_t);
    uint32_t texture_output = 9;
    uint32_t raw_attribute_fetch = 0;
    std::array<std::array<uint32_t, 4>, 136> transform_program{};
    std::array<std::array<uint32_t, 4>, 192> transform_constants{};
    std::array<std::array<uint32_t, 4>, 4> raw_attribute_formats{};
    std::array<std::array<uint32_t, 4>, 4> raw_attribute_base_offsets{};
    std::array<std::array<uint32_t, 4>, 16> current_vertex_attributes{};
    uint32_t raw_source_index_base = 0;
    uint32_t reserved1 = 0;
    uint32_t reserved2 = 0;
    uint32_t reserved3 = 0;
};

static_assert(offsetof(NativeVertexProgramState, transform_program) == 64u);
static_assert(offsetof(NativeVertexProgramState, raw_attribute_formats) == 5312u);
static_assert(sizeof(NativeVertexProgramState) == 5712u);

struct RenderTargetFeedbackSpec {
    uint32_t address = 0;
    uint32_t width = 0;
    uint32_t height = 0;
    std::string format = "A8R8G8B8_LINEAR";
    uint32_t producer_address = 0;
    bool offscreen_produced = false;

    bool operator==(const RenderTargetFeedbackSpec& other) const {
        return address == other.address
            && width == other.width
            && height == other.height
            && format == other.format
            && producer_address == other.producer_address
            && offscreen_produced == other.offscreen_produced;
    }
};

struct OffscreenRenderTarget {
    RenderTargetFeedbackSpec spec{};
    VkFramebuffer framebuffer = VK_NULL_HANDLE;
    VkImageView color_view = VK_NULL_HANDLE;
    VkImage depth_image = VK_NULL_HANDLE;
    VkDeviceMemory depth_memory = VK_NULL_HANDLE;
    VkImageView depth_view = VK_NULL_HANDLE;
};

bool host_texture_matches_draw(
    const HostTexture& texture,
    const NativeDraw& draw);

VkSamplerAddressMode nv2a_sampler_address_mode(uint32_t value);

std::string nv2a_sampler_address_name(uint32_t value);

struct HostPipeline {
    NativePipelineState state{};
    VkPipeline pipeline = VK_NULL_HANDLE;
};

NativePipelineState pipeline_state_for_draw(const NativeDraw& draw);

NativePipelineState frontend_text_pipeline_state();

NativeFragmentState fragment_state_for_draw(const NativeDraw& draw);

bool default_fixed_function_texture_combiner_recovery_required(
    const NativeDraw& draw);

NativeFragmentState frontend_text_fragment_state();

bool gpu_vertex_program_compatible(const NativeDraw& draw);

NativeVertexProgramState vertex_program_state_for_draw(
    const NativeDraw& draw,
    VkExtent2D target_extent,
    bool enable_gpu_program);

VkCompareOp nv2a_depth_compare_op(uint32_t function);

VkCullModeFlags nv2a_cull_mode(uint32_t enabled, uint32_t face);

VkFrontFace nv2a_front_face(uint32_t face);

VkBlendFactor nv2a_blend_factor(
    uint32_t factor,
    VkBlendFactor fallback);

VkBlendOp nv2a_blend_op(uint32_t equation);

VkColorComponentFlags nv2a_color_write_mask(uint32_t mask);

std::vector<uint32_t> referenced_vertex_program_constants(
    const NativeDraw& draw);

float nv2a_programmable_fog_factor(
    const NativeDraw& draw,
    float fog_distance);

uint32_t execute_presented_vertex_program(
    std::vector<NativeVertex>& vertices,
    const NativeDraw& draw,
    PresentedVertexTransformDiagnostics* diagnostics = nullptr);

bool fixed_function_composite_matrix_valid(const NativeDraw& draw);

uint32_t execute_presented_fixed_function_transform(
    std::vector<NativeVertex>& vertices,
    const NativeDraw& draw,
    PresentedVertexTransformDiagnostics* diagnostics = nullptr);

uint32_t normalize_presented_linear_texture_coordinates(
    std::vector<NativeVertex>& vertices,
    const NativeDraw& draw);

struct InterpretedD3DStream {
    VkClearValue diagnostic_clear_color{};
    bool clear_color_valid = false;
    uint32_t clear_color_argb = 0;
    bool surface_payload_color_valid = false;
    uint32_t surface_payload_argb = 0;
    uint32_t surface_payload_sample_count = 0;
    uint32_t surface_payload_dominant_count = 0;
    uint32_t surface_payload_scan_skipped_count = 0;
    uint32_t ordered_push_buffer_append_count = 0;
    uint32_t indexed_word_push_buffer_append_count = 0;
    uint32_t reconstructed_push_buffer_append_count = 0;
    uint32_t push_buffer_word_count = 0;
    uint32_t method_packet_count = 0;
    uint32_t interpreted_method_count = 0;
    uint32_t bulk_indexed_method_count = 0;
    uint32_t bulk_inline_method_count = 0;
    uint32_t last_interpreted_method_count = 0;
    uint32_t last_bulk_indexed_method_count = 0;
    uint32_t last_bulk_inline_method_count = 0;
    uint64_t last_push_buffer_collect_us = 0;
    uint64_t last_method_apply_us = 0;
    uint64_t last_method_finalize_us = 0;
    uint32_t zero_count_method_word_count = 0;
    uint32_t zero_count_indexed_array_noop_packet_count = 0;
    uint32_t control_flow_packet_count = 0;
    uint32_t mmio_setup_write_count = 0;
    uint32_t submission_kick_count = 0;
    uint32_t unknown_packet_count = 0;
    uint32_t truncated_packet_count = 0;
    bool pending_method_packet = false;
    bool pending_long_count_word = false;
    bool pending_method_non_increasing = false;
    uint32_t pending_first_method = 0;
    uint32_t pending_method_count = 0;
    uint32_t pending_method_index = 0;
    uint32_t pending_next_address = 0;
    uint32_t unsupported_draw_arrays_count = 0;
    uint32_t indexed_array_draw_count = 0;
    uint32_t indexed_array_element_count = 0;
    uint32_t materialized_indexed_draw_count = 0;
    uint32_t materialized_indexed_vertex_count = 0;
    uint32_t missing_indexed_resource_draw_count = 0;
    uint32_t converted_quad_count = 0;
    uint32_t discarded_quad_vertex_count = 0;
    uint32_t state_seed = 0;
    bool state_seed_updates_required = true;
    std::array<uint32_t, 16> vertex_offsets{};
    std::array<uint32_t, 16> vertex_formats{};
    Nv2aVertexAttributes current_vertex_attributes =
        default_nv2a_vertex_attributes();
    std::array<uint32_t, 4> texture_offsets{};
    std::array<uint32_t, 4> texture_addresses{};
    std::array<uint32_t, 4> texture_formats{};
    std::array<uint32_t, 4> texture_image_rects{};
    std::array<uint32_t, 4> texture_controls{};
    std::array<uint32_t, 4> texture_filters{};
    uint32_t blend_enable = 0;
    uint32_t blend_source_factor = 1;
    uint32_t blend_destination_factor = 0;
    uint32_t blend_equation = 0x8006;
    uint32_t alpha_test_enable = 0;
    uint32_t alpha_function = 0x0207;
    uint32_t alpha_reference = 0;
    uint32_t surface_format = 0;
    uint32_t surface_pitch = 0;
    uint32_t surface_color_offset = 0;
    uint32_t surface_clip_horizontal = 0;
    uint32_t surface_clip_vertical = 0;
    uint32_t color_mask = 0x01010101;
    uint32_t depth_test_enable = 0;
    uint32_t depth_function = 0x0203;
    uint32_t depth_write_enable = 1;
    uint32_t polygon_offset_fill_enable = 0;
    uint32_t polygon_offset_scale_factor = 0;
    uint32_t polygon_offset_bias = 0;
    uint32_t skin_mode = 0;
    uint32_t specular_enable = 0;
    uint32_t cull_face_enable = 0;
    uint32_t cull_face = 0x0405;
    uint32_t front_face = 0x0900;
    uint32_t shader_stage_program = 0;
    uint32_t combiner_control = 0;
    std::array<uint32_t, 8> combiner_color_inputs{};
    std::array<uint32_t, 8> combiner_color_outputs{};
    std::array<uint32_t, 8> combiner_alpha_inputs{};
    std::array<uint32_t, 8> combiner_alpha_outputs{};
    std::array<uint32_t, 8> combiner_factors0{};
    std::array<uint32_t, 8> combiner_factors1{};
    uint32_t final_combiner_inputs0 = 0;
    uint32_t final_combiner_inputs1 = 0;
    std::array<uint32_t, 2> final_combiner_factors{};
    uint32_t fog_mode = 0;
    uint32_t fog_generation_mode = 0;
    uint32_t fog_enable = 0;
    uint32_t fog_color = 0;
    std::array<uint32_t, 3> fog_params{};
    uint32_t transform_execution_mode = 0;
    uint32_t transform_program_load = 0;
    uint32_t transform_program_start = 0;
    uint32_t transform_constant_load = 0;
    uint32_t launch_transform_program_count = 0;
    uint32_t failed_launch_transform_program_count = 0;
    std::array<uint32_t, 4> transform_data{};
    std::array<std::array<uint32_t, 4>, 136> transform_program{};
    std::array<std::array<uint32_t, 4>, 192> transform_constants{};
    uint32_t active_primitive = 0;
    uint32_t frame_draw_begin = 0;
    uint32_t presented_draw_begin = 0;
    uint32_t presented_draw_count = 0;
    uint32_t flip_count = 0;
    std::vector<uint32_t> inline_words;
    std::vector<uint32_t> active_vertex_indices;
    std::vector<PushBufferWord> push_buffer_words_scratch;
    std::vector<NativeVertex> vertices;
    std::vector<NativeDraw> draws;
    std::vector<uint32_t> gpu_raw_vertex_indices;
    uint32_t gpu_raw_attribute_draw_count = 0;
    uint32_t gpu_raw_attribute_vertex_count = 0;
};

struct GpuRawVertexResourceLayout {
    uint32_t guest_address = 0;
    uint32_t packed_offset = 0;
    uint32_t byte_size = 0;
};

using GpuRawVertexDirtyRange = DirtyRange;

struct GpuRawVertexResourceCache {
    std::vector<GpuRawVertexResourceLayout> layout;
    std::vector<uint8_t> gpu_raw_vertex_bytes;
    std::vector<GpuRawVertexDirtyRange> dirty_ranges;
    uint64_t refresh_count = 0;
    uint64_t layout_rebuild_count = 0;
    bool layout_rebuilt = false;
    uint32_t resource_count = 0;
    uint32_t compared_resource_count = 0;
    uint32_t changed_resource_count = 0;
    uint32_t reused_resource_count = 0;
    uint64_t compared_bytes = 0;
    uint64_t dirty_bytes = 0;
    uint64_t refresh_us = 0;
};

std::pair<size_t, size_t> presented_vertex_span(
    const InterpretedD3DStream& stream);

bool recover_presented_half_surface_quad(
    std::vector<NativeVertex>& vertices,
    const NativeDraw& draw,
    uint32_t output_width,
    uint32_t output_height,
    bool& overscan_height_recovered);

std::vector<RecoveredD3DCommand> build_recovered_d3d_command_stream();

uint32_t parse_json_u32_text(const std::string& value, const char* label);

uint64_t parse_json_u64_text(const std::string& value, const char* label);

uint32_t parse_json_u32_wrapping_text(const std::string& value, const char* label);

uint8_t parse_hex_byte(const std::string& value, size_t offset);

std::vector<uint8_t> parse_json_bytes_hex(const std::string& value);

std::vector<uint8_t> payload_from_value(uint32_t value, uint32_t size);

RecoveredD3DCommand make_recovered_d3d_command(
    RecoveredD3DCommandKind kind,
    uint32_t address,
    uint32_t value,
    uint32_t size,
    const uint8_t* payload,
    size_t payload_size);

RecoveredD3DCommand recovered_d3d_command_from_packed_record(
    const uint8_t* record);

std::optional<std::string> json_object_field_text(
    const std::string& object,
    const std::string& field);

std::vector<RecoveredD3DCommand> load_recovered_d3d_command_stream(
    const std::filesystem::path& path);

std::vector<RecoveredD3DCommand> load_recovered_d3d_binary_stream(
    const std::filesystem::path& path);

std::vector<RecoveredTextureResource> load_recovered_texture_resources_stream(
    std::istream& file,
    std::vector<RecoveredTextureResource>* reusable_resources = nullptr,
    uint64_t* reused_resource_count = nullptr,
    uint64_t* reused_payload_bytes = nullptr);

std::vector<RecoveredTextureResource> load_recovered_texture_resources(
    const std::filesystem::path& path,
    bool required = false,
    std::vector<RecoveredTextureResource>* reusable_resources = nullptr,
    uint64_t* reused_resource_count = nullptr,
    uint64_t* reused_payload_bytes = nullptr);

std::vector<RecoveredTextureResource> load_recovered_texture_resources_bytes(
    const std::vector<uint8_t>& bytes,
    std::vector<RecoveredTextureResource>* reusable_resources = nullptr,
    uint64_t* reused_resource_count = nullptr,
    uint64_t* reused_payload_bytes = nullptr);

struct RecoveredD3DStreamSource {
    std::vector<RecoveredD3DCommand> commands;
    std::vector<RecoveredTextureResource> textures;
    std::string source;
    std::string frontend_text;
    uint32_t frontend_text_x_bits = 0x43A00000u;
    uint32_t frontend_text_y_bits = 0x43AA0000u;
    uint32_t frontend_text_size_bits = 0x41C80000u;
    uint32_t frontend_text_color_argb = 0xFFFFFFFFu;
    bool resources_unchanged = false;
    std::filesystem::path resource_source;
    std::filesystem::path interpreter_bootstrap_source;
};

RecoveredD3DStreamSource load_or_build_recovered_d3d_command_stream(
    const std::filesystem::path& render_stream_json,
    bool load_textures = true,
    const std::string* supplied_manifest_text = nullptr,
    bool load_commands = true,
    bool require_resource_source = false,
    std::vector<RecoveredTextureResource>* reusable_resources = nullptr,
    uint64_t* reused_resource_count = nullptr,
    uint64_t* reused_payload_bytes = nullptr);

const char* recovered_d3d_command_kind_name(RecoveredD3DCommandKind kind);

uint32_t mix_d3d_state_seed(uint32_t seed, uint32_t value);

inline void update_d3d_state_seed(
    InterpretedD3DStream& interpreted,
    uint32_t value) {
    if (interpreted.state_seed_updates_required) {
        interpreted.state_seed = mix_d3d_state_seed(
            interpreted.state_seed, value);
    }
}

inline VkClearValue color_from_interpreted_d3d_state(uint32_t seed) {
    const float red = 0.18f + static_cast<float>((seed >> 16) & 0xFFu) / 255.0f * 0.62f;
    const float green = 0.18f + static_cast<float>((seed >> 8) & 0xFFu) / 255.0f * 0.62f;
    const float blue = 0.18f + static_cast<float>(seed & 0xFFu) / 255.0f * 0.62f;
    VkClearValue color{};
    color.color = {{red, green, blue, 1.0f}};
    return color;
}

inline VkClearValue color_from_d3d_argb(uint32_t argb) {
    const float alpha = static_cast<float>((argb >> 24u) & 0xFFu) / 255.0f;
    const float red = static_cast<float>((argb >> 16u) & 0xFFu) / 255.0f;
    const float green = static_cast<float>((argb >> 8u) & 0xFFu) / 255.0f;
    const float blue = static_cast<float>(argb & 0xFFu) / 255.0f;
    VkClearValue color{};
    color.color = {{red, green, blue, alpha}};
    return color;
}

template <typename CommandAt>
void push_buffer_words_from_recovered_commands(
    size_t command_count,
    CommandAt command_at,
    InterpretedD3DStream& interpreted,
    std::vector<PushBufferWord>& words) {
    words.clear();
    if (words.capacity() < command_count) {
        words.reserve(command_count);
    }
    uint32_t aperture_begin_offset = kRecoveredPushBufferApertureSize;
    uint32_t aperture_end_offset = 0u;
    bool ordered_word_writes = true;
    bool indexed_word_writes = true;
    uint32_t indexed_word_alignment = std::numeric_limits<uint32_t>::max();
    bool ordered_run_active = false;
    uint32_t ordered_previous_address = 0u;
    uint32_t ordered_pending_max_address = 0u;
    size_t ordered_word_count = 0u;
    for (size_t command_index = 0u;
         command_index < command_count;
         ++command_index) {
        const auto command = command_at(command_index);
        update_d3d_state_seed(interpreted, command.address);
        update_d3d_state_seed(interpreted, command.value);
        if (command.kind == RecoveredD3DCommandKind::MmioWrite) {
            const uint32_t offset = command.address - 0xFED00000u;
            if (offset == 0x0008u) {
                ++interpreted.submission_kick_count;
            } else if (offset == 0x0040u || offset == 0x0048u
                       || offset == 0x004Cu || offset == 0x0050u) {
                ++interpreted.mmio_setup_write_count;
            }
        }
        if (command.kind != RecoveredD3DCommandKind::PushBufferWrite
            || command.address < kRecoveredPushBufferBase
            || command.address >= kRecoveredPushBufferEnd) {
            ordered_run_active = false;
            ordered_pending_max_address = 0u;
            continue;
        }
        const size_t payload_size = command.payload_size != 0u
            ? command.payload_size
            : std::min<size_t>(command.size, command.payload.size());
        const uint32_t command_offset = command.address - kRecoveredPushBufferBase;
        const uint32_t command_end_offset = static_cast<uint32_t>(
            std::min<uint64_t>(
                kRecoveredPushBufferApertureSize,
                static_cast<uint64_t>(command_offset) + payload_size));
        aperture_begin_offset = std::min(aperture_begin_offset, command_offset);
        aperture_end_offset = std::max(aperture_end_offset, command_end_offset);
        if (payload_size == 0u || payload_size % sizeof(uint32_t) != 0u
            || (command.address & 3u) != 0u) {
            ordered_word_writes = false;
            indexed_word_writes = false;
        } else if (payload_size != sizeof(uint32_t)) {
            indexed_word_writes = false;
        }
        ordered_word_count += payload_size / sizeof(uint32_t);
        const uint32_t command_alignment = command.address & 3u;
        if (indexed_word_alignment == std::numeric_limits<uint32_t>::max()) {
            indexed_word_alignment = command_alignment;
        } else if (command_alignment != indexed_word_alignment) {
            indexed_word_writes = false;
        }
        if (ordered_run_active
            && command.address + 0x1000u < ordered_pending_max_address) {
            ordered_run_active = false;
            ordered_pending_max_address = 0u;
        }
        if (ordered_run_active
            && command.address < ordered_previous_address + sizeof(uint32_t)) {
            ordered_word_writes = false;
        }
        ordered_run_active = true;
        ordered_previous_address = command.address
            + static_cast<uint32_t>(
                payload_size >= sizeof(uint32_t)
                    ? payload_size - sizeof(uint32_t)
                    : 0u);
        ordered_pending_max_address = std::max(
            ordered_pending_max_address,
            command.address + static_cast<uint32_t>(payload_size));
    }
    if (aperture_begin_offset >= aperture_end_offset) {
        return;
    }
    if (ordered_word_writes) {
        if (words.capacity() < ordered_word_count) {
            words.reserve(ordered_word_count);
        }
        bool run_active = false;
        uint32_t previous_address = 0u;
        uint32_t pending_max_address = 0u;
        uint32_t run_id = 0u;
        for (size_t command_index = 0u;
             command_index < command_count;
             ++command_index) {
            const auto command = command_at(command_index);
            if (command.kind != RecoveredD3DCommandKind::PushBufferWrite
                || command.address < kRecoveredPushBufferBase
                || command.address >= kRecoveredPushBufferEnd) {
                run_active = false;
                pending_max_address = 0u;
                continue;
            }
            if (run_active
                && command.address + 0x1000u < pending_max_address) {
                run_active = false;
                pending_max_address = 0u;
            }
            const size_t payload_size = command.payload_size != 0u
                ? command.payload_size
                : std::min<size_t>(command.size, command.payload.size());
            const uint8_t* payload = command.payload.data();
            for (size_t payload_offset = 0u;
                 payload_offset + sizeof(uint32_t) <= payload_size;
                 payload_offset += sizeof(uint32_t)) {
                const uint32_t address = command.address
                    + static_cast<uint32_t>(payload_offset);
                const bool contiguous = run_active
                    && address == previous_address + sizeof(uint32_t);
                if (!contiguous) {
                    ++run_id;
                }
                uint32_t value = 0u;
                std::memcpy(&value, payload + payload_offset, sizeof(value));
                words.push_back({address, value, run_id});
                run_active = true;
                previous_address = address;
            }
            pending_max_address = std::max(
                pending_max_address,
                command.address + static_cast<uint32_t>(payload_size));
        }
        ++interpreted.ordered_push_buffer_append_count;
        return;
    }
    const uint32_t aperture_span = aperture_end_offset - aperture_begin_offset;
    if (indexed_word_writes) {
        const uint32_t word_span = (aperture_span + 3u) / 4u;
        std::vector<uint32_t> pending_words(word_span);
        std::vector<uint8_t> pending_valid(word_span);
        uint32_t pending_min_word = word_span;
        uint32_t pending_max_word = 0u;
        uint32_t pending_max_address = 0u;
        uint32_t run_id = 0u;

        auto flush_pending_words = [&]() {
            uint32_t previous_address = 0u;
            bool emitted_word = false;
            for (uint32_t word_offset = pending_min_word;
                 word_offset < pending_max_word;
                 ++word_offset) {
                if (pending_valid[word_offset] == 0u) {
                    continue;
                }
                const uint32_t address = kRecoveredPushBufferBase
                    + aperture_begin_offset + word_offset * 4u;
                const bool contiguous = emitted_word
                    && address == previous_address + 4u;
                if (!contiguous) {
                    ++run_id;
                }
                words.push_back({
                    address,
                    pending_words[word_offset],
                    run_id,
                });
                previous_address = address;
                emitted_word = true;
            }
            if (pending_min_word < pending_max_word) {
                std::fill(
                    pending_valid.begin() + pending_min_word,
                    pending_valid.begin() + pending_max_word,
                    0u);
            }
            pending_min_word = word_span;
            pending_max_word = 0u;
            pending_max_address = 0u;
        };

        for (size_t command_index = 0u;
             command_index < command_count;
             ++command_index) {
            const auto command = command_at(command_index);
            if (command.kind != RecoveredD3DCommandKind::PushBufferWrite
                || command.address < kRecoveredPushBufferBase
                || command.address >= kRecoveredPushBufferEnd) {
                flush_pending_words();
                continue;
            }
            if (pending_min_word != word_span
                && command.address + 0x1000u < pending_max_address) {
                flush_pending_words();
            }
            const uint32_t word_offset = (
                command.address - kRecoveredPushBufferBase
                - aperture_begin_offset) / 4u;
            pending_words[word_offset] = command.value;
            pending_valid[word_offset] = 1u;
            pending_min_word = std::min(pending_min_word, word_offset);
            pending_max_word = std::max(pending_max_word, word_offset + 1u);
            pending_max_address = std::max(
                pending_max_address,
                command.address + static_cast<uint32_t>(sizeof(uint32_t)));
        }
        flush_pending_words();
        ++interpreted.indexed_word_push_buffer_append_count;
        return;
    }
    ++interpreted.reconstructed_push_buffer_append_count;
    std::vector<uint8_t> pending_bytes(aperture_span);
    std::vector<uint8_t> pending_valid(aperture_span);
    uint32_t pending_min_offset = aperture_span;
    uint32_t pending_max_offset = 0u;
    uint32_t pending_max_address = 0;
    uint32_t run_id = 0u;

    auto flush_pending = [&]() {
        uint32_t previous_address = 0;
        bool emitted_word = false;
        uint32_t offset = pending_min_offset;
        while (offset + 3u < pending_max_offset) {
            if (pending_valid[offset] == 0u || pending_valid[offset + 1u] == 0u
                || pending_valid[offset + 2u] == 0u || pending_valid[offset + 3u] == 0u) {
                ++offset;
                continue;
            }
            const uint32_t address =
                kRecoveredPushBufferBase + aperture_begin_offset + offset;
            const bool contiguous = emitted_word && address == previous_address + 4u;
            const uint32_t value = static_cast<uint32_t>(pending_bytes[offset]) |
                (static_cast<uint32_t>(pending_bytes[offset + 1u]) << 8u) |
                (static_cast<uint32_t>(pending_bytes[offset + 2u]) << 16u) |
                (static_cast<uint32_t>(pending_bytes[offset + 3u]) << 24u);
            if (!contiguous) {
                ++run_id;
            }
            words.push_back({address, value, run_id});
            previous_address = address;
            emitted_word = true;
            offset += 4u;
        }
        if (pending_min_offset < pending_max_offset) {
            std::fill(
                pending_valid.begin() + pending_min_offset,
                pending_valid.begin() + pending_max_offset,
                0u);
        }
        pending_min_offset = aperture_span;
        pending_max_offset = 0u;
        pending_max_address = 0;
    };

    for (size_t command_index = 0u;
         command_index < command_count;
         ++command_index) {
        const auto command = command_at(command_index);
        if (command.kind != RecoveredD3DCommandKind::PushBufferWrite
            || command.address < kRecoveredPushBufferBase
            || command.address >= kRecoveredPushBufferEnd) {
            flush_pending();
            continue;
        }
        std::array<uint8_t, 8> fallback_payload{};
        const uint8_t* payload_data = command.payload.data();
        size_t payload_size = command.payload_size;
        if (command.payload_size == 0u) {
            payload_size = std::min<size_t>(command.size, fallback_payload.size());
            std::memcpy(fallback_payload.data(), &command.value, payload_size);
            payload_data = fallback_payload.data();
        }
        if (pending_min_offset != aperture_span
            && command.address + 0x1000u < pending_max_address) {
            flush_pending();
        }
        for (size_t offset = 0; offset < payload_size; ++offset) {
            const uint32_t aperture_offset =
                command.address + static_cast<uint32_t>(offset)
                - kRecoveredPushBufferBase;
            if (aperture_offset < aperture_begin_offset
                || aperture_offset >= aperture_end_offset) {
                continue;
            }
            const uint32_t pending_offset = aperture_offset - aperture_begin_offset;
            pending_bytes[pending_offset] = payload_data[offset];
            pending_valid[pending_offset] = 1u;
            pending_min_offset = std::min(pending_min_offset, pending_offset);
            pending_max_offset = std::max(pending_max_offset, pending_offset + 1u);
        }
        pending_max_address = std::max(
            pending_max_address,
            command.address + static_cast<uint32_t>(payload_size));
    }
    flush_pending();
}

struct SurfacePayloadCandidate {
    bool valid = false;
    uint32_t argb = 0;
    uint32_t sample_count = 0;
    uint32_t dominant_count = 0;
};

SurfacePayloadCandidate dominant_surface_payload_from_words(
    const std::vector<PushBufferWord>& words);

float float_from_u32(uint32_t value);

uint32_t u32_from_float(float value);

float float_from_i16_bits(uint32_t value);

NativeDraw native_draw_from_interpreted_state(
    const InterpretedD3DStream& interpreted);

void finish_inline_draw(InterpretedD3DStream& interpreted);

void finish_indexed_draw(InterpretedD3DStream& interpreted);

void interpret_nv2a_method(
    uint32_t method,
    uint32_t data,
    InterpretedD3DStream& interpreted);

using Nv2aBootstrapMethod = std::pair<uint32_t, uint32_t>;

std::vector<Nv2aBootstrapMethod> native_draw_interpreter_bootstrap_methods(
    const NativeDraw& draw,
    bool clear_color_valid,
    uint32_t clear_color_argb);

void load_interpreter_bootstrap_state(
    const std::filesystem::path& path,
    InterpretedD3DStream& interpreted);

void interpret_push_buffer_method_packet(
    const std::vector<PushBufferWord>& words,
    size_t& index,
    InterpretedD3DStream& interpreted,
    bool non_increasing);

void interpret_long_non_increasing_packet(
    const std::vector<PushBufferWord>& words,
    size_t& index,
    InterpretedD3DStream& interpreted);

void interpret_pending_push_buffer_method_packet(
    const std::vector<PushBufferWord>& words,
    size_t& index,
    InterpretedD3DStream& interpreted);

template <typename CommandAt>
void interpret_recovered_d3d_command_append(
    size_t command_count,
    CommandAt command_at,
    InterpretedD3DStream& interpreted) {
    const auto collect_begin = std::chrono::steady_clock::now();
    interpreted.last_interpreted_method_count = 0u;
    interpreted.last_bulk_indexed_method_count = 0u;
    interpreted.last_bulk_inline_method_count = 0u;
    interpreted.last_push_buffer_collect_us = 0u;
    interpreted.last_method_apply_us = 0u;
    interpreted.last_method_finalize_us = 0u;
    interpreted.state_seed_updates_required =
        !interpreted.clear_color_valid
        && !interpreted.surface_payload_color_valid;
    if (interpreted.state_seed == 0u) {
        interpreted.state_seed = 0xB200D3D8u;
    }
    const uint32_t starting_flip_count = interpreted.flip_count;
    const uint32_t starting_method_count =
        interpreted.interpreted_method_count;
    const bool surface_payload_scan_required = !interpreted.clear_color_valid;
    std::vector<PushBufferWord>& words =
        interpreted.push_buffer_words_scratch;
    push_buffer_words_from_recovered_commands(
        command_count, command_at, interpreted, words);
    const auto after_collect = std::chrono::steady_clock::now();
    interpreted.last_push_buffer_collect_us = std::chrono::duration_cast<
        std::chrono::microseconds>(after_collect - collect_begin).count();
    interpreted.push_buffer_word_count += static_cast<uint32_t>(words.size());
    size_t index = 0;
    interpret_pending_push_buffer_method_packet(words, index, interpreted);
    while (index < words.size()) {
        const uint32_t word = words[index].value;
        if ((word & 0xE0030003u) == 0u) {
            interpret_push_buffer_method_packet(words, index, interpreted, false);
        } else if ((word & 0xE0030003u) == 0x40000000u) {
            interpret_push_buffer_method_packet(words, index, interpreted, true);
        } else if ((word & 0xFFFF0003u) == 0x00030000u) {
            interpret_long_non_increasing_packet(words, index, interpreted);
        } else if ((word & 0xE0000003u) == 0x20000000u || (word & 0x3u) == 0x1u ||
                   (word & 0x3u) == 0x2u || word == 0x00020000u) {
            ++interpreted.control_flow_packet_count;
            update_d3d_state_seed(interpreted, word);
            ++index;
        } else {
            ++interpreted.unknown_packet_count;
            update_d3d_state_seed(interpreted, word);
            ++index;
        }
    }
    const auto after_method_apply = std::chrono::steady_clock::now();
    interpreted.last_method_apply_us = std::chrono::duration_cast<
        std::chrono::microseconds>(
            after_method_apply - after_collect).count();
    if (surface_payload_scan_required) {
        const SurfacePayloadCandidate surface_payload =
            dominant_surface_payload_from_words(words);
        if (surface_payload.valid) {
            interpreted.surface_payload_color_valid = true;
            interpreted.surface_payload_argb = surface_payload.argb;
            interpreted.surface_payload_sample_count = surface_payload.sample_count;
            interpreted.surface_payload_dominant_count =
                surface_payload.dominant_count;
        }
    } else {
        ++interpreted.surface_payload_scan_skipped_count;
    }
    if (interpreted.flip_count != starting_flip_count) {
        std::vector<NativeVertex> retained_vertices;
        std::vector<NativeDraw> retained_draws;
        retained_vertices.reserve(interpreted.vertices.size());
        retained_draws.reserve(interpreted.draws.size());
        auto retain_draw_range = [&](size_t first, size_t count) {
            const size_t draw_end = std::min(first + count, interpreted.draws.size());
            for (size_t draw_index = std::min(first, interpreted.draws.size());
                 draw_index < draw_end;
                 ++draw_index) {
                NativeDraw draw = std::move(interpreted.draws[draw_index]);
                if (draw.gpu_raw_attribute_fetch) {
                    draw.first_vertex = 0u;
                    retained_draws.push_back(std::move(draw));
                    continue;
                }
                const size_t vertex_begin = std::min<size_t>(
                    draw.first_vertex, interpreted.vertices.size());
                const size_t vertex_end = std::min<size_t>(
                    vertex_begin + draw.vertex_count, interpreted.vertices.size());
                draw.first_vertex = static_cast<uint32_t>(retained_vertices.size());
                draw.vertex_count = static_cast<uint32_t>(vertex_end - vertex_begin);
                retained_vertices.insert(
                    retained_vertices.end(),
                    interpreted.vertices.begin() + vertex_begin,
                    interpreted.vertices.begin() + vertex_end);
                retained_draws.push_back(std::move(draw));
            }
        };
        retain_draw_range(
            interpreted.presented_draw_begin,
            interpreted.presented_draw_count);
        const uint32_t retained_presented_count =
            static_cast<uint32_t>(retained_draws.size());
        retain_draw_range(
            interpreted.frame_draw_begin,
            interpreted.draws.size() - std::min<size_t>(
                interpreted.frame_draw_begin, interpreted.draws.size()));
        interpreted.vertices = std::move(retained_vertices);
        interpreted.draws = std::move(retained_draws);
        interpreted.presented_draw_begin = 0u;
        interpreted.presented_draw_count = retained_presented_count;
        interpreted.frame_draw_begin = retained_presented_count;
    }
    if (interpreted.clear_color_valid) {
        interpreted.diagnostic_clear_color = color_from_d3d_argb(interpreted.clear_color_argb);
    } else if (interpreted.surface_payload_color_valid) {
        interpreted.diagnostic_clear_color = color_from_d3d_argb(interpreted.surface_payload_argb);
    } else {
        interpreted.diagnostic_clear_color = color_from_interpreted_d3d_state(interpreted.state_seed);
    }
    interpreted.last_interpreted_method_count =
        interpreted.interpreted_method_count - starting_method_count;
    interpreted.last_method_finalize_us = std::chrono::duration_cast<
        std::chrono::microseconds>(
            std::chrono::steady_clock::now() - after_method_apply).count();
}

inline void interpret_recovered_d3d_append(
    const std::vector<RecoveredD3DCommand>& stream,
    size_t begin,
    size_t end,
    InterpretedD3DStream& interpreted) {
    const size_t bounded_begin = std::min(begin, stream.size());
    const size_t bounded_end = std::min(std::max(end, bounded_begin), stream.size());
    interpret_recovered_d3d_command_append(
        bounded_end - bounded_begin,
        [&](size_t command_index) -> const RecoveredD3DCommand& {
            return stream[bounded_begin + command_index];
        },
        interpreted);
}

inline void interpret_recovered_d3d_packed_append(
    const std::vector<uint8_t>& packed_commands,
    InterpretedD3DStream& interpreted) {
    if (packed_commands.size() % kRecoveredD3DCommandRecordSize != 0u) {
        throw std::runtime_error("packed live command delta is truncated");
    }
    interpret_recovered_d3d_command_append(
        packed_commands.size() / kRecoveredD3DCommandRecordSize,
        [&](size_t command_index) {
            return recovered_d3d_command_from_packed_record(
                packed_commands.data()
                + command_index * kRecoveredD3DCommandRecordSize);
        },
        interpreted);
}

inline std::vector<RecoveredD3DCommandSpan> recovered_d3d_command_spans_from_packed(
    const std::vector<uint8_t>& packed_spans,
    uint64_t* logical_write_count = nullptr) {
    std::vector<RecoveredD3DCommandSpan> spans;
    uint64_t writes = 0u;
    size_t cursor = 0u;
    while (cursor < packed_spans.size()) {
        if (packed_spans.size() - cursor
            < kRecoveredD3DCommandSpanHeaderSize) {
            throw std::runtime_error("packed live command span header is truncated");
        }
        const uint8_t* header = packed_spans.data() + cursor;
        const uint8_t kind = header[0];
        const uint8_t flags = header[1];
        uint32_t address = 0u;
        uint32_t payload_size = 0u;
        uint32_t span_write_count = 0u;
        std::memcpy(&address, header + 4u, sizeof(address));
        std::memcpy(&payload_size, header + 8u, sizeof(payload_size));
        std::memcpy(&span_write_count, header + 12u, sizeof(span_write_count));
        cursor += kRecoveredD3DCommandSpanHeaderSize;
        if (kind > 1u || (flags & ~1u) != 0u || payload_size == 0u
            || span_write_count == 0u
            || payload_size > packed_spans.size() - cursor) {
            throw std::runtime_error("packed live command span is invalid");
        }
        uint32_t value = 0u;
        std::memcpy(
            &value,
            packed_spans.data() + cursor,
            std::min<size_t>(payload_size, sizeof(value)));
        spans.push_back({
            kind == 0u ? RecoveredD3DCommandKind::MmioWrite
                       : RecoveredD3DCommandKind::PushBufferWrite,
            address,
            value,
            payload_size,
            {packed_spans.data() + cursor, payload_size},
            payload_size,
            span_write_count,
            flags,
        });
        writes += span_write_count;
        cursor += payload_size;
    }
    if (logical_write_count != nullptr) {
        *logical_write_count = writes;
    }
    return spans;
}

inline uint64_t interpret_recovered_d3d_span_append(
    const std::vector<uint8_t>& packed_spans,
    InterpretedD3DStream& interpreted) {
    uint64_t logical_write_count = 0u;
    const auto spans = recovered_d3d_command_spans_from_packed(
        packed_spans,
        &logical_write_count);
    interpret_recovered_d3d_command_append(
        spans.size(),
        [&](size_t span_index) -> const RecoveredD3DCommandSpan& {
            return spans[span_index];
        },
        interpreted);
    return logical_write_count;
}

inline InterpretedD3DStream interpret_recovered_d3d_stream(
    const std::vector<RecoveredD3DCommand>& stream,
    const std::filesystem::path& interpreter_bootstrap = {}) {
    InterpretedD3DStream interpreted{};
    interpreted.state_seed = 0xB200D3D8u;
    if (!interpreter_bootstrap.empty()) {
        load_interpreter_bootstrap_state(
            interpreter_bootstrap,
            interpreted);
    }
    interpret_recovered_d3d_append(stream, 0u, stream.size(), interpreted);
    finish_inline_draw(interpreted);
    finish_indexed_draw(interpreted);
    if (interpreted.flip_count == 0u) {
        interpreted.presented_draw_begin = 0u;
        interpreted.presented_draw_count = static_cast<uint32_t>(interpreted.draws.size());
    }
    return interpreted;
}

inline uint32_t nv2a_vertex_element_size(uint32_t format_raw) {
    const uint32_t type = format_raw & 0xFu;
    const uint32_t components = (format_raw >> 4u) & 0xFu;
    if (components == 0u) {
        return 0u;
    }
    if (type == 2u) {
        return components * 4u;
    }
    if (type == 0u || type == 4u || type == 6u) {
        return 4u;
    }
    return components * 2u;
}

using RecoveredVertexResourceIndex = std::unordered_map<
    uint32_t,
    std::vector<const RecoveredTextureResource*>>;

RecoveredVertexResourceIndex build_recovered_vertex_resource_index(
    const std::vector<RecoveredTextureResource>& resources);

const RecoveredTextureResource* recovered_vertex_resource(
    const RecoveredVertexResourceIndex& resource_index,
    uint32_t address,
    uint32_t size);

const uint8_t* recovered_vertex_bytes(
    const RecoveredVertexResourceIndex& resource_index,
    uint32_t address,
    uint32_t size);

int32_t sign_extend_vertex_component(uint32_t value, uint32_t bits);

bool decode_indexed_vertex_attribute_payload(
    const uint8_t* payload,
    uint32_t format_raw,
    std::array<float, 4>& output);

bool decode_indexed_vertex_attribute(
    const RecoveredVertexResourceIndex& resource_index,
    uint32_t address,
    uint32_t format_raw,
    std::array<float, 4>& output);

bool prepare_gpu_raw_attribute_fetch(
    NativeDraw& draw,
    const RecoveredVertexResourceIndex& resource_index,
    const std::unordered_map<const RecoveredTextureResource*, uint32_t>&
        packed_resource_offsets,
    uint32_t raw_index_word_base,
    std::vector<uint32_t>& raw_indices);

bool refresh_gpu_raw_vertex_resource_cache(
    const std::vector<RecoveredTextureResource>& resources,
    bool resources_unchanged,
    GpuRawVertexResourceCache& cache,
    std::unordered_map<const RecoveredTextureResource*, uint32_t>&
        packed_resource_offsets);

void materialize_indexed_draws(
    InterpretedD3DStream& interpreted,
    const std::vector<RecoveredTextureResource>& resources,
    GpuRawVertexResourceCache& raw_resource_cache,
    bool enable_gpu_raw_attribute_fetch = false,
    bool resources_unchanged = false);

struct GpuRawAttributeValidation {
    uint32_t eligible_draw_count = 0;
    uint64_t eligible_vertex_count = 0;
    uint64_t decoded_attribute_count = 0;
    uint64_t compared_vertex_count = 0;
    uint64_t mismatch_vertex_count = 0;
    uint32_t mapping_fallback_draw_count = 0;
    uint64_t packed_resource_bytes = 0;
};

GpuRawAttributeValidation validate_gpu_raw_attribute_fetch(
    const InterpretedD3DStream& cpu_stream,
    const std::vector<RecoveredTextureResource>& resources);

struct GpuRawDirtyRangeValidation {
    bool unchanged_generation_passed = false;
    bool changed_generation_passed = false;
    bool layout_change_passed = false;
    uint32_t changed_range_count = 0;
    uint64_t changed_upload_bytes = 0;
    uint64_t packed_resource_bytes = 0;

    bool passed() const {
        return unchanged_generation_passed
            && changed_generation_passed
            && layout_change_passed;
    }
};

GpuRawDirtyRangeValidation validate_gpu_raw_dirty_range_uploads();

}  // namespace b2r::host::vulkan_detail
