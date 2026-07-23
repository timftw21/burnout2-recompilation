#define NOMINMAX
#define WIN32_LEAN_AND_MEAN
#define VK_USE_PLATFORM_WIN32_KHR
#define SDL_MAIN_HANDLED

#include <windows.h>
#include <shellapi.h>
#include <SDL3/SDL.h>
#include <SDL3/SDL_main.h>
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

#include "nv2a_vertex_program.h"

namespace {

constexpr uint32_t kDefaultWidth = 640;
constexpr uint32_t kDefaultHeight = 480;
constexpr uint32_t kDefaultFrames = 120;
constexpr int64_t kTargetFrameUs = 16667;
constexpr auto kTargetFrameInterval = std::chrono::nanoseconds(16666667);
constexpr auto kFpsCounterSampleInterval = std::chrono::seconds(1);
constexpr DWORD kHighResolutionWaitableTimerFlag = 0x00000002u;
constexpr int64_t kControllerMinimumPulseMs = 150;
constexpr int64_t kControllerMaximumPulseMs = 2000;
constexpr uint64_t kControllerMinimumPulseGuestFlips = 2u;
constexpr auto kControllerDiscoveryInterval = std::chrono::milliseconds(500);
constexpr int32_t kControllerStickDeadzone = 4096;
constexpr int32_t kControllerStickPublishHysteresis = 256;
constexpr int32_t kControllerTriggerPublishHysteresis = 8;
constexpr uint16_t kControllerDpadUp = 0x0001u;
constexpr uint16_t kControllerDpadDown = 0x0002u;
constexpr uint16_t kControllerDpadLeft = 0x0004u;
constexpr uint16_t kControllerDpadRight = 0x0008u;
constexpr uint16_t kControllerStart = 0x0010u;
constexpr uint16_t kControllerBack = 0x0020u;
constexpr uint16_t kControllerLeftThumb = 0x0040u;
constexpr uint16_t kControllerRightThumb = 0x0080u;
constexpr uint16_t kControllerBlack = 0x0100u;
constexpr uint16_t kControllerWhite = 0x0200u;
constexpr uint16_t kControllerA = 0x1000u;
constexpr uint16_t kControllerB = 0x2000u;
constexpr uint16_t kControllerX = 0x4000u;
constexpr uint16_t kControllerY = 0x8000u;
constexpr uint32_t kRecoveredPushBufferBase = 0x80000000u;
constexpr uint32_t kRecoveredPushBufferApertureSize = 0x01000000u;
constexpr uint32_t kRecoveredPushBufferEnd =
    kRecoveredPushBufferBase + kRecoveredPushBufferApertureSize;

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

struct HostControllerState {
    bool connected = false;
    uint16_t buttons = 0u;
    uint8_t left_trigger = 0u;
    uint8_t right_trigger = 0u;
    int16_t thumb_lx = 0;
    int16_t thumb_ly = 0;
    int16_t thumb_rx = 0;
    int16_t thumb_ry = 0;
};

bool controller_axis_equivalent(int32_t left, int32_t right, int32_t limit) {
    if (left == right) {
        return true;
    }
    if (left == 0 || right == 0
        || std::abs(left) == limit || std::abs(right) == limit) {
        return false;
    }
    return std::abs(left - right) <= kControllerStickPublishHysteresis;
}

bool controller_trigger_equivalent(uint8_t left, uint8_t right) {
    if (left == right) {
        return true;
    }
    if (left == 0u || right == 0u || left == 255u || right == 255u) {
        return false;
    }
    return std::abs(static_cast<int32_t>(left) - right)
        <= kControllerTriggerPublishHysteresis;
}

bool controller_states_equivalent(
    const HostControllerState& left,
    const HostControllerState& right) {
    return left.connected == right.connected
        && left.buttons == right.buttons
        && controller_trigger_equivalent(
            left.left_trigger, right.left_trigger)
        && controller_trigger_equivalent(
            left.right_trigger, right.right_trigger)
        && controller_axis_equivalent(left.thumb_lx, right.thumb_lx, 32767)
        && controller_axis_equivalent(left.thumb_ly, right.thumb_ly, 32767)
        && controller_axis_equivalent(left.thumb_rx, right.thumb_rx, 32767)
        && controller_axis_equivalent(left.thumb_ry, right.thumb_ry, 32767);
}

int16_t normalize_sdl_stick(int16_t raw_value, bool invert = false) {
    int32_t value = raw_value;
    if (invert) {
        value = value == -32768 ? 32767 : -value;
    }
    const int32_t magnitude = std::abs(value);
    if (magnitude <= kControllerStickDeadzone) {
        return 0;
    }
    const int32_t adjusted = std::min<int32_t>(
        32767,
        (magnitude - kControllerStickDeadzone) * 32767
            / (32767 - kControllerStickDeadzone));
    const int32_t signed_adjusted = value < 0 ? -adjusted : adjusted;
    if (adjusted == 32767) {
        return static_cast<int16_t>(signed_adjusted);
    }
    return static_cast<int16_t>((signed_adjusted / 128) * 128);
}

uint8_t normalize_sdl_trigger(int16_t raw_value) {
    const int32_t clamped = std::clamp<int32_t>(raw_value, 0, 32767);
    const int32_t value = (clamped * 255 + 16383) / 32767;
    if (value <= 4) {
        return 0u;
    }
    return static_cast<uint8_t>(value >= 252 ? 255 : (value / 4) * 4);
}

HostControllerState map_sdl_gamepad_state(SDL_Gamepad* gamepad) {
    HostControllerState state{};
    if (gamepad == nullptr || !SDL_GamepadConnected(gamepad)) {
        return state;
    }
    state.connected = true;
    const auto button_down = [gamepad](SDL_GamepadButton button) {
        return SDL_GetGamepadButton(gamepad, button);
    };
    if (button_down(SDL_GAMEPAD_BUTTON_SOUTH)) state.buttons |= kControllerA;
    if (button_down(SDL_GAMEPAD_BUTTON_EAST)) state.buttons |= kControllerB;
    if (button_down(SDL_GAMEPAD_BUTTON_WEST)) state.buttons |= kControllerX;
    if (button_down(SDL_GAMEPAD_BUTTON_NORTH)) state.buttons |= kControllerY;
    if (button_down(SDL_GAMEPAD_BUTTON_LEFT_SHOULDER)) state.buttons |= kControllerWhite;
    if (button_down(SDL_GAMEPAD_BUTTON_RIGHT_SHOULDER)) state.buttons |= kControllerBlack;
    if (button_down(SDL_GAMEPAD_BUTTON_BACK)) state.buttons |= kControllerBack;
    if (button_down(SDL_GAMEPAD_BUTTON_START)) state.buttons |= kControllerStart;
    if (button_down(SDL_GAMEPAD_BUTTON_LEFT_STICK)) state.buttons |= kControllerLeftThumb;
    if (button_down(SDL_GAMEPAD_BUTTON_RIGHT_STICK)) state.buttons |= kControllerRightThumb;
    if (button_down(SDL_GAMEPAD_BUTTON_DPAD_UP)) state.buttons |= kControllerDpadUp;
    if (button_down(SDL_GAMEPAD_BUTTON_DPAD_DOWN)) state.buttons |= kControllerDpadDown;
    if (button_down(SDL_GAMEPAD_BUTTON_DPAD_LEFT)) state.buttons |= kControllerDpadLeft;
    if (button_down(SDL_GAMEPAD_BUTTON_DPAD_RIGHT)) state.buttons |= kControllerDpadRight;

    state.thumb_lx = normalize_sdl_stick(
        SDL_GetGamepadAxis(gamepad, SDL_GAMEPAD_AXIS_LEFTX));
    state.thumb_ly = normalize_sdl_stick(
        SDL_GetGamepadAxis(gamepad, SDL_GAMEPAD_AXIS_LEFTY), true);
    state.thumb_rx = normalize_sdl_stick(
        SDL_GetGamepadAxis(gamepad, SDL_GAMEPAD_AXIS_RIGHTX));
    state.thumb_ry = normalize_sdl_stick(
        SDL_GetGamepadAxis(gamepad, SDL_GAMEPAD_AXIS_RIGHTY), true);
    state.left_trigger = normalize_sdl_trigger(
        SDL_GetGamepadAxis(gamepad, SDL_GAMEPAD_AXIS_LEFT_TRIGGER));
    state.right_trigger = normalize_sdl_trigger(
        SDL_GetGamepadAxis(gamepad, SDL_GAMEPAD_AXIS_RIGHT_TRIGGER));
    return state;
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

std::string json_u32_vector(const std::vector<uint32_t>& values) {
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

void vk_check(VkResult result, const char* operation) {
    if (result != VK_SUCCESS) {
        std::ostringstream message;
        message << operation << " failed with VkResult " << result;
        throw std::runtime_error(message.str());
    }
}

struct Options {
    uint32_t width = kDefaultWidth;
    uint32_t height = kDefaultHeight;
    uint32_t max_frames = kDefaultFrames;
    uint32_t presented_draw_begin = 0;
    uint32_t presented_draw_end = std::numeric_limits<uint32_t>::max();
    uint32_t presentation_pipeline_depth = 1;
    uint32_t flip_audit_max_flips = 0;
    uint32_t flip_audit_health_interval = 30;
    bool inject_input = false;
    bool list_adapters_only = false;
    std::wstring title = L"B2 Recomp - First Interactive Frame";
    std::filesystem::path debug_json;
    std::filesystem::path render_stream_json;
    std::filesystem::path screenshot;
    std::filesystem::path hotkey_screenshot_directory;
    std::filesystem::path metrics_report_directory;
    std::filesystem::path flip_audit_ack;
    std::filesystem::path flip_audit_frame_directory;
    std::filesystem::path vertex_shader;
    std::filesystem::path fragment_shader;
    std::filesystem::path texture_convert_shader;
    std::filesystem::path pipeline_cache;
    bool live_render_stream = false;
    bool strict_render_validation = false;
    bool analyze_render_stream_only = false;
    bool cpu_vertex_programs = false;
    bool cpu_vertex_attributes = false;
    bool cpu_texture_conversion = false;
    std::filesystem::path controller_state_json;
};

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
    size_t index) {
    return index == 0u || words[index].run_id != words[index - 1u].run_id;
}

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

Nv2aVertexAttributes default_nv2a_vertex_attributes() {
    Nv2aVertexAttributes attributes{};
    for (auto& attribute : attributes) {
        attribute = {0.0f, 0.0f, 0.0f, 1.0f};
    }
    attributes[3] = {1.0f, 1.0f, 1.0f, 1.0f};
    return attributes;
}

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

uint32_t nv2a_canonical_resource_address(uint32_t address) {
    // Burnout's texture DMA window aliases 0x2xxxxxxx guest offsets onto the
    // same physical NV2A allocation used by 0x0xxxxxxx surface offsets.
    return (address & 0xF0000000u) == 0x20000000u
        ? address & ~0x20000000u
        : address;
}

bool nv2a_texture_format_matches(
    const std::string& format,
    uint32_t format_raw) {
    switch ((format_raw >> 8u) & 0xFFu) {
    case 0x05u: return format == "R5G6B5";
    case 0x06u: return format == "A8R8G8B8";
    case 0x07u: return format == "X8R8G8B8";
    case 0x0Cu: return format == "DXT1";
    case 0x0Eu: return format == "DXT3";
    case 0x0Fu: return format == "DXT5";
    case 0x12u: return format == "A8R8G8B8_LINEAR";
    case 0x1Eu: return format == "X8R8G8B8_LINEAR";
    default:
        constexpr char hexadecimal[] = "0123456789ABCDEF";
        const uint32_t color_format = (format_raw >> 8u) & 0xFFu;
        std::array<char, 10> name{
            'f', 'o', 'r', 'm', 'a', 't', '_', '0', '0', '\0'};
        name[7] = hexadecimal[(color_format >> 4u) & 0xFu];
        name[8] = hexadecimal[color_format & 0xFu];
        return format == name.data();
    }
}

bool nv2a_texture_format_is_linear(uint32_t format_raw) {
    const uint32_t color_format = (format_raw >> 8u) & 0xFFu;
    return color_format == 0x12u || color_format == 0x1Eu;
}

std::pair<uint32_t, uint32_t> nv2a_texture_extent(
    uint32_t format_raw,
    uint32_t image_rect_raw) {
    if (nv2a_texture_format_is_linear(format_raw) && image_rect_raw != 0u) {
        return {
            (image_rect_raw >> 16u) & 0xFFFFu,
            image_rect_raw & 0xFFFFu,
        };
    }
    return {
        1u << ((format_raw >> 20u) & 0xFu),
        1u << ((format_raw >> 24u) & 0xFu),
    };
}

bool host_texture_matches_draw(
    const HostTexture& texture,
    const NativeDraw& draw) {
    if (!draw.texture_enabled) {
        return texture.guest_address == 0u;
    }
    if (draw.texture_stage >= draw.texture_formats.size()
        || nv2a_canonical_resource_address(texture.guest_address)
            != nv2a_canonical_resource_address(draw.texture_address)) {
        return false;
    }
    const uint32_t format_raw = draw.texture_formats[draw.texture_stage];
    const auto [width, height] = nv2a_texture_extent(
        format_raw,
        draw.texture_image_rects[draw.texture_stage]);
    return texture.width == width
        && texture.height == height
        && nv2a_texture_format_matches(texture.format, format_raw);
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

struct NativePipelineState {
    uint32_t primitive = 6;
    uint32_t blend_enable = 0;
    uint32_t blend_source_factor = 1;
    uint32_t blend_destination_factor = 0;
    uint32_t blend_equation = 0x8006;
    uint32_t color_mask = 0x01010101;
    uint32_t depth_test_enable = 0;
    uint32_t depth_function = 0x0203;
    uint32_t depth_write_enable = 1;
    uint32_t cull_face_enable = 0;
    uint32_t cull_face = 0x0405;
    uint32_t front_face = 0x0900;
    bool raw_attribute_fetch = false;
    bool operator==(const NativePipelineState& other) const {
        return primitive == other.primitive
            && blend_enable == other.blend_enable
            && blend_source_factor == other.blend_source_factor
            && blend_destination_factor == other.blend_destination_factor
            && blend_equation == other.blend_equation
            && color_mask == other.color_mask
            && depth_test_enable == other.depth_test_enable
            && depth_function == other.depth_function
            && depth_write_enable == other.depth_write_enable
            && cull_face_enable == other.cull_face_enable
            && cull_face == other.cull_face
            && front_face == other.front_face
            && raw_attribute_fetch == other.raw_attribute_fetch;
    }
};

struct NativePipelineStateHash {
    size_t operator()(const NativePipelineState& state) const noexcept {
        size_t value = 1469598103934665603ull;
        const auto mix = [&](uint32_t field) {
            value ^= static_cast<size_t>(field);
            value *= 1099511628211ull;
        };
        mix(state.primitive);
        mix(state.blend_enable);
        mix(state.blend_source_factor);
        mix(state.blend_destination_factor);
        mix(state.blend_equation);
        mix(state.color_mask);
        mix(state.depth_test_enable);
        mix(state.depth_function);
        mix(state.depth_write_enable);
        mix(state.cull_face_enable);
        mix(state.cull_face);
        mix(state.front_face);
        mix(state.raw_attribute_fetch ? 1u : 0u);
        return value;
    }
};

struct HostPipeline {
    NativePipelineState state{};
    VkPipeline pipeline = VK_NULL_HANDLE;
};

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

NativeFragmentState fragment_state_for_draw(const NativeDraw& draw) {
    const uint32_t texture_stage = std::min<uint32_t>(
        draw.texture_stage, 3u);
    NativeFragmentState state{};
    state.alpha_test_enable = draw.alpha_test_enable;
    state.alpha_function = draw.alpha_function;
    state.alpha_reference = draw.alpha_reference & 0xFFu;
    const uint32_t programmed_texture_mode = (draw.shader_stage_program
        >> (texture_stage * 5u)) & 0x1Fu;
    state.texture_mode = draw.texture_enabled
        ? programmed_texture_mode
        : 0u;
    state.texture_alpha_kill = state.texture_mode != 0u
        ? draw.texture_controls[texture_stage] & (1u << 2u)
        : 0u;
    const uint32_t texture_color_format =
        (draw.texture_formats[texture_stage] >> 8u) & 0xFFu;
    state.texture_opaque_alpha = state.texture_mode != 0u
        && (texture_color_format == 0x07u
            || texture_color_format == 0x1Eu);
    state.texture_stage = texture_stage;
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
    if (draw.texture_enabled
        && draw.texture_stage < draw.texture_formats.size()) {
        const uint32_t format = draw.texture_formats[draw.texture_stage];
        const auto [width, height] = nv2a_texture_extent(
            format,
            draw.texture_image_rects[draw.texture_stage]);
        state.texture_linear = nv2a_texture_format_is_linear(format) ? 1u : 0u;
        state.texture_width = width;
        state.texture_height = height;
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
    PresentedVertexTransformDiagnostics* diagnostics = nullptr) {
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
        if ((result.output_masks[0] & 12u) == 12u
            && std::isfinite(position[0]) && std::isfinite(position[1])) {
            ++local.position_output_vertex_count;
            local.clip_position_bounds.include(position[0], position[1]);
            if ((result.output_masks[0] & 2u) != 0u) {
                local.program_output_z_bounds.include(position[2]);
            }
            if ((result.output_masks[0] & 1u) != 0u) {
                local.program_output_w_bounds.include(position[3]);
            }
            if ((result.output_masks[0] & 1u) != 0u
                && std::isfinite(position[3])) {
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
            if ((result.output_masks[0] & 1u) != 0u) vertex.w = position[3];
            vertex.program_position_valid =
                (result.output_masks[0] & 15u) == 15u
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
        const uint32_t texture_output = std::min<uint32_t>(
            9u + draw.texture_stage, 15u);
        if (result.output_masks[texture_output]) {
            ++local.texture_output_vertex_count;
            const auto& texture = result.outputs[texture_output];
            if (result.output_masks[texture_output] & 8u) vertex.u = texture[0];
            if (result.output_masks[texture_output] & 4u) vertex.v = texture[1];
            if (result.output_masks[texture_output] & 2u) vertex.texture_r = texture[2];
            if (result.output_masks[texture_output] & 1u) vertex.texture_q = texture[3];
        }
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
    PresentedVertexTransformDiagnostics* diagnostics = nullptr) {
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
    const uint32_t format_raw = draw.texture_formats[draw.texture_stage];
    if (!nv2a_texture_format_is_linear(format_raw)) {
        return 0u;
    }
    const auto [width, height] = nv2a_texture_extent(
        format_raw,
        draw.texture_image_rects[draw.texture_stage]);
    if (width == 0u || height == 0u) {
        return 0u;
    }
    for (uint32_t index = 0; index < draw.vertex_count; ++index) {
        NativeVertex& vertex = vertices[draw.first_vertex + index];
        vertex.u /= static_cast<float>(width);
        vertex.v /= static_cast<float>(height);
    }
    return draw.vertex_count;
}

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

struct GpuRawVertexDirtyRange {
    uint32_t offset = 0;
    uint32_t size = 0;
};

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
            if (payload_size == 0u || logical_write_count == 0u) {
                throw std::runtime_error("invalid live command span size");
            }
            std::vector<uint8_t> payload(payload_size);
            file.read(
                reinterpret_cast<char*>(payload.data()),
                static_cast<std::streamsize>(payload.size()));
            if (!file) {
                throw std::runtime_error("truncated live command span payload");
            }
            for (size_t offset = 0u; offset < payload.size(); offset += 8u) {
                const size_t size = std::min<size_t>(8u, payload.size() - offset);
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

bool append_recovered_d3d_binary_stream(
    const std::filesystem::path& path,
    std::vector<RecoveredD3DCommand>& commands) {
    std::ifstream file(path, std::ios::binary | std::ios::ate);
    if (!file) {
        return false;
    }
    const std::streamoff byte_size = file.tellg();
    constexpr std::streamoff header_size = 8;
    constexpr std::streamoff record_size = 16;
    if (byte_size < header_size || (byte_size - header_size) % record_size != 0) {
        return false;
    }
    const size_t record_count = static_cast<size_t>(
        (byte_size - header_size) / record_size);
    if (record_count < commands.size()) {
        return false;
    }
    file.seekg(0, std::ios::beg);
    std::array<char, 8> magic{};
    file.read(magic.data(), static_cast<std::streamsize>(magic.size()));
    if (std::string(magic.data(), magic.size()) != "B2APPND1") {
        return false;
    }
    file.seekg(
        header_size + static_cast<std::streamoff>(commands.size()) * record_size,
        std::ios::beg);
    commands.reserve(record_count);
    while (commands.size() < record_count) {
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
            return false;
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
    return true;
}

std::vector<RecoveredTextureResource> load_recovered_texture_resources(
    const std::filesystem::path& path,
    bool required = false,
    std::vector<RecoveredTextureResource>* reusable_resources = nullptr,
    uint64_t* reused_resource_count = nullptr,
    uint64_t* reused_payload_bytes = nullptr) {
    if (reused_resource_count != nullptr) {
        *reused_resource_count = 0u;
    }
    if (reused_payload_bytes != nullptr) {
        *reused_payload_bytes = 0u;
    }
    std::ifstream file(path, std::ios::binary);
    if (!file) {
        if (required) {
            throw std::runtime_error(
                "cannot open live resource snapshot: " + path.string());
        }
        return {};
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

std::string load_recovered_frontend_text(const std::filesystem::path& path) {
    const auto text = read_text_file_shared(path);
    if (!text.has_value()) {
        return {};
    }
    const std::regex field_regex("\"frontend_text\"\\s*:\\s*\"([^\"]*)\"");
    std::smatch match;
    return std::regex_search(*text, match, field_regex) ? match[1].str() : std::string{};
}

std::filesystem::path load_recovered_resource_snapshot_path(
    const std::filesystem::path& stream_path) {
    const auto text = read_text_file_shared(stream_path);
    if (!text.has_value()) {
        return stream_path;
    }
    const auto field = json_object_field_text(*text, "resource_snapshot_path");
    return field.has_value() ? std::filesystem::path(*field) : stream_path;
}

std::filesystem::path load_recovered_command_snapshot_path(
    const std::filesystem::path& stream_path) {
    const auto text = read_text_file_shared(stream_path);
    if (!text.has_value()) {
        return {};
    }
    const auto field = json_object_field_text(*text, "command_snapshot_path");
    return field.has_value() ? std::filesystem::path(*field) : std::filesystem::path{};
}

RecoveredD3DStreamSource load_or_build_recovered_d3d_command_stream(
    const std::filesystem::path& render_stream_json,
    bool load_textures = true,
    const std::string* supplied_manifest_text = nullptr,
    bool load_commands = true,
    bool require_resource_source = false,
    std::vector<RecoveredTextureResource>* reusable_resources = nullptr,
    uint64_t* reused_resource_count = nullptr,
    uint64_t* reused_payload_bytes = nullptr) {
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

inline void update_d3d_state_seed(
    InterpretedD3DStream& interpreted,
    uint32_t value) {
    if (interpreted.state_seed_updates_required) {
        interpreted.state_seed = mix_d3d_state_seed(
            interpreted.state_seed, value);
    }
}

VkClearValue color_from_interpreted_d3d_state(uint32_t seed) {
    const float red = 0.18f + static_cast<float>((seed >> 16) & 0xFFu) / 255.0f * 0.62f;
    const float green = 0.18f + static_cast<float>((seed >> 8) & 0xFFu) / 255.0f * 0.62f;
    const float blue = 0.18f + static_cast<float>(seed & 0xFFu) / 255.0f * 0.62f;
    VkClearValue color{};
    color.color = {{red, green, blue, 1.0f}};
    return color;
}

VkClearValue color_from_d3d_argb(uint32_t argb) {
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
            } else if (register_offset == 0x18u) {
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

using Nv2aBootstrapMethod = std::pair<uint32_t, uint32_t>;

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
        append(base + 0x18u, draw.texture_image_rects[stage]);
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
    if ((bulk_indexed || bulk_inline) && complete_packet) {
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
            } else {
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

void interpret_recovered_d3d_append(
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

void interpret_recovered_d3d_packed_append(
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

std::vector<RecoveredD3DCommandSpan> recovered_d3d_command_spans_from_packed(
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

uint64_t interpret_recovered_d3d_span_append(
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

InterpretedD3DStream interpret_recovered_d3d_stream(
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

uint32_t nv2a_vertex_element_size(uint32_t format_raw) {
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

void append_gpu_raw_vertex_dirty_range(
    GpuRawVertexResourceCache& cache,
    uint32_t offset,
    uint32_t size) {
    if (size == 0u) {
        return;
    }
    constexpr uint64_t kMergeGapBytes = 64u;
    const uint64_t range_end = static_cast<uint64_t>(offset) + size;
    if (!cache.dirty_ranges.empty()) {
        GpuRawVertexDirtyRange& previous = cache.dirty_ranges.back();
        const uint64_t previous_end = static_cast<uint64_t>(previous.offset)
            + previous.size;
        if (offset <= previous_end + kMergeGapBytes) {
            previous.size = static_cast<uint32_t>(
                std::max(previous_end, range_end) - previous.offset);
            return;
        }
    }
    cache.dirty_ranges.push_back({offset, size});
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
            append_gpu_raw_vertex_dirty_range(
                cache, 0u, static_cast<uint32_t>(packed_size));
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
                append_gpu_raw_vertex_dirty_range(
                    cache,
                    slot.packed_offset + static_cast<uint32_t>(local_offset),
                    static_cast<uint32_t>(chunk_size));
            }
        }
    }
    for (const GpuRawVertexDirtyRange& range : cache.dirty_ranges) {
        cache.dirty_bytes += range.size;
    }
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
    bool enable_gpu_raw_attribute_fetch = false,
    bool resources_unchanged = false) {
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

class DebugLog {
public:
    void configure_live_mode(bool live_mode) {
        echo_stdout_ = !live_mode;
        flush_interval_ = live_mode ? 120u : 1u;
    }

    void open(const std::filesystem::path& path) {
        if (path.empty()) {
            enabled_ = false;
            return;
        }
        if (path.has_parent_path()) {
            std::filesystem::create_directories(path.parent_path());
        }
        file_.open(path, std::ios::out | std::ios::trunc);
        if (!file_) {
            throw std::runtime_error("failed to open debug json log: " + path.string());
        }
        path_ = path;
    }

    void emit(
        const std::string& event,
        std::initializer_list<std::pair<std::string, std::string>> fields = {}) {
        if (!enabled_) {
            return;
        }
        std::ostringstream line;
        line << "{\"sequence\":" << sequence_++ << ",\"event\":" << json_string(event);
        for (const auto& [key, value] : fields) {
            line << "," << json_string(key) << ":" << value;
        }
        line << "}";
        if (file_) {
            file_ << line.str() << "\n";
            if (++pending_lines_ >= flush_interval_) {
                file_.flush();
                pending_lines_ = 0;
            }
        }
        if (echo_stdout_) {
            std::cout << line.str() << "\n";
        }
    }

    const std::filesystem::path& path() const {
        return path_;
    }

    bool enabled() const {
        return enabled_;
    }

private:
    uint64_t sequence_ = 0;
    uint32_t pending_lines_ = 0;
    uint32_t flush_interval_ = 1;
    bool echo_stdout_ = true;
    bool enabled_ = true;
    std::ofstream file_;
    std::filesystem::path path_;
};

class VulkanFirstFrameApp {
public:
    explicit VulkanFirstFrameApp(Options options) : options_(std::move(options)) {}

    int run() {
        log_.configure_live_mode(
            options_.live_render_stream && !options_.analyze_render_stream_only);
        log_.open(options_.debug_json);
        log_.emit(
            "startup",
            {
                {"target_platform", json_string("windows")},
                {"renderer_backend", json_string("vulkan")},
                {"width", std::to_string(options_.width)},
                {"height", std::to_string(options_.height)},
                {"max_frames", std::to_string(options_.max_frames)},
                {"vertex_program_backend", json_string(
                    options_.cpu_vertex_programs ? "cpu" : "gpu")},
                {"vertex_attribute_backend", json_string(
                    options_.cpu_vertex_attributes ? "cpu" : "gpu")},
                {"texture_conversion_backend", json_string(
                    options_.cpu_texture_conversion ? "cpu" : "gpu")},
                {"presentation_pipeline_depth", std::to_string(
                    options_.presentation_pipeline_depth)},
            });

        if (options_.analyze_render_stream_only) {
            swapchain_extent_ = {options_.width, options_.height};
            if (!load_initial_recovered_render_work()) {
                throw std::runtime_error(
                    "render manifest was incomplete during headless analysis");
            }
            const GpuRawAttributeValidation raw_attribute_validation =
                validate_gpu_raw_attribute_fetch(
                    interpreted_stream_, recovered_source_.textures);
            log_.emit(
                "nv2a_gpu_raw_attribute_validation",
                {
                    {"eligible_draws", std::to_string(
                        raw_attribute_validation.eligible_draw_count)},
                    {"eligible_vertices", std::to_string(
                        raw_attribute_validation.eligible_vertex_count)},
                    {"decoded_attributes", std::to_string(
                        raw_attribute_validation.decoded_attribute_count)},
                    {"compared_vertices", std::to_string(
                        raw_attribute_validation.compared_vertex_count)},
                    {"mismatch_vertices", std::to_string(
                        raw_attribute_validation.mismatch_vertex_count)},
                    {"mapping_fallback_draws", std::to_string(
                        raw_attribute_validation.mapping_fallback_draw_count)},
                    {"packed_resource_bytes", std::to_string(
                        raw_attribute_validation.packed_resource_bytes)},
                    {"passed", json_bool(
                        raw_attribute_validation.mismatch_vertex_count == 0u)},
                });
            const GpuRawDirtyRangeValidation dirty_range_validation =
                validate_gpu_raw_dirty_range_uploads();
            log_.emit(
                "nv2a_gpu_raw_dirty_range_validation",
                {
                    {"unchanged_generation_passed", json_bool(
                        dirty_range_validation.unchanged_generation_passed)},
                    {"changed_generation_passed", json_bool(
                        dirty_range_validation.changed_generation_passed)},
                    {"layout_change_passed", json_bool(
                        dirty_range_validation.layout_change_passed)},
                    {"changed_ranges", std::to_string(
                        dirty_range_validation.changed_range_count)},
                    {"changed_upload_bytes", std::to_string(
                        dirty_range_validation.changed_upload_bytes)},
                    {"packed_resource_bytes", std::to_string(
                        dirty_range_validation.packed_resource_bytes)},
                    {"passed", json_bool(dirty_range_validation.passed())},
                });
            const std::vector<NativeVertex> vertices =
                prepare_presented_vertices();
            emit_presented_vertex_transform_diagnostics(true);
            emit_presented_render_state_diagnostics();
            log_.emit(
                "render_stream_analysis_complete",
                {
                    {"source_commands", std::to_string(
                        options_.live_render_stream
                            ? interpreted_source_command_count_
                            : recovered_source_.commands.size())},
                    {"guest_flips", std::to_string(interpreted_stream_.flip_count)},
                    {"launch_transform_program_count", std::to_string(interpreted_stream_.launch_transform_program_count)},
                    {"failed_launch_transform_program_count", std::to_string(interpreted_stream_.failed_launch_transform_program_count)},
                    {"presented_draws", std::to_string(interpreted_stream_.presented_draw_count)},
                    {"zero_count_method_words", std::to_string(interpreted_stream_.zero_count_method_word_count)},
                    {"zero_count_indexed_array_noop_packets", std::to_string(interpreted_stream_.zero_count_indexed_array_noop_packet_count)},
                    {"ordered_push_buffer_appends", std::to_string(
                        interpreted_stream_.ordered_push_buffer_append_count)},
                    {"indexed_word_push_buffer_appends", std::to_string(
                        interpreted_stream_.indexed_word_push_buffer_append_count)},
                    {"reconstructed_push_buffer_appends", std::to_string(
                        interpreted_stream_.reconstructed_push_buffer_append_count)},
                    {"surface_payload_scans_skipped", std::to_string(
                        interpreted_stream_.surface_payload_scan_skipped_count)},
                    {"feedback_spec_cache_builds", std::to_string(
                        feedback_spec_cache_build_count_)},
                    {"feedback_spec_cache_hits", std::to_string(
                        feedback_spec_cache_hit_count_)},
                    {"uploaded_vertices", std::to_string(vertices.size())},
                    {"presented_surface_color_offset", std::to_string(presented_surface_color_offset_)},
                    {"continuation_bootstrapped", json_bool(continuation_analysis_bootstrap_)},
                    {"state_history_complete", json_bool(!continuation_analysis_bootstrap_)},
                });
            log_.emit("shutdown", {{"status", json_string("analysis_ok")}});
            return 0;
        }

        create_instance();
        if (options_.list_adapters_only) {
            list_adapters();
            cleanup();
            return 0;
        }
        create_window();
        initialize_controller_backend();
        create_surface();
        pick_physical_device();
        create_logical_device();
        create_pipeline_cache();
        create_swapchain();
        create_image_views();
        create_depth_resources();
        create_render_pass();
        create_framebuffers();
        create_command_pool();
        create_gpu_timing_resources();
        if (!load_initial_recovered_render_work()) {
            throw std::runtime_error("live render manifest was incomplete at startup");
        }
        create_native_graphics_pipeline();
        create_native_render_resources();
        create_readback_buffer();
        create_command_buffers();
        if (publication_event_) {
            ResetEvent(publication_event_);
        }
        acknowledge_current_presentation();
        create_sync_objects();
        main_loop();
        vkDeviceWaitIdle(device_);
        cleanup();
        log_.emit("shutdown", {{"status", json_string("ok")}});
        return 0;
    }

private:
    std::optional<std::string> read_live_render_manifest() {
        if (!options_.flip_audit_ack.empty()) {
            // Audit publications are atomic replacements, so every read must
            // open the newest file identity.
            return read_text_file_shared(options_.render_stream_json);
        }
        if (live_manifest_file_ == INVALID_HANDLE_VALUE) {
            live_manifest_file_ = CreateFileW(
                options_.render_stream_json.c_str(),
                GENERIC_READ,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                nullptr,
                OPEN_EXISTING,
                FILE_ATTRIBUTE_NORMAL,
                nullptr);
        }
        return read_text_handle_shared(live_manifest_file_);
    }

    bool load_initial_recovered_render_work() {
        const auto deadline = std::chrono::steady_clock::now()
            + std::chrono::seconds(2);
        uint32_t retry_count = 0;
        while (!load_recovered_render_work()) {
            if (!options_.live_render_stream
                || std::chrono::steady_clock::now() >= deadline) {
                return false;
            }
            ++retry_count;
            std::this_thread::sleep_for(std::chrono::milliseconds(2));
        }
        if (retry_count != 0) {
            log_.emit(
                "live_render_startup_manifest_retried",
                {{"retries", std::to_string(retry_count)}});
        }
        return true;
    }

    bool update_flip_audit_manifest_state(const std::string& manifest_text) {
        if (options_.flip_audit_ack.empty() || manifest_text.empty()) {
            return options_.flip_audit_ack.empty();
        }
        const bool enabled = manifest_text.find(
            "\"lossless_flip_audit\":true") != std::string::npos;
        const auto flip = json_object_field_text(manifest_text, "audit_flip_index");
        const auto guest_steps = json_object_field_text(
            manifest_text, "audit_guest_steps");
        const auto flip_value = json_object_field_text(
            manifest_text, "audit_flip_value");
        const auto command_count = json_object_field_text(
            manifest_text, "audit_command_record_count");
        const auto health_reason = json_object_field_text(
            manifest_text, "audit_health_reason");
        const auto generation = json_object_field_text(
            manifest_text, "command_stream_generation");
        if (!enabled || !flip.has_value()
            || !guest_steps.has_value() || !flip_value.has_value()
            || !command_count.has_value()
            || !generation.has_value()) {
            return false;
        }
        current_audit_flip_ = parse_json_u32_text(*flip, "audit flip index");
        current_audit_guest_steps_ = parse_json_u32_text(
            *guest_steps, "audit guest steps");
        current_audit_flip_value_ = parse_json_u32_text(
            *flip_value, "audit flip value");
        current_audit_command_count_ = parse_json_u32_text(
            *command_count, "audit command count");
        current_audit_generation_ = *generation;
        current_audit_health_reason_ = health_reason.value_or(std::string{});
        return true;
    }

    std::string flip_audit_health_check_reason() const {
        if (options_.flip_audit_ack.empty() || current_audit_flip_ == 0u) {
            return {};
        }
        if (!current_audit_health_reason_.empty()) {
            return current_audit_health_reason_;
        }
        if (acknowledged_audit_flip_ == 0u) {
            return "first_flip";
        }
        if (live_resource_generation_ != last_audit_resource_generation_) {
            return "resource_changed";
        }
        const uint32_t command_delta = current_audit_command_count_
            - std::min(current_audit_command_count_, last_audit_command_count_);
        if (command_delta != last_audit_command_delta_) {
            return "command_shape_changed";
        }
        if (current_audit_flip_value_ != last_audit_flip_value_) {
            return "flip_value_changed";
        }
        if (options_.flip_audit_health_interval != 0u
            && current_audit_flip_ % options_.flip_audit_health_interval == 0u) {
            return "periodic_sample";
        }
        return {};
    }

    bool read_live_command_stream_delta(
        const std::filesystem::path& path,
        uint64_t snapshot_base_record_count,
        uint64_t first_record_count,
        uint64_t required_record_count) {
        last_command_file_open_us_ = 0;
        last_command_file_read_us_ = 0;
        last_command_record_validation_us_ = 0;
        last_command_read_bytes_ = 0u;
        last_live_command_mmio_count_ = 0u;
        last_command_file_reused_ = path == live_command_file_path_
            && live_command_file_.is_open();
        if (first_record_count < snapshot_base_record_count
            || required_record_count < first_record_count) {
            throw std::runtime_error(
                "live command cursor is outside the published snapshot");
        }
        if (!last_command_file_reused_) {
            const auto open_begin = std::chrono::steady_clock::now();
            live_command_file_.close();
            live_command_file_.clear();
            live_command_file_.open(path, std::ios::binary);
            if (!live_command_file_) {
                return false;
            }
            std::array<char, 8> magic{};
            live_command_file_.read(
                magic.data(), static_cast<std::streamsize>(magic.size()));
            if (std::string(magic.data(), magic.size()) != "B2APPND1") {
                live_command_file_.close();
                return false;
            }
            live_command_file_path_ = path;
            last_command_file_open_us_ = std::chrono::duration_cast<
                std::chrono::microseconds>(
                    std::chrono::steady_clock::now() - open_begin).count();
        }
        constexpr uint64_t header_size = 8u;
        constexpr uint64_t record_size = kRecoveredD3DCommandRecordSize;
        // The manifest is the validated publication boundary and is written
        // only after the producer flushes this many records. Avoid a separate
        // filesystem metadata query and do not parse commands from a newer,
        // in-progress guest frame merely because they already exist on disk.
        const uint64_t local_first_record =
            first_record_count - snapshot_base_record_count;
        const uint64_t appended_count_u64 =
            required_record_count - first_record_count;
        if (local_first_record
                > (static_cast<uint64_t>(
                    std::numeric_limits<std::streamoff>::max()) - header_size)
                    / record_size
            || appended_count_u64
                > std::numeric_limits<size_t>::max() / record_size) {
            throw std::runtime_error("live command delta is too large");
        }
        live_command_delta_bytes_.clear();
        last_native_command_read_count_ = 0u;
        if (appended_count_u64 == 0u) {
            return true;
        }
        const size_t appended_count = static_cast<size_t>(appended_count_u64);
        live_command_delta_bytes_.resize(appended_count * record_size);
        const auto read_begin = std::chrono::steady_clock::now();
        live_command_file_.clear();
        live_command_file_.seekg(
            static_cast<std::streamoff>(
                header_size + local_first_record * record_size),
            std::ios::beg);
        live_command_file_.read(
            reinterpret_cast<char*>(live_command_delta_bytes_.data()),
            static_cast<std::streamsize>(live_command_delta_bytes_.size()));
        last_command_file_read_us_ = std::chrono::duration_cast<
            std::chrono::microseconds>(
                std::chrono::steady_clock::now() - read_begin).count();
        if (!live_command_file_) {
            live_command_delta_bytes_.clear();
            return false;
        }
        last_command_read_bytes_ = live_command_delta_bytes_.size();
        const auto validation_begin = std::chrono::steady_clock::now();
        for (size_t record_index = 0; record_index < appended_count; ++record_index) {
            const uint8_t* record = live_command_delta_bytes_.data()
                + record_index * record_size;
            const uint8_t kind_raw = record[0];
            const uint8_t size = record[1];
            if (size == 0 || size > 8u || kind_raw > 1u) {
                live_command_delta_bytes_.clear();
                return false;
            }
            last_live_command_mmio_count_ += static_cast<uint32_t>(kind_raw == 0u);
        }
        last_command_record_validation_us_ = std::chrono::duration_cast<
            std::chrono::microseconds>(
                std::chrono::steady_clock::now() - validation_begin).count();
        last_native_command_read_count_ = appended_count;
        native_command_read_count_ += appended_count;
        return true;
    }

    bool read_live_command_span_stream_delta(
        const std::filesystem::path& path,
        uint64_t snapshot_base_byte_count,
        uint64_t first_byte_count,
        uint64_t required_byte_count,
        uint64_t expected_logical_write_count) {
        last_command_file_open_us_ = 0;
        last_command_file_read_us_ = 0;
        last_command_record_validation_us_ = 0;
        last_command_read_bytes_ = 0u;
        last_live_command_mmio_count_ = 0u;
        last_native_command_span_count_ = 0u;
        last_command_file_reused_ = path == live_command_file_path_
            && live_command_file_.is_open();
        if (first_byte_count < snapshot_base_byte_count
            || required_byte_count < first_byte_count) {
            throw std::runtime_error(
                "live command span cursor is outside the published snapshot");
        }
        if (!last_command_file_reused_) {
            const auto open_begin = std::chrono::steady_clock::now();
            live_command_file_.close();
            live_command_file_.clear();
            live_command_file_.open(path, std::ios::binary);
            if (!live_command_file_) {
                return false;
            }
            std::array<char, 8> magic{};
            live_command_file_.read(
                magic.data(), static_cast<std::streamsize>(magic.size()));
            if (std::string(magic.data(), magic.size()) != "B2SPAN01") {
                live_command_file_.close();
                return false;
            }
            live_command_file_path_ = path;
            last_command_file_open_us_ = std::chrono::duration_cast<
                std::chrono::microseconds>(
                    std::chrono::steady_clock::now() - open_begin).count();
        }
        constexpr uint64_t header_size = 8u;
        const uint64_t local_first_byte =
            first_byte_count - snapshot_base_byte_count;
        const uint64_t appended_byte_count =
            required_byte_count - first_byte_count;
        if (local_first_byte
                > static_cast<uint64_t>(
                    std::numeric_limits<std::streamoff>::max()) - header_size
            || appended_byte_count > std::numeric_limits<size_t>::max()) {
            throw std::runtime_error("live command span delta is too large");
        }
        live_command_delta_bytes_.clear();
        last_native_command_read_count_ = 0u;
        if (appended_byte_count == 0u) {
            return expected_logical_write_count == 0u;
        }
        live_command_delta_bytes_.resize(
            static_cast<size_t>(appended_byte_count));
        const auto read_begin = std::chrono::steady_clock::now();
        live_command_file_.clear();
        live_command_file_.seekg(
            static_cast<std::streamoff>(header_size + local_first_byte),
            std::ios::beg);
        live_command_file_.read(
            reinterpret_cast<char*>(live_command_delta_bytes_.data()),
            static_cast<std::streamsize>(live_command_delta_bytes_.size()));
        last_command_file_read_us_ = std::chrono::duration_cast<
            std::chrono::microseconds>(
                std::chrono::steady_clock::now() - read_begin).count();
        if (!live_command_file_) {
            live_command_delta_bytes_.clear();
            return false;
        }
        last_command_read_bytes_ = live_command_delta_bytes_.size();
        const auto validation_begin = std::chrono::steady_clock::now();
        uint64_t logical_write_count = 0u;
        size_t cursor = 0u;
        while (cursor < live_command_delta_bytes_.size()) {
            if (live_command_delta_bytes_.size() - cursor
                < kRecoveredD3DCommandSpanHeaderSize) {
                live_command_delta_bytes_.clear();
                return false;
            }
            const uint8_t* header = live_command_delta_bytes_.data() + cursor;
            const uint8_t kind = header[0];
            const uint8_t flags = header[1];
            uint32_t payload_size = 0u;
            uint32_t span_write_count = 0u;
            std::memcpy(&payload_size, header + 8u, sizeof(payload_size));
            std::memcpy(
                &span_write_count,
                header + 12u,
                sizeof(span_write_count));
            cursor += kRecoveredD3DCommandSpanHeaderSize;
            if (kind > 1u || (flags & ~1u) != 0u || payload_size == 0u
                || span_write_count == 0u
                || payload_size > live_command_delta_bytes_.size() - cursor) {
                live_command_delta_bytes_.clear();
                return false;
            }
            last_live_command_mmio_count_ += static_cast<uint32_t>(kind == 0u);
            logical_write_count += span_write_count;
            ++last_native_command_span_count_;
            cursor += payload_size;
        }
        last_command_record_validation_us_ = std::chrono::duration_cast<
            std::chrono::microseconds>(
                std::chrono::steady_clock::now() - validation_begin).count();
        if (logical_write_count != expected_logical_write_count) {
            live_command_delta_bytes_.clear();
            return false;
        }
        last_native_command_read_count_ = static_cast<size_t>(logical_write_count);
        native_command_read_count_ += logical_write_count;
        native_command_span_read_count_ += last_native_command_span_count_;
        return true;
    }

    void create_instance() {
        VkApplicationInfo app_info{};
        app_info.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO;
        app_info.pApplicationName = "b2_recomp_first_frame";
        app_info.applicationVersion = VK_MAKE_VERSION(0, 6, 0);
        app_info.pEngineName = "b2_recomp";
        app_info.engineVersion = VK_MAKE_VERSION(0, 6, 0);
        app_info.apiVersion = VK_API_VERSION_1_1;

        const std::vector<const char*> extensions = {
            VK_KHR_SURFACE_EXTENSION_NAME,
            VK_KHR_WIN32_SURFACE_EXTENSION_NAME,
        };

        VkInstanceCreateInfo create_info{};
        create_info.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO;
        create_info.pApplicationInfo = &app_info;
        create_info.enabledExtensionCount = static_cast<uint32_t>(extensions.size());
        create_info.ppEnabledExtensionNames = extensions.data();

        vk_check(vkCreateInstance(&create_info, nullptr, &instance_), "vkCreateInstance");
        log_.emit(
            "vulkan_instance_created",
            {
                {"api_version", json_string("1.1")},
                {"surface_extension", json_string(VK_KHR_WIN32_SURFACE_EXTENSION_NAME)},
            });
    }

    void list_adapters() {
        uint32_t device_count = 0;
        vk_check(vkEnumeratePhysicalDevices(instance_, &device_count, nullptr), "vkEnumeratePhysicalDevices");
        std::vector<VkPhysicalDevice> devices(device_count);
        if (device_count > 0) {
            vk_check(
                vkEnumeratePhysicalDevices(instance_, &device_count, devices.data()),
                "vkEnumeratePhysicalDevices");
        }
        for (uint32_t index = 0; index < device_count; ++index) {
            VkPhysicalDeviceProperties properties{};
            vkGetPhysicalDeviceProperties(devices[index], &properties);
            log_.emit(
                "vulkan_adapter",
                {
                    {"index", std::to_string(index)},
                    {"name", json_string(properties.deviceName)},
                    {"vendor_id", std::to_string(properties.vendorID)},
                    {"device_id", std::to_string(properties.deviceID)},
                });
        }
    }

    void create_window() {
        hinstance_ = GetModuleHandleW(nullptr);
        const wchar_t* class_name = L"B2RecompFirstFrameWindow";

        WNDCLASSEXW wc{};
        wc.cbSize = sizeof(wc);
        wc.lpfnWndProc = &VulkanFirstFrameApp::window_proc;
        wc.hInstance = hinstance_;
        wc.hCursor = LoadCursor(nullptr, IDC_ARROW);
        wc.lpszClassName = class_name;
        RegisterClassExW(&wc);

        RECT rect{0, 0, static_cast<LONG>(options_.width), static_cast<LONG>(options_.height)};
        AdjustWindowRect(&rect, WS_OVERLAPPEDWINDOW, FALSE);
        hwnd_ = CreateWindowExW(
            0,
            class_name,
            options_.title.c_str(),
            WS_OVERLAPPEDWINDOW,
            CW_USEDEFAULT,
            CW_USEDEFAULT,
            rect.right - rect.left,
            rect.bottom - rect.top,
            nullptr,
            nullptr,
            hinstance_,
            this);
        if (!hwnd_) {
            throw std::runtime_error("CreateWindowExW failed");
        }
        ShowWindow(hwnd_, SW_SHOWNORMAL);
        UpdateWindow(hwnd_);
        log_.emit(
            "window_created",
            {
                {"title", json_string(narrow(options_.title))},
                {"width", std::to_string(options_.width)},
                {"height", std::to_string(options_.height)},
            });
    }

    void create_surface() {
        VkWin32SurfaceCreateInfoKHR create_info{};
        create_info.sType = VK_STRUCTURE_TYPE_WIN32_SURFACE_CREATE_INFO_KHR;
        create_info.hinstance = hinstance_;
        create_info.hwnd = hwnd_;
        vk_check(vkCreateWin32SurfaceKHR(instance_, &create_info, nullptr, &surface_), "vkCreateWin32SurfaceKHR");
        log_.emit("vulkan_surface_created", {{"platform", json_string("win32")}});
    }

    void pick_physical_device() {
        uint32_t device_count = 0;
        vk_check(vkEnumeratePhysicalDevices(instance_, &device_count, nullptr), "vkEnumeratePhysicalDevices");
        if (device_count == 0) {
            throw std::runtime_error("no Vulkan physical devices are available");
        }
        std::vector<VkPhysicalDevice> devices(device_count);
        vk_check(vkEnumeratePhysicalDevices(instance_, &device_count, devices.data()), "vkEnumeratePhysicalDevices");

        for (VkPhysicalDevice candidate : devices) {
            std::optional<QueueFamilySelection> queue_family = find_queue_family(candidate);
            if (!queue_family.has_value() || !device_supports_swapchain(candidate)) {
                continue;
            }
            const SwapchainSupport support = query_swapchain_support(candidate);
            if (support.formats.empty() || support.present_modes.empty()) {
                continue;
            }
            physical_device_ = candidate;
            queue_family_ = *queue_family;
            VkPhysicalDeviceProperties properties{};
            vkGetPhysicalDeviceProperties(candidate, &properties);
            log_.emit(
                "physical_device_selected",
                {
                    {"name", json_string(properties.deviceName)},
                    {"vendor_id", std::to_string(properties.vendorID)},
                    {"device_id", std::to_string(properties.deviceID)},
                    {"queue_family", std::to_string(queue_family_.index)},
                });
            return;
        }
        throw std::runtime_error("no Vulkan device supports graphics presentation and swapchain");
    }

    std::optional<QueueFamilySelection> find_queue_family(VkPhysicalDevice device) const {
        uint32_t count = 0;
        vkGetPhysicalDeviceQueueFamilyProperties(device, &count, nullptr);
        std::vector<VkQueueFamilyProperties> families(count);
        vkGetPhysicalDeviceQueueFamilyProperties(device, &count, families.data());
        for (uint32_t index = 0; index < count; ++index) {
            VkBool32 present_supported = VK_FALSE;
            vkGetPhysicalDeviceSurfaceSupportKHR(device, index, surface_, &present_supported);
            if ((families[index].queueFlags & VK_QUEUE_GRAPHICS_BIT)
                && present_supported == VK_TRUE) {
                return QueueFamilySelection{
                    index,
                    (families[index].queueFlags & VK_QUEUE_COMPUTE_BIT) != 0u,
                };
            }
        }
        return std::nullopt;
    }

    bool device_supports_swapchain(VkPhysicalDevice device) const {
        uint32_t extension_count = 0;
        vkEnumerateDeviceExtensionProperties(device, nullptr, &extension_count, nullptr);
        std::vector<VkExtensionProperties> extensions(extension_count);
        vkEnumerateDeviceExtensionProperties(device, nullptr, &extension_count, extensions.data());
        return std::any_of(
            extensions.begin(),
            extensions.end(),
            [](const VkExtensionProperties& item) {
                return std::string(item.extensionName) == VK_KHR_SWAPCHAIN_EXTENSION_NAME;
            });
    }

    SwapchainSupport query_swapchain_support(VkPhysicalDevice device) const {
        SwapchainSupport support{};
        vk_check(
            vkGetPhysicalDeviceSurfaceCapabilitiesKHR(device, surface_, &support.capabilities),
            "vkGetPhysicalDeviceSurfaceCapabilitiesKHR");

        uint32_t format_count = 0;
        vk_check(
            vkGetPhysicalDeviceSurfaceFormatsKHR(device, surface_, &format_count, nullptr),
            "vkGetPhysicalDeviceSurfaceFormatsKHR");
        support.formats.resize(format_count);
        if (format_count > 0) {
            vk_check(
                vkGetPhysicalDeviceSurfaceFormatsKHR(device, surface_, &format_count, support.formats.data()),
                "vkGetPhysicalDeviceSurfaceFormatsKHR");
        }

        uint32_t present_mode_count = 0;
        vk_check(
            vkGetPhysicalDeviceSurfacePresentModesKHR(device, surface_, &present_mode_count, nullptr),
            "vkGetPhysicalDeviceSurfacePresentModesKHR");
        support.present_modes.resize(present_mode_count);
        if (present_mode_count > 0) {
            vk_check(
                vkGetPhysicalDeviceSurfacePresentModesKHR(
                    device, surface_, &present_mode_count, support.present_modes.data()),
                "vkGetPhysicalDeviceSurfacePresentModesKHR");
        }
        return support;
    }

    void create_logical_device() {
        float queue_priority = 1.0f;
        VkDeviceQueueCreateInfo queue_create_info{};
        queue_create_info.sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO;
        queue_create_info.queueFamilyIndex = queue_family_.index;
        queue_create_info.queueCount = 1;
        queue_create_info.pQueuePriorities = &queue_priority;

        const std::vector<const char*> extensions = {VK_KHR_SWAPCHAIN_EXTENSION_NAME};
        VkDeviceCreateInfo create_info{};
        create_info.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO;
        create_info.queueCreateInfoCount = 1;
        create_info.pQueueCreateInfos = &queue_create_info;
        create_info.enabledExtensionCount = static_cast<uint32_t>(extensions.size());
        create_info.ppEnabledExtensionNames = extensions.data();

        vk_check(vkCreateDevice(physical_device_, &create_info, nullptr, &device_), "vkCreateDevice");
        vkGetDeviceQueue(device_, queue_family_.index, 0, &graphics_queue_);
        log_.emit(
            "logical_device_created",
            {
                {"queue_family", std::to_string(queue_family_.index)},
                {"compute_supported", json_bool(
                    queue_family_.supports_compute)},
            });
    }

    void create_pipeline_cache() {
        std::vector<uint8_t> initial_data;
        if (!options_.pipeline_cache.empty()) {
            std::ifstream input(options_.pipeline_cache, std::ios::binary);
            if (input) {
                input.seekg(0, std::ios::end);
                const std::streamoff size = input.tellg();
                if (size > 0 && size <= 256 * 1024 * 1024) {
                    initial_data.resize(static_cast<size_t>(size));
                    input.seekg(0, std::ios::beg);
                    input.read(
                        reinterpret_cast<char*>(initial_data.data()),
                        static_cast<std::streamsize>(initial_data.size()));
                    if (!input) {
                        initial_data.clear();
                    }
                }
            }
        }
        VkPipelineCacheCreateInfo cache_info{};
        cache_info.sType = VK_STRUCTURE_TYPE_PIPELINE_CACHE_CREATE_INFO;
        cache_info.initialDataSize = initial_data.size();
        cache_info.pInitialData = initial_data.empty()
            ? nullptr
            : initial_data.data();
        VkResult result = vkCreatePipelineCache(
            device_, &cache_info, nullptr, &pipeline_cache_);
        if (result != VK_SUCCESS && !initial_data.empty()) {
            pipeline_cache_ = VK_NULL_HANDLE;
            cache_info.initialDataSize = 0u;
            cache_info.pInitialData = nullptr;
            result = vkCreatePipelineCache(
                device_, &cache_info, nullptr, &pipeline_cache_);
            pipeline_cache_rejected_ = true;
        }
        vk_check(result, "vkCreatePipelineCache");
        pipeline_cache_loaded_bytes_ = pipeline_cache_rejected_
            ? 0u
            : initial_data.size();
        log_.emit(
            "vulkan_pipeline_cache_opened",
            {
                {"path", options_.pipeline_cache.empty()
                    ? "null" : json_string(options_.pipeline_cache.string())},
                {"loaded_bytes", std::to_string(
                    pipeline_cache_loaded_bytes_)},
                {"initial_data_rejected", json_bool(
                    pipeline_cache_rejected_)},
            });
    }

    void persist_pipeline_cache() {
        if (pipeline_cache_ == VK_NULL_HANDLE) {
            return;
        }
        size_t byte_count = 0u;
        VkResult result = vkGetPipelineCacheData(
            device_, pipeline_cache_, &byte_count, nullptr);
        std::vector<uint8_t> data;
        if (result == VK_SUCCESS && byte_count != 0u) {
            data.resize(byte_count);
            result = vkGetPipelineCacheData(
                device_, pipeline_cache_, &byte_count, data.data());
            if (result == VK_SUCCESS) {
                data.resize(byte_count);
            }
        }
        bool written = false;
        if (result == VK_SUCCESS
            && !data.empty()
            && !options_.pipeline_cache.empty()) {
            std::error_code directory_error;
            if (options_.pipeline_cache.has_parent_path()) {
                std::filesystem::create_directories(
                    options_.pipeline_cache.parent_path(),
                    directory_error);
            }
            const std::filesystem::path temporary =
                options_.pipeline_cache.string() + ".tmp";
            if (!directory_error) {
                std::ofstream output(
                    temporary,
                    std::ios::binary | std::ios::trunc);
                output.write(
                    reinterpret_cast<const char*>(data.data()),
                    static_cast<std::streamsize>(data.size()));
                output.close();
                written = static_cast<bool>(output)
                    && MoveFileExW(
                        temporary.c_str(),
                        options_.pipeline_cache.c_str(),
                        MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH);
            }
            if (!written) {
                std::error_code ignored;
                std::filesystem::remove(temporary, ignored);
            }
        }
        pipeline_cache_saved_bytes_ = written ? data.size() : 0u;
        log_.emit(
            "vulkan_pipeline_cache_saved",
            {
                {"path", options_.pipeline_cache.empty()
                    ? "null" : json_string(options_.pipeline_cache.string())},
                {"bytes", std::to_string(pipeline_cache_saved_bytes_)},
                {"written", json_bool(written)},
            });
    }

    void create_swapchain() {
        const SwapchainSupport support = query_swapchain_support(physical_device_);
        const VkSurfaceFormatKHR surface_format = choose_surface_format(support.formats);
        const VkPresentModeKHR present_mode = VK_PRESENT_MODE_FIFO_KHR;
        const VkExtent2D extent = choose_extent(support.capabilities);
        uint32_t image_count = support.capabilities.minImageCount + 1;
        if (support.capabilities.maxImageCount > 0) {
            image_count = std::min(image_count, support.capabilities.maxImageCount);
        }

        VkSwapchainCreateInfoKHR create_info{};
        create_info.sType = VK_STRUCTURE_TYPE_SWAPCHAIN_CREATE_INFO_KHR;
        create_info.surface = surface_;
        create_info.minImageCount = image_count;
        create_info.imageFormat = surface_format.format;
        create_info.imageColorSpace = surface_format.colorSpace;
        create_info.imageExtent = extent;
        create_info.imageArrayLayers = 1;
        if ((support.capabilities.supportedUsageFlags
                & VK_IMAGE_USAGE_TRANSFER_SRC_BIT) == 0) {
            throw std::runtime_error(
                "surface does not support swapchain transfer-source feedback");
        }
        create_info.imageUsage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT
            | VK_IMAGE_USAGE_TRANSFER_SRC_BIT;
        create_info.imageSharingMode = VK_SHARING_MODE_EXCLUSIVE;
        create_info.preTransform = support.capabilities.currentTransform;
        create_info.compositeAlpha = VK_COMPOSITE_ALPHA_OPAQUE_BIT_KHR;
        create_info.presentMode = present_mode;
        create_info.clipped = VK_TRUE;
        create_info.oldSwapchain = VK_NULL_HANDLE;

        vk_check(vkCreateSwapchainKHR(device_, &create_info, nullptr, &swapchain_), "vkCreateSwapchainKHR");
        swapchain_format_ = surface_format.format;
        swapchain_extent_ = extent;

        vk_check(vkGetSwapchainImagesKHR(device_, swapchain_, &image_count, nullptr), "vkGetSwapchainImagesKHR");
        swapchain_images_.resize(image_count);
        vk_check(
            vkGetSwapchainImagesKHR(device_, swapchain_, &image_count, swapchain_images_.data()),
            "vkGetSwapchainImagesKHR");

        log_.emit(
            "swapchain_created",
            {
                {"image_count", std::to_string(image_count)},
                {"width", std::to_string(extent.width)},
                {"height", std::to_string(extent.height)},
                {"present_mode", json_string("fifo")},
                {"format", std::to_string(surface_format.format)},
                {"color_space", std::to_string(surface_format.colorSpace)},
                {"xbox_combiner_unorm_output", json_bool(
                    surface_format.format == VK_FORMAT_B8G8R8A8_UNORM
                    || surface_format.format == VK_FORMAT_R8G8B8A8_UNORM)},
            });
    }

    VkSurfaceFormatKHR choose_surface_format(const std::vector<VkSurfaceFormatKHR>& formats) const {
        const auto preferred = std::find_if(
            formats.begin(),
            formats.end(),
            [](const VkSurfaceFormatKHR& format) {
                // NV2A register combiners operate on the title's packed
                // 8-bit texture/color values. Sampling those resources as
                // UNORM and then targeting an sRGB attachment applies an
                // extra transfer function and produces the washed-out live
                // image. Preserve the Xbox combiner-domain values here; the
                // display color space still performs normal presentation.
                return format.format == VK_FORMAT_B8G8R8A8_UNORM
                    && format.colorSpace == VK_COLOR_SPACE_SRGB_NONLINEAR_KHR;
            });
        if (preferred != formats.end()) {
            return *preferred;
        }
        const auto srgb_fallback = std::find_if(
            formats.begin(),
            formats.end(),
            [](const VkSurfaceFormatKHR& format) {
                return format.format == VK_FORMAT_B8G8R8A8_SRGB
                    && format.colorSpace == VK_COLOR_SPACE_SRGB_NONLINEAR_KHR;
            });
        if (srgb_fallback != formats.end()) {
            return *srgb_fallback;
        }
        return formats.front();
    }

    VkExtent2D choose_extent(const VkSurfaceCapabilitiesKHR& capabilities) const {
        if (capabilities.currentExtent.width != UINT32_MAX) {
            return capabilities.currentExtent;
        }
        VkExtent2D extent{options_.width, options_.height};
        extent.width = std::clamp(
            extent.width,
            capabilities.minImageExtent.width,
            capabilities.maxImageExtent.width);
        extent.height = std::clamp(
            extent.height,
            capabilities.minImageExtent.height,
            capabilities.maxImageExtent.height);
        return extent;
    }

    void create_image_views() {
        swapchain_image_views_.resize(swapchain_images_.size());
        for (size_t index = 0; index < swapchain_images_.size(); ++index) {
            VkImageViewCreateInfo create_info{};
            create_info.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;
            create_info.image = swapchain_images_[index];
            create_info.viewType = VK_IMAGE_VIEW_TYPE_2D;
            create_info.format = swapchain_format_;
            create_info.components.r = VK_COMPONENT_SWIZZLE_IDENTITY;
            create_info.components.g = VK_COMPONENT_SWIZZLE_IDENTITY;
            create_info.components.b = VK_COMPONENT_SWIZZLE_IDENTITY;
            create_info.components.a = VK_COMPONENT_SWIZZLE_IDENTITY;
            create_info.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
            create_info.subresourceRange.baseMipLevel = 0;
            create_info.subresourceRange.levelCount = 1;
            create_info.subresourceRange.baseArrayLayer = 0;
            create_info.subresourceRange.layerCount = 1;
            vk_check(
                vkCreateImageView(device_, &create_info, nullptr, &swapchain_image_views_[index]),
                "vkCreateImageView");
        }
        log_.emit("image_views_created", {{"count", std::to_string(swapchain_image_views_.size())}});
    }

    void destroy_depth_attachment(
        VkImage& image,
        VkDeviceMemory& memory,
        VkImageView& view) {
        if (view != VK_NULL_HANDLE) {
            vkDestroyImageView(device_, view, nullptr);
            view = VK_NULL_HANDLE;
        }
        if (image != VK_NULL_HANDLE) {
            vkDestroyImage(device_, image, nullptr);
            image = VK_NULL_HANDLE;
        }
        if (memory != VK_NULL_HANDLE) {
            vkFreeMemory(device_, memory, nullptr);
            memory = VK_NULL_HANDLE;
        }
    }

    void create_depth_attachment(
        uint32_t width,
        uint32_t height,
        VkImage& image,
        VkDeviceMemory& memory,
        VkImageView& view) {
        try {
            VkImageCreateInfo image_info{};
            image_info.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO;
            image_info.imageType = VK_IMAGE_TYPE_2D;
            image_info.extent = {width, height, 1u};
            image_info.mipLevels = 1;
            image_info.arrayLayers = 1;
            image_info.format = depth_format_;
            image_info.tiling = VK_IMAGE_TILING_OPTIMAL;
            image_info.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
            image_info.usage = VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT;
            image_info.samples = VK_SAMPLE_COUNT_1_BIT;
            image_info.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
            vk_check(
                vkCreateImage(device_, &image_info, nullptr, &image),
                "vkCreateImage(depth attachment)");

            VkMemoryRequirements requirements{};
            vkGetImageMemoryRequirements(device_, image, &requirements);
            VkMemoryAllocateInfo allocation{};
            allocation.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;
            allocation.allocationSize = requirements.size;
            allocation.memoryTypeIndex = find_memory_type(
                requirements.memoryTypeBits,
                VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
            vk_check(
                vkAllocateMemory(device_, &allocation, nullptr, &memory),
                "vkAllocateMemory(depth attachment)");
            vk_check(
                vkBindImageMemory(device_, image, memory, 0),
                "vkBindImageMemory(depth attachment)");

            VkImageViewCreateInfo view_info{};
            view_info.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;
            view_info.image = image;
            view_info.viewType = VK_IMAGE_VIEW_TYPE_2D;
            view_info.format = depth_format_;
            view_info.subresourceRange.aspectMask = VK_IMAGE_ASPECT_DEPTH_BIT;
            view_info.subresourceRange.levelCount = 1;
            view_info.subresourceRange.layerCount = 1;
            vk_check(
                vkCreateImageView(device_, &view_info, nullptr, &view),
                "vkCreateImageView(depth attachment)");
        } catch (...) {
            destroy_depth_attachment(image, memory, view);
            throw;
        }
    }

    void create_depth_resources() {
        static constexpr std::array<VkFormat, 3> candidates = {
            VK_FORMAT_D32_SFLOAT,
            VK_FORMAT_D24_UNORM_S8_UINT,
            VK_FORMAT_D16_UNORM,
        };
        for (const VkFormat candidate : candidates) {
            VkFormatProperties properties{};
            vkGetPhysicalDeviceFormatProperties(
                physical_device_, candidate, &properties);
            if ((properties.optimalTilingFeatures
                 & VK_FORMAT_FEATURE_DEPTH_STENCIL_ATTACHMENT_BIT) != 0u) {
                depth_format_ = candidate;
                break;
            }
        }
        if (depth_format_ == VK_FORMAT_UNDEFINED) {
            throw std::runtime_error("no supported Vulkan depth format");
        }
        create_depth_attachment(
            swapchain_extent_.width,
            swapchain_extent_.height,
            depth_image_,
            depth_memory_,
            depth_image_view_);
        log_.emit(
            "depth_resources_created",
            {{"format", std::to_string(depth_format_)}});
    }

    void create_render_pass() {
        VkAttachmentDescription color_attachment{};
        color_attachment.format = swapchain_format_;
        color_attachment.samples = VK_SAMPLE_COUNT_1_BIT;
        color_attachment.loadOp = VK_ATTACHMENT_LOAD_OP_CLEAR;
        color_attachment.storeOp = VK_ATTACHMENT_STORE_OP_STORE;
        color_attachment.stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
        color_attachment.stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
        color_attachment.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
        color_attachment.finalLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;

        VkAttachmentDescription depth_attachment{};
        depth_attachment.format = depth_format_;
        depth_attachment.samples = VK_SAMPLE_COUNT_1_BIT;
        depth_attachment.loadOp = VK_ATTACHMENT_LOAD_OP_CLEAR;
        depth_attachment.storeOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
        depth_attachment.stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
        depth_attachment.stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
        depth_attachment.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
        depth_attachment.finalLayout =
            VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;

        VkAttachmentReference color_ref{};
        color_ref.attachment = 0;
        color_ref.layout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;

        VkAttachmentReference depth_ref{};
        depth_ref.attachment = 1;
        depth_ref.layout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;

        VkSubpassDescription subpass{};
        subpass.pipelineBindPoint = VK_PIPELINE_BIND_POINT_GRAPHICS;
        subpass.colorAttachmentCount = 1;
        subpass.pColorAttachments = &color_ref;
        subpass.pDepthStencilAttachment = &depth_ref;

        VkSubpassDependency dependency{};
        dependency.srcSubpass = VK_SUBPASS_EXTERNAL;
        dependency.dstSubpass = 0;
        dependency.srcStageMask = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT
            | VK_PIPELINE_STAGE_EARLY_FRAGMENT_TESTS_BIT
            | VK_PIPELINE_STAGE_LATE_FRAGMENT_TESTS_BIT;
        dependency.dstStageMask = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT
            | VK_PIPELINE_STAGE_EARLY_FRAGMENT_TESTS_BIT
            | VK_PIPELINE_STAGE_LATE_FRAGMENT_TESTS_BIT;
        dependency.srcAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT
            | VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT;
        dependency.dstAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT
            | VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT;

        const std::array<VkAttachmentDescription, 2> attachments = {
            color_attachment,
            depth_attachment,
        };

        VkRenderPassCreateInfo create_info{};
        create_info.sType = VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO;
        create_info.attachmentCount = static_cast<uint32_t>(attachments.size());
        create_info.pAttachments = attachments.data();
        create_info.subpassCount = 1;
        create_info.pSubpasses = &subpass;
        create_info.dependencyCount = 1;
        create_info.pDependencies = &dependency;
        vk_check(vkCreateRenderPass(device_, &create_info, nullptr, &render_pass_), "vkCreateRenderPass");
        log_.emit("render_pass_created");
    }

    void create_framebuffers() {
        framebuffers_.resize(swapchain_image_views_.size());
        for (size_t index = 0; index < swapchain_image_views_.size(); ++index) {
            VkImageView attachments[] = {
                swapchain_image_views_[index],
                depth_image_view_,
            };
            VkFramebufferCreateInfo create_info{};
            create_info.sType = VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO;
            create_info.renderPass = render_pass_;
            create_info.attachmentCount = 2;
            create_info.pAttachments = attachments;
            create_info.width = swapchain_extent_.width;
            create_info.height = swapchain_extent_.height;
            create_info.layers = 1;
            vk_check(
                vkCreateFramebuffer(device_, &create_info, nullptr, &framebuffers_[index]),
                "vkCreateFramebuffer");
        }
        log_.emit("framebuffers_created", {{"count", std::to_string(framebuffers_.size())}});
    }

    void create_command_pool() {
        VkCommandPoolCreateInfo create_info{};
        create_info.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO;
        create_info.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
        create_info.queueFamilyIndex = queue_family_.index;
        vk_check(vkCreateCommandPool(device_, &create_info, nullptr, &command_pool_), "vkCreateCommandPool");
        log_.emit("command_pool_created");
    }

    void create_gpu_timing_resources() {
        uint32_t family_count = 0u;
        vkGetPhysicalDeviceQueueFamilyProperties(
            physical_device_, &family_count, nullptr);
        std::vector<VkQueueFamilyProperties> families(family_count);
        vkGetPhysicalDeviceQueueFamilyProperties(
            physical_device_, &family_count, families.data());
        if (queue_family_.index >= families.size()
            || families[queue_family_.index].timestampValidBits == 0u
            || framebuffers_.empty()) {
            log_.emit("gpu_frame_timing_unavailable");
            return;
        }
        VkPhysicalDeviceProperties properties{};
        vkGetPhysicalDeviceProperties(physical_device_, &properties);
        gpu_timestamp_period_ns_ = properties.limits.timestampPeriod;
        gpu_timestamp_valid_bits_ =
            families[queue_family_.index].timestampValidBits;
        VkQueryPoolCreateInfo create_info{};
        create_info.sType = VK_STRUCTURE_TYPE_QUERY_POOL_CREATE_INFO;
        create_info.queryType = VK_QUERY_TYPE_TIMESTAMP;
        create_info.queryCount = static_cast<uint32_t>(framebuffers_.size()) * 2u;
        vk_check(
            vkCreateQueryPool(device_, &create_info, nullptr, &gpu_timing_query_pool_),
            "vkCreateQueryPool(frame timing)");
        log_.emit(
            "gpu_frame_timing_ready",
            {
                {"timestamp_period_ns", json_float(gpu_timestamp_period_ns_)},
                {"timestamp_valid_bits", std::to_string(
                    gpu_timestamp_valid_bits_)},
            });
    }

    uint32_t find_memory_type(uint32_t type_bits, VkMemoryPropertyFlags properties) const {
        VkPhysicalDeviceMemoryProperties memory_properties{};
        vkGetPhysicalDeviceMemoryProperties(physical_device_, &memory_properties);
        for (uint32_t index = 0; index < memory_properties.memoryTypeCount; ++index) {
            if ((type_bits & (1u << index)) != 0
                && (memory_properties.memoryTypes[index].propertyFlags & properties) == properties) {
                return index;
            }
        }
        throw std::runtime_error("no compatible Vulkan host memory type for frame readback");
    }

    void create_readback_buffer() {
        if (!readback_enabled()) {
            return;
        }
        if (swapchain_format_ != VK_FORMAT_B8G8R8A8_SRGB
            && swapchain_format_ != VK_FORMAT_B8G8R8A8_UNORM
            && swapchain_format_ != VK_FORMAT_R8G8B8A8_SRGB
            && swapchain_format_ != VK_FORMAT_R8G8B8A8_UNORM) {
            throw std::runtime_error("frame readback requires an 8-bit BGRA or RGBA swapchain format");
        }
        readback_size_ = static_cast<VkDeviceSize>(swapchain_extent_.width)
            * static_cast<VkDeviceSize>(swapchain_extent_.height) * 4u;
        VkBufferCreateInfo buffer_info{};
        buffer_info.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;
        buffer_info.size = readback_size_;
        buffer_info.usage = VK_BUFFER_USAGE_TRANSFER_DST_BIT;
        buffer_info.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
        vk_check(vkCreateBuffer(device_, &buffer_info, nullptr, &readback_buffer_), "vkCreateBuffer(readback)");

        VkMemoryRequirements requirements{};
        vkGetBufferMemoryRequirements(device_, readback_buffer_, &requirements);
        VkMemoryAllocateInfo allocate_info{};
        allocate_info.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;
        allocate_info.allocationSize = requirements.size;
        allocate_info.memoryTypeIndex = find_memory_type(
            requirements.memoryTypeBits,
            VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
        vk_check(vkAllocateMemory(device_, &allocate_info, nullptr, &readback_memory_), "vkAllocateMemory(readback)");
        vk_check(vkBindBufferMemory(device_, readback_buffer_, readback_memory_, 0), "vkBindBufferMemory(readback)");
        log_.emit(
            "frame_readback_ready",
            {
                {"bytes", std::to_string(readback_size_)},
                {"output", json_string(options_.screenshot.string())},
                {"hotkey_directory", json_string(options_.hotkey_screenshot_directory.string())},
            });
    }

    bool load_recovered_render_work(
        const std::function<void()>& source_resident_callback = {}) {
        const auto command_load_begin = std::chrono::steady_clock::now();
        last_resource_snapshot_reused_count_ = 0u;
        last_resource_snapshot_reused_bytes_ = 0u;
        auto after_manifest_read = command_load_begin;
        bool resources_unchanged = false;
        bool resource_generation_changed = false;
        bool resource_generation_declared = false;
        std::filesystem::path resource_source;
        std::filesystem::file_time_type resource_write_time{};
        std::string next_resource_generation = live_resource_generation_;
        std::string manifest_text;
        if (options_.live_render_stream && !options_.render_stream_json.empty()) {
            manifest_text = read_live_render_manifest().value_or(
                std::string{});
            after_manifest_read = std::chrono::steady_clock::now();
            const size_t final_non_space = manifest_text.find_last_not_of(" \t\r\n");
            if (final_non_space == std::string::npos
                || manifest_text[final_non_space] != '}') {
                return false;
            }
            if (!options_.flip_audit_ack.empty()) {
                const auto deadline = std::chrono::steady_clock::now()
                    + std::chrono::seconds(2);
                while (!update_flip_audit_manifest_state(manifest_text)) {
                    if (std::chrono::steady_clock::now() >= deadline) {
                        throw std::runtime_error(
                            "lossless flip audit manifest is missing required fields");
                    }
                    std::this_thread::sleep_for(std::chrono::milliseconds(2));
                    manifest_text = read_live_render_manifest().value_or(
                        std::string{});
                }
            }
            const auto guest_flip_field = json_object_field_text(
                manifest_text, "guest_flip_count");
            const auto guest_steps_field = json_object_field_text(
                manifest_text, "guest_steps");
            const auto guest_compiled_blocks_field = json_object_field_text(
                manifest_text, "guest_compiled_blocks");
            const auto guest_invalidations_field = json_object_field_text(
                manifest_text, "guest_invalidations");
            const auto presentable_command_field = json_object_field_text(
                manifest_text, "presentable_command_record_count");
            const auto published_command_field = json_object_field_text(
                manifest_text, "published_command_record_count");
            const auto command_base_field = json_object_field_text(
                manifest_text, "command_snapshot_base_record_count");
            const auto command_transport_field = json_object_field_text(
                manifest_text, "command_transport_format");
            const auto presentable_command_byte_field = json_object_field_text(
                manifest_text, "presentable_command_byte_count");
            const auto command_base_byte_field = json_object_field_text(
                manifest_text, "command_snapshot_base_byte_count");
            if (guest_flip_field.has_value()) {
                current_manifest_guest_flip_count_ = parse_json_u64_text(
                    *guest_flip_field, "guest flip count");
            }
            if (guest_steps_field.has_value()) {
                current_manifest_guest_steps_ = parse_json_u64_text(
                    *guest_steps_field, "guest steps");
            }
            if (guest_compiled_blocks_field.has_value()) {
                current_manifest_guest_compiled_blocks_ = parse_json_u64_text(
                    *guest_compiled_blocks_field, "guest compiled blocks");
            }
            if (guest_invalidations_field.has_value()) {
                current_manifest_guest_invalidations_ = parse_json_u64_text(
                    *guest_invalidations_field, "guest invalidations");
            }
            if (presentable_command_field.has_value()) {
                current_manifest_presentable_command_count_ = parse_json_u64_text(
                    *presentable_command_field, "presentable command record count");
            } else if (published_command_field.has_value()) {
                current_manifest_presentable_command_count_ = parse_json_u64_text(
                    *published_command_field, "published command record count");
            }
            current_manifest_command_base_count_ = command_base_field.has_value()
                ? parse_json_u64_text(
                    *command_base_field, "command snapshot base record count")
                : 0u;
            current_manifest_bulk_span_commands_ =
                command_transport_field.has_value()
                && *command_transport_field == "bulk_span_v1";
            current_manifest_presentable_command_byte_count_ =
                presentable_command_byte_field.has_value()
                ? parse_json_u64_text(
                    *presentable_command_byte_field,
                    "presentable command byte count")
                : 0u;
            current_manifest_command_base_byte_count_ =
                command_base_byte_field.has_value()
                ? parse_json_u64_text(
                    *command_base_byte_field,
                    "command snapshot base byte count")
                : 0u;
            if (current_manifest_bulk_span_commands_
                && (!presentable_command_byte_field.has_value()
                    || !command_base_byte_field.has_value()
                    || current_manifest_command_base_byte_count_
                        > current_manifest_presentable_command_byte_count_)) {
                throw std::runtime_error(
                    "bulk-span manifest has an invalid command byte boundary");
            }
            if (current_manifest_command_base_count_
                > current_manifest_presentable_command_count_) {
                throw std::runtime_error(
                    "live render command snapshot begins after its presentable boundary");
            }
            const auto resource_field = json_object_field_text(
                manifest_text, "resource_snapshot_path");
            const auto resource_generation_field = json_object_field_text(
                manifest_text, "resource_stream_generation");
            resource_generation_declared = resource_generation_field.has_value();
            resource_source = resource_field.has_value()
                ? std::filesystem::path(*resource_field)
                : options_.render_stream_json;
            if (resource_generation_field.has_value()) {
                next_resource_generation = *resource_generation_field;
                resources_unchanged = !live_resource_generation_.empty()
                    && next_resource_generation == live_resource_generation_
                    && resource_source == live_resource_source_;
                if (!live_resource_generation_.empty()
                    && next_resource_generation == live_resource_generation_
                    && !live_resource_source_.empty()
                    && resource_source != live_resource_source_) {
                    throw std::runtime_error(
                        "live resource generation changed snapshot paths");
                }
                resource_generation_changed = !resources_unchanged;
            } else {
                std::error_code resource_error;
                resource_write_time = std::filesystem::last_write_time(
                    resource_source, resource_error);
                if (!resource_error
                    && resource_source == recovered_source_.resource_source
                    && resource_write_time == live_resource_write_time_) {
                    resources_unchanged = true;
                } else if (!resource_error) {
                    resource_generation_changed = true;
                }
            }
        }
        const auto command_field = manifest_text.empty()
            ? std::optional<std::string>{}
            : json_object_field_text(manifest_text, "command_snapshot_path");
        const auto generation_field = manifest_text.empty()
            ? std::optional<std::string>{}
            : json_object_field_text(manifest_text, "command_stream_generation");
        const auto presentation_ack_field = manifest_text.empty()
            ? std::optional<std::string>{}
            : json_object_field_text(manifest_text, "presentation_ack_path");
        const auto presentation_event_field = manifest_text.empty()
            ? std::optional<std::string>{}
            : json_object_field_text(manifest_text, "presentation_event_name");
        const auto publication_event_field = manifest_text.empty()
            ? std::optional<std::string>{}
            : json_object_field_text(manifest_text, "publication_event_name");
        if (!manifest_text.empty()) {
            current_live_manifest_text_ = manifest_text;
            current_live_command_snapshot_path_ = command_field.has_value()
                ? std::filesystem::path(*command_field)
                : std::filesystem::path{};
            current_live_resource_snapshot_path_ = resource_source;
        } else {
            current_live_manifest_text_.clear();
            current_live_command_snapshot_path_.clear();
            current_live_resource_snapshot_path_.clear();
        }
        if (presentation_ack_field.has_value()) {
            const std::filesystem::path next_ack_path(*presentation_ack_field);
            if (next_ack_path != presentation_ack_path_) {
                if (presentation_ack_file_ != INVALID_HANDLE_VALUE) {
                    CloseHandle(presentation_ack_file_);
                    presentation_ack_file_ = INVALID_HANDLE_VALUE;
                }
                presentation_ack_path_ = next_ack_path;
                acknowledged_presentation_flip_ = 0u;
            }
        }
        if (presentation_event_field.has_value()
            && *presentation_event_field != presentation_event_name_) {
            if (presentation_ack_event_) {
                CloseHandle(presentation_ack_event_);
                presentation_ack_event_ = nullptr;
            }
            presentation_event_name_ = *presentation_event_field;
        }
        if (publication_event_field.has_value()
            && *publication_event_field != publication_event_name_) {
            publication_retry_pending_ = false;
            if (publication_event_) {
                CloseHandle(publication_event_);
                publication_event_ = nullptr;
            }
            publication_event_name_ = *publication_event_field;
            publication_event_ = OpenEventW(
                SYNCHRONIZE,
                FALSE,
                widen(publication_event_name_).c_str());
        }
        const bool command_generation_changed = options_.live_render_stream
            && generation_field.has_value()
            && *generation_field != live_command_generation_;
        const bool native_incremental_commands = options_.live_render_stream
            && command_field.has_value()
            && generation_field.has_value();
        if (options_.live_render_stream && !native_incremental_commands) {
            throw std::runtime_error(
                "live render manifest is missing its command snapshot identity");
        }
        uint64_t target_command_count =
            current_manifest_presentable_command_count_;
        const uint64_t target_command_byte_count =
            current_manifest_presentable_command_byte_count_;
        if (!options_.flip_audit_ack.empty()) {
            if (current_audit_command_count_ > target_command_count) {
                throw std::runtime_error(
                    "lossless flip audit exceeds the presentable command boundary");
            }
            target_command_count = current_audit_command_count_;
        }
        bool reset_interpreter = false;
        uint64_t command_read_begin = 0u;
        uint64_t command_read_begin_byte = 0u;
        if (native_incremental_commands) {
            reset_interpreter = command_generation_changed
                || interpreted_source_command_count_ > target_command_count
                || (current_manifest_bulk_span_commands_
                    && interpreted_source_command_byte_count_
                        > target_command_byte_count);
            if (reset_interpreter) {
                if (current_manifest_command_base_count_ != 0u
                    && !options_.analyze_render_stream_only) {
                    throw std::runtime_error(
                        "live render continuation snapshot cannot bootstrap a new presenter");
                }
                command_read_begin = current_manifest_command_base_count_;
                command_read_begin_byte =
                    current_manifest_command_base_byte_count_;
            } else if (interpreted_source_command_count_
                < current_manifest_command_base_count_) {
                if (!options_.analyze_render_stream_only) {
                    throw std::runtime_error(
                        "live render continuation snapshot skipped uninterpreted commands");
                }
                reset_interpreter = true;
                command_read_begin = current_manifest_command_base_count_;
                command_read_begin_byte =
                    current_manifest_command_base_byte_count_;
            } else {
                command_read_begin = interpreted_source_command_count_;
                command_read_begin_byte = interpreted_source_command_byte_count_;
            }
            if (command_read_begin > target_command_count) {
                throw std::runtime_error(
                    "live command cursor passed the presentable boundary");
            }
            if (current_manifest_bulk_span_commands_
                && command_read_begin_byte > target_command_byte_count) {
                throw std::runtime_error(
                    "live command byte cursor passed the presentable boundary");
            }
        }
        const auto before_source_load = std::chrono::steady_clock::now();
        if (native_incremental_commands) {
            const bool loaded_commands = current_manifest_bulk_span_commands_
                ? read_live_command_span_stream_delta(
                    std::filesystem::path(*command_field),
                    current_manifest_command_base_byte_count_,
                    command_read_begin_byte,
                    target_command_byte_count,
                    target_command_count - command_read_begin)
                : read_live_command_stream_delta(
                    std::filesystem::path(*command_field),
                    current_manifest_command_base_count_,
                    command_read_begin,
                    target_command_count);
            if (!loaded_commands) {
                return false;
            }
        }
        // Validate and retain the command delta before moving hash-identical
        // payloads out of the active resource snapshot. An incomplete sidecar
        // retry must leave the currently presented resources intact.
        RecoveredD3DStreamSource next_source = load_or_build_recovered_d3d_command_stream(
            options_.render_stream_json,
            !resources_unchanged,
            manifest_text.empty() ? nullptr : &manifest_text,
            !native_incremental_commands,
            options_.live_render_stream && !resources_unchanged,
            options_.live_render_stream && !resources_unchanged
                ? &recovered_source_.textures : nullptr,
            &last_resource_snapshot_reused_count_,
            &last_resource_snapshot_reused_bytes_);
        if (resources_unchanged) {
            next_source.textures = std::move(recovered_source_.textures);
        }
        const auto after_source_load = std::chrono::steady_clock::now();
        if (options_.live_render_stream) {
            live_command_generation_ = *generation_field;
            live_resource_generation_ = next_resource_generation;
            live_resource_source_ = resource_source;
            if (!resource_generation_declared) {
                live_resource_write_time_ = resource_write_time;
            }
        }
        next_source.resources_unchanged = resources_unchanged;
        recovered_source_ = std::move(next_source);
        last_resource_generation_changed_ = resource_generation_changed;
        last_command_cursor_reset_ = reset_interpreter;
        const auto after_command_load = std::chrono::steady_clock::now();
        if (source_resident_callback) {
            // The packed command delta and resource payloads are private to the
            // presenter now. A depth-two reload may release the producer before
            // interpreting either buffer; subsequent epoch rotation cannot
            // invalidate these resident copies.
            source_resident_callback();
        }
        const auto before_interpret = std::chrono::steady_clock::now();
        if (options_.live_render_stream) {
            if (reset_interpreter) {
                interpreted_stream_ = InterpretedD3DStream{};
                interpreted_stream_.state_seed = 0xB200D3D8u;
                interpreted_source_command_count_ = static_cast<size_t>(
                    current_manifest_command_base_count_);
                interpreted_source_command_byte_count_ =
                    current_manifest_command_base_byte_count_;
                continuation_analysis_bootstrap_ =
                    current_manifest_command_base_count_ != 0u;
                if (continuation_analysis_bootstrap_) {
                    log_.emit(
                        "render_stream_continuation_bootstrapped",
                        {
                            {"analysis_only", json_bool(true)},
                            {"state_history_complete", json_bool(false)},
                            {"command_snapshot_base_record_count", std::to_string(current_manifest_command_base_count_)},
                            {"presentable_command_record_count", std::to_string(target_command_count)},
                            {"resident_command_record_count", std::to_string(
                                target_command_count - current_manifest_command_base_count_)},
                        });
                }
            }
            const uint64_t expected_delta = target_command_count - command_read_begin;
            if (current_manifest_bulk_span_commands_) {
                const uint64_t interpreted_delta =
                    interpret_recovered_d3d_span_append(
                        live_command_delta_bytes_,
                        interpreted_stream_);
                if (interpreted_delta != expected_delta) {
                    throw std::runtime_error(
                        "bulk live command interpreter returned an incomplete delta");
                }
            } else {
                if (expected_delta
                    != live_command_delta_bytes_.size()
                        / kRecoveredD3DCommandRecordSize) {
                    throw std::runtime_error(
                        "native live command reader returned an incomplete delta");
                }
                interpret_recovered_d3d_packed_append(
                    live_command_delta_bytes_,
                    interpreted_stream_);
            }
            last_interpreted_command_delta_ = static_cast<size_t>(expected_delta);
            interpreted_source_command_count_ = static_cast<size_t>(
                target_command_count);
            interpreted_source_command_byte_count_ = target_command_byte_count;
        } else {
            interpreted_stream_ = interpret_recovered_d3d_stream(
                recovered_source_.commands,
                recovered_source_.interpreter_bootstrap_source);
            interpreted_source_command_count_ = recovered_source_.commands.size();
            last_interpreted_command_delta_ = interpreted_source_command_count_;
        }
        const auto after_method_interpret = std::chrono::steady_clock::now();
        materialize_indexed_draws(
            interpreted_stream_,
            recovered_source_.textures,
            gpu_raw_vertex_resource_cache_,
            !options_.cpu_vertex_programs
                && !options_.cpu_vertex_attributes
                && !options_.analyze_render_stream_only,
            recovered_source_.resources_unchanged);
        if (last_interpreted_command_delta_ != 0u
            || last_resource_generation_changed_) {
            ++render_work_generation_;
            presented_diagnostics_valid_ = false;
        }
        presented_surface_color_offset_ =
            select_presented_surface_color_offset();
        const auto after_interpret = std::chrono::steady_clock::now();
        last_command_load_us_ = std::chrono::duration_cast<std::chrono::microseconds>(
            after_command_load - command_load_begin).count();
        last_manifest_read_us_ = std::chrono::duration_cast<std::chrono::microseconds>(
            after_manifest_read - command_load_begin).count();
        last_manifest_parse_us_ = std::chrono::duration_cast<std::chrono::microseconds>(
            before_source_load - after_manifest_read).count();
        last_source_load_us_ = std::chrono::duration_cast<std::chrono::microseconds>(
            after_source_load - before_source_load).count();
        last_interpret_us_ = std::chrono::duration_cast<std::chrono::microseconds>(
            after_interpret - before_interpret).count();
        last_method_interpret_us_ = std::chrono::duration_cast<
            std::chrono::microseconds>(
                after_method_interpret - before_interpret).count();
        last_indexed_materialize_us_ = std::chrono::duration_cast<
            std::chrono::microseconds>(
                after_interpret - after_method_interpret).count();
        recovered_frontend_text_ = recovered_source_.frontend_text;
        if (options_.live_render_stream && !publication_event_) {
            std::error_code error;
            live_render_write_time_ = std::filesystem::last_write_time(
                options_.render_stream_json, error);
        }
        return true;
    }

    void destroy_native_render_resources(
        bool preserve_textures = false,
        bool preserve_vertex_buffer = false,
        bool preserve_offscreen_render_targets = false) {
        if (!preserve_offscreen_render_targets) {
            destroy_offscreen_render_targets();
        }
        if (!command_buffers_.empty()) {
            vkFreeCommandBuffers(
                device_, command_pool_, static_cast<uint32_t>(command_buffers_.size()),
                command_buffers_.data());
            command_buffers_.clear();
            command_buffer_draw_counts_.clear();
            command_buffer_triangle_counts_.clear();
            command_buffer_barrier_counts_.clear();
        }
        if (!preserve_vertex_buffer) {
            destroy_host_texture_bindings();
            if (vertex_mapped_) {
                vkUnmapMemory(device_, vertex_memory_);
                vertex_mapped_ = nullptr;
            }
            if (vertex_buffer_) {
                vkDestroyBuffer(device_, vertex_buffer_, nullptr);
                vertex_buffer_ = VK_NULL_HANDLE;
            }
            if (vertex_memory_) {
                vkFreeMemory(device_, vertex_memory_, nullptr);
                vertex_memory_ = VK_NULL_HANDLE;
            }
            vertex_buffer_size_ = 0;
        }
        if (!preserve_textures) {
            destroy_host_texture_bindings();
            for (const HostTexture& texture : host_textures_) {
                if (texture.view) vkDestroyImageView(device_, texture.view, nullptr);
                if (texture.image) vkDestroyImage(device_, texture.image, nullptr);
                if (texture.memory) vkFreeMemory(device_, texture.memory, nullptr);
            }
            host_textures_.clear();
            for (HostTexture& texture :
                 render_target_feedback_image_cache_) {
                destroy_host_texture(texture);
            }
            render_target_feedback_image_cache_.clear();
        }
        presented_half_quad_recovered_ = false;
        presented_overscan_height_recovered_ = false;
        presented_vertex_program_transformed_count_ = 0;
        presented_fixed_function_transformed_count_ = 0;
        presented_linear_texture_normalized_vertex_count_ = 0;
        offscreen_render_target_draw_count_ = 0;
        offscreen_render_target_transformed_vertex_count_ = 0;
        frontend_text_rectangle_count_ = 0;
        frontend_text_first_vertex_ = 0;
        frontend_text_vertex_count_ = 0;
        frontend_text_fragment_state_index_ =
            std::numeric_limits<uint32_t>::max();
    }

    void reload_live_render_work(bool publication_signaled = false) {
        if (!options_.live_render_stream) {
            return;
        }
        const auto probe_begin = std::chrono::steady_clock::now();
        if (publication_event_ && !publication_signaled) {
            if (publication_retry_pending_) {
                publication_signaled = true;
            } else {
                const DWORD result = WaitForSingleObject(publication_event_, 0u);
                if (result == WAIT_TIMEOUT) {
                    last_reload_probe_us_ += static_cast<uint64_t>(
                        std::chrono::duration_cast<std::chrono::microseconds>(
                            std::chrono::steady_clock::now() - probe_begin).count());
                    return;
                }
                if (result != WAIT_OBJECT_0) {
                    throw std::runtime_error(
                        "publication event probe failed");
                }
                publication_signaled = true;
            }
        }
        // The validated manifest is the publication boundary. Command/resource
        // sidecars are written first and may grow while a guest frame is still
        // under construction; consuming them directly would replay partial
        // work under a stale flip identity.
        if (!publication_signaled) {
            std::error_code error;
            const auto write_time = std::filesystem::last_write_time(
                options_.render_stream_json, error);
            if (error || write_time == live_render_write_time_) {
                last_reload_probe_us_ += static_cast<uint64_t>(
                    std::chrono::duration_cast<std::chrono::microseconds>(
                        std::chrono::steady_clock::now() - probe_begin).count());
                return;
            }
        }
        last_reload_probe_us_ += static_cast<uint64_t>(
            std::chrono::duration_cast<std::chrono::microseconds>(
                std::chrono::steady_clock::now() - probe_begin).count());
        const auto reload_begin = std::chrono::steady_clock::now();
        // Only one frame can be in flight. Waiting for its fence is sufficient
        // before replacing command buffers/resources and avoids draining the
        // entire device on every guest publication.
        wait_for_in_flight_fence("vkWaitForFences(live reload)");
        const auto after_wait = std::chrono::steady_clock::now();
        auto source_resident = after_wait;
        auto presentation_ack_begin = after_wait;
        auto presentation_ack_end = after_wait;
        bool presentation_ack_published = false;
        const auto source_resident_callback = [&]() {
            source_resident = std::chrono::steady_clock::now();
            if (options_.presentation_pipeline_depth == 2u) {
                // Both sidecars are fully loaded into presenter-owned memory.
                // Releasing the guest here overlaps method interpretation,
                // indexed materialization, and all Vulkan preparation with its
                // next flip while preserving one-future-publication ordering.
                presentation_ack_begin = source_resident;
                presentation_ack_published = acknowledge_current_presentation();
                presentation_ack_end = std::chrono::steady_clock::now();
            }
        };
        if (!load_recovered_render_work(source_resident_callback)) {
            publication_retry_pending_ = publication_event_ != nullptr;
            return;
        }
        publication_retry_pending_ = false;
        const auto after_load = std::chrono::steady_clock::now();
        if (last_interpreted_command_delta_ == 0u
            && recovered_source_.resources_unchanged
            && current_manifest_guest_flip_count_ != 0u
            && current_manifest_guest_flip_count_
                <= acknowledged_presentation_flip_) {
            ++live_render_skipped_reload_count_;
            log_.emit(
                "live_render_stream_reload_skipped",
                {
                    {"skipped_reload", std::to_string(live_render_skipped_reload_count_)},
                    {"manifest_guest_flip_count", std::to_string(current_manifest_guest_flip_count_)},
                    {"presentable_command_record_count", std::to_string(current_manifest_presentable_command_count_)},
                    {"reason", json_string("already_presented_without_command_or_resource_delta")},
                    {"load_interpret_us", std::to_string(std::chrono::duration_cast<std::chrono::microseconds>(after_load - after_wait).count())},
                });
            return;
        }
        // Keep existing images alive across a resource generation change so
        // refresh_host_textures can retain payload-identical textures and
        // upload only additions/replacements.
        const bool preserve_textures = !host_textures_.empty();
        const auto [presented_vertex_begin, presented_vertex_end] =
            presented_vertex_span(interpreted_stream_);
        const VkDeviceSize required_vertex_bytes =
            std::max<VkDeviceSize>(
                (presented_vertex_end - presented_vertex_begin)
                    * sizeof(NativeVertex),
                sizeof(NativeVertex));
        const bool preserve_vertex_buffer = vertex_buffer_ != VK_NULL_HANDLE
            && required_vertex_bytes <= vertex_buffer_size_;
        // Resource payload generations do not own GPU-produced feedback
        // attachments. Keep an offscreen framebuffer when the newly loaded
        // command stream requests the same target and its exact backing view
        // is still resident; refresh_host_textures verifies the view again
        // after applying the new generation.
        const bool preserve_offscreen_render_targets =
            preserve_textures
            && !offscreen_render_targets_.empty()
            && offscreen_render_targets_match_presented_specs();
        const auto resource_destroy_begin =
            std::chrono::steady_clock::now();
        destroy_native_render_resources(
            preserve_textures,
            preserve_vertex_buffer,
            preserve_offscreen_render_targets);
        const auto after_resource_destroy =
            std::chrono::steady_clock::now();
        create_native_render_resources(
            !recovered_source_.resources_unchanged,
            preserve_offscreen_render_targets);
        const auto after_native_resources =
            std::chrono::steady_clock::now();
        create_native_graphics_pipeline();
        const auto after_resources = std::chrono::steady_clock::now();
        const int64_t resource_preflight_us =
            std::chrono::duration_cast<std::chrono::microseconds>(
                resource_destroy_begin - after_load).count();
        const int64_t resource_destroy_us =
            std::chrono::duration_cast<std::chrono::microseconds>(
                after_resource_destroy - resource_destroy_begin).count();
        const int64_t native_resource_create_us =
            std::chrono::duration_cast<std::chrono::microseconds>(
                after_native_resources - after_resource_destroy).count();
        const int64_t pipeline_prepare_us =
            std::chrono::duration_cast<std::chrono::microseconds>(
                after_resources - after_native_resources).count();
        const int64_t resource_update_us =
            std::chrono::duration_cast<std::chrono::microseconds>(
                after_resources - after_load).count();
        const int64_t resource_prepare_us = std::max<int64_t>(
            resource_update_us - last_render_validation_us_,
            0);
        create_command_buffers();
        const auto after_commands = std::chrono::steady_clock::now();
        if (options_.presentation_pipeline_depth == 1u) {
            presentation_ack_begin = std::chrono::steady_clock::now();
            presentation_ack_published = acknowledge_current_presentation();
            presentation_ack_end = std::chrono::steady_clock::now();
        }
        ++live_render_reload_count_;
        log_.emit(
            "live_render_stream_reloaded",
            {
                {"reload", std::to_string(live_render_reload_count_)},
                {"writes", std::to_string(interpreted_source_command_count_)},
                {"guest_flips", std::to_string(interpreted_stream_.flip_count)},
                {"manifest_guest_flip_count", std::to_string(current_manifest_guest_flip_count_)},
                {"manifest_guest_steps", std::to_string(current_manifest_guest_steps_)},
                {"launch_transform_program_count", std::to_string(interpreted_stream_.launch_transform_program_count)},
                {"failed_launch_transform_program_count", std::to_string(interpreted_stream_.failed_launch_transform_program_count)},
                {"presentable_command_record_count", std::to_string(current_manifest_presentable_command_count_)},
                {"command_snapshot_base_record_count", std::to_string(current_manifest_command_base_count_)},
                {"resident_command_record_count", std::to_string(
                    current_manifest_presentable_command_count_
                        - current_manifest_command_base_count_)},
                {"native_command_records_read", std::to_string(
                    last_native_command_read_count_)},
                {"native_command_records_read_total", std::to_string(
                    native_command_read_count_)},
                {"native_command_spans_read", std::to_string(
                    last_native_command_span_count_)},
                {"native_command_spans_read_total", std::to_string(
                    native_command_span_read_count_)},
                {"command_transport", json_string(
                    current_manifest_bulk_span_commands_
                        ? "bulk_span_v1" : "packed_direct")},
                {"command_file_reused", json_bool(last_command_file_reused_)},
                {"command_read_bytes", std::to_string(last_command_read_bytes_)},
                {"command_file_open_us", std::to_string(
                    last_command_file_open_us_)},
                {"command_file_read_us", std::to_string(
                    last_command_file_read_us_)},
                {"command_record_validation_us", std::to_string(
                    last_command_record_validation_us_)},
                {"interpreted_source_commands", std::to_string(interpreted_source_command_count_)},
                {"interpreted_command_delta", std::to_string(last_interpreted_command_delta_)},
                {"pending_method_packet", json_bool(interpreted_stream_.pending_method_packet)},
                {"truncated_packets", std::to_string(interpreted_stream_.truncated_packet_count)},
                {"control_flow_packets", std::to_string(interpreted_stream_.control_flow_packet_count)},
                {"unknown_packets", std::to_string(interpreted_stream_.unknown_packet_count)},
                {"zero_count_indexed_array_noop_packets", std::to_string(interpreted_stream_.zero_count_indexed_array_noop_packet_count)},
                {"ordered_push_buffer_appends", std::to_string(
                    interpreted_stream_.ordered_push_buffer_append_count)},
                {"indexed_word_push_buffer_appends", std::to_string(
                    interpreted_stream_.indexed_word_push_buffer_append_count)},
                {"reconstructed_push_buffer_appends", std::to_string(
                    interpreted_stream_.reconstructed_push_buffer_append_count)},
                {"surface_payload_scans_skipped", std::to_string(
                    interpreted_stream_.surface_payload_scan_skipped_count)},
                {"exact_completed_flip", json_bool(
                    interpreted_source_command_count_
                        == current_manifest_presentable_command_count_
                    && interpreted_stream_.flip_count
                        == current_manifest_guest_flip_count_)},
                {"resources_unchanged", json_bool(recovered_source_.resources_unchanged)},
                {"resource_generation_changed", json_bool(
                    last_resource_generation_changed_)},
                {"resource_snapshot_reused_resources", std::to_string(
                    last_resource_snapshot_reused_count_)},
                {"resource_snapshot_reused_bytes", std::to_string(
                    last_resource_snapshot_reused_bytes_)},
                {"render_work_generation", std::to_string(
                    render_work_generation_)},
                {"presented_diagnostics_valid", json_bool(
                    presented_diagnostics_valid_
                    && presented_diagnostics_generation_
                        == render_work_generation_)},
                {"presented_diagnostics_sampled", json_bool(
                    last_presented_diagnostics_sampled_)},
                {"offscreen_render_targets_reused", json_bool(
                    last_offscreen_render_targets_reused_)},
                {"offscreen_render_target_count", std::to_string(
                    offscreen_render_targets_.size())},
                {"presentation_pipeline_depth", std::to_string(
                    options_.presentation_pipeline_depth)},
                {"presentation_ack_phase", json_string(
                    options_.presentation_pipeline_depth == 2u
                        ? "after_source_load"
                        : "after_command_record")},
                {"presentation_ack_published", json_bool(
                    presentation_ack_published)},
                {"presentation_ack_us", std::to_string(
                    std::chrono::duration_cast<std::chrono::microseconds>(
                        presentation_ack_end - presentation_ack_begin).count())},
                {"pre_ack_us", std::to_string(
                    std::chrono::duration_cast<std::chrono::microseconds>(
                        presentation_ack_end - reload_begin).count())},
                {"post_ack_us", std::to_string(
                    options_.presentation_pipeline_depth == 2u
                        ? std::chrono::duration_cast<std::chrono::microseconds>(
                            after_commands - presentation_ack_end).count()
                        : 0)},
                {"wait_us", std::to_string(std::chrono::duration_cast<std::chrono::microseconds>(after_wait - reload_begin).count())},
                {"source_residency_us", std::to_string(
                    std::chrono::duration_cast<std::chrono::microseconds>(
                        source_resident - after_wait).count())},
                {"load_interpret_us", std::to_string(std::chrono::duration_cast<std::chrono::microseconds>(after_load - after_wait).count())},
                {"command_load_us", std::to_string(last_command_load_us_)},
                {"manifest_read_us", std::to_string(last_manifest_read_us_)},
                {"manifest_parse_us", std::to_string(last_manifest_parse_us_)},
                {"source_load_us", std::to_string(last_source_load_us_)},
                {"interpret_us", std::to_string(last_interpret_us_)},
                {"method_interpret_us", std::to_string(last_method_interpret_us_)},
                {"push_buffer_collect_us", std::to_string(
                    interpreted_stream_.last_push_buffer_collect_us)},
                {"method_apply_us", std::to_string(
                    interpreted_stream_.last_method_apply_us)},
                {"method_finalize_us", std::to_string(
                    interpreted_stream_.last_method_finalize_us)},
                {"interpreted_method_delta", std::to_string(
                    interpreted_stream_.last_interpreted_method_count)},
                {"bulk_indexed_method_delta", std::to_string(
                    interpreted_stream_.last_bulk_indexed_method_count)},
                {"bulk_inline_method_delta", std::to_string(
                    interpreted_stream_.last_bulk_inline_method_count)},
                {"state_seed_updates_required", json_bool(
                    interpreted_stream_.state_seed_updates_required)},
                {"indexed_materialize_us", std::to_string(
                    last_indexed_materialize_us_)},
                {"resource_update_us", std::to_string(resource_update_us)},
                {"resource_prepare_us", std::to_string(resource_prepare_us)},
                {"resource_preflight_us", std::to_string(
                    resource_preflight_us)},
                {"resource_destroy_us", std::to_string(resource_destroy_us)},
                {"native_resource_create_us", std::to_string(
                    native_resource_create_us)},
                {"vertex_resource_prepare_us", std::to_string(
                    last_vertex_resource_prepare_us_)},
                {"state_resource_prepare_us", std::to_string(
                    last_state_resource_prepare_us_)},
                {"texture_resource_prepare_us", std::to_string(
                    last_texture_resource_prepare_us_)},
                {"texture_refresh_us", std::to_string(
                    last_texture_refresh_us_)},
                {"texture_indexed_lookup_count", std::to_string(
                    last_texture_indexed_lookup_count_)},
                {"texture_indexed_lookup_candidates", std::to_string(
                    last_texture_indexed_lookup_candidate_count_)},
                {"texture_constant_lookup_count", std::to_string(
                    last_texture_constant_lookup_count_)},
                {"render_target_feedback_image_cache_hits", std::to_string(
                    last_render_target_feedback_image_cache_hit_count_)},
                {"render_target_feedback_image_cache_misses", std::to_string(
                    last_render_target_feedback_image_cache_miss_count_)},
                {"render_target_feedback_image_cache_stores", std::to_string(
                    last_render_target_feedback_image_cache_store_count_)},
                {"render_target_feedback_image_cache_evictions", std::to_string(
                    last_render_target_feedback_image_cache_eviction_count_)},
                {"render_target_feedback_image_cache_resident", std::to_string(
                    render_target_feedback_image_cache_.size())},
                {"render_target_feedback_image_cache_capacity", std::to_string(
                    kRenderTargetFeedbackImageCacheCapacity)},
                {"offscreen_resource_prepare_us", std::to_string(
                    last_offscreen_resource_prepare_us_)},
                {"resource_bookkeeping_us", std::to_string(
                    last_resource_bookkeeping_us_)},
                {"pipeline_prepare_us", std::to_string(
                    pipeline_prepare_us)},
                {"pipeline_state_discovery_us", std::to_string(
                    last_pipeline_state_discovery_us_)},
                {"pipeline_candidate_draws", std::to_string(
                    last_pipeline_candidate_draw_count_)},
                {"pipeline_unique_states", std::to_string(
                    last_pipeline_unique_state_count_)},
                {"pipeline_missing_states", std::to_string(
                    last_pipeline_missing_state_count_)},
                {"feedback_spec_build_us", std::to_string(
                    last_feedback_spec_build_us_)},
                {"feedback_spec_cache_builds", std::to_string(
                    feedback_spec_cache_build_count_)},
                {"feedback_spec_cache_hits", std::to_string(
                    feedback_spec_cache_hit_count_)},
                {"texture_binding_update_us", std::to_string(
                    last_texture_binding_update_us_)},
                {"texture_binding_set_reused", json_bool(
                    last_texture_binding_set_reused_)},
                {"texture_binding_image_descriptor_updates", std::to_string(
                    last_texture_binding_image_descriptor_update_count_)},
                {"texture_binding_descriptor_sets_allocated", std::to_string(
                    last_texture_binding_descriptor_set_allocation_count_)},
                {"texture_binding_set_reuses", std::to_string(
                    texture_binding_set_reuse_count_)},
                {"texture_binding_set_rebuilds", std::to_string(
                    texture_binding_set_rebuild_count_)},
                {"texture_binding_image_descriptor_updates_total", std::to_string(
                    texture_binding_image_descriptor_update_count_)},
                {"texture_binding_descriptor_sets_allocated_total", std::to_string(
                    texture_binding_descriptor_set_allocation_count_)},
                {"render_validation_us", std::to_string(
                    last_render_validation_us_)},
                {"vertex_transform_us", std::to_string(last_vertex_transform_us_)},
                {"vertex_state_upload_us", std::to_string(
                    last_vertex_state_upload_us_)},
                {"raw_vertex_upload_us", std::to_string(
                    last_raw_vertex_upload_us_)},
                {"gpu_raw_attribute_draws", std::to_string(
                    gpu_raw_attribute_draw_count_)},
                {"gpu_raw_attribute_vertices", std::to_string(
                    gpu_raw_attribute_vertex_count_)},
                {"expanded_vertex_bytes_avoided", std::to_string(
                    static_cast<uint64_t>(
                        gpu_raw_attribute_vertex_count_)
                        * sizeof(NativeVertex))},
                {"gpu_vertex_program_draws", std::to_string(
                    gpu_vertex_program_draw_count_)},
                {"gpu_vertex_program_vertices", std::to_string(
                    gpu_vertex_program_vertex_count_)},
                {"cpu_vertex_program_fallback_draws", std::to_string(
                    cpu_vertex_program_fallback_draw_count_)},
                {"cpu_vertex_program_fallback_vertices", std::to_string(
                    cpu_vertex_program_fallback_vertex_count_)},
                {"vertex_map_us", std::to_string(last_vertex_map_us_)},
                {"vertex_copy_us", std::to_string(last_vertex_copy_us_)},
                {"command_record_us", std::to_string(std::chrono::duration_cast<std::chrono::microseconds>(after_commands - after_resources).count())},
                {"total_us", std::to_string(std::chrono::duration_cast<std::chrono::microseconds>(after_commands - reload_begin).count())},
            });
    }

    std::vector<uint32_t> read_spirv(const std::filesystem::path& path) const {
        std::ifstream file(path, std::ios::binary | std::ios::ate);
        if (!file) {
            throw std::runtime_error("cannot open SPIR-V shader: " + path.string());
        }
        const std::streamsize size = file.tellg();
        if (size <= 0 || size % 4 != 0) {
            throw std::runtime_error("invalid SPIR-V shader size: " + path.string());
        }
        file.seekg(0);
        std::vector<uint32_t> code(static_cast<size_t>(size) / 4u);
        file.read(reinterpret_cast<char*>(code.data()), size);
        return code;
    }

    VkShaderModule create_shader_module(const std::filesystem::path& path) const {
        const std::vector<uint32_t> code = read_spirv(path);
        VkShaderModuleCreateInfo create_info{};
        create_info.sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO;
        create_info.codeSize = code.size() * sizeof(uint32_t);
        create_info.pCode = code.data();
        VkShaderModule module = VK_NULL_HANDLE;
        vk_check(vkCreateShaderModule(device_, &create_info, nullptr, &module), "vkCreateShaderModule");
        return module;
    }

    bool ensure_texture_conversion_pipeline() {
        if (options_.cpu_texture_conversion
            || !queue_family_.supports_compute
            || options_.texture_convert_shader.empty()) {
            return false;
        }
        if (texture_convert_pipeline_ != VK_NULL_HANDLE) {
            return true;
        }
        std::array<VkDescriptorSetLayoutBinding, 2> bindings{};
        for (uint32_t index = 0u; index < bindings.size(); ++index) {
            bindings[index].binding = index;
            bindings[index].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
            bindings[index].descriptorCount = 1u;
            bindings[index].stageFlags = VK_SHADER_STAGE_COMPUTE_BIT;
        }
        VkDescriptorSetLayoutCreateInfo descriptor_info{};
        descriptor_info.sType =
            VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO;
        descriptor_info.bindingCount = static_cast<uint32_t>(bindings.size());
        descriptor_info.pBindings = bindings.data();
        vk_check(
            vkCreateDescriptorSetLayout(
                device_,
                &descriptor_info,
                nullptr,
                &texture_convert_descriptor_layout_),
            "vkCreateDescriptorSetLayout(texture conversion)");

        VkPushConstantRange push_constants{};
        push_constants.stageFlags = VK_SHADER_STAGE_COMPUTE_BIT;
        push_constants.size = sizeof(NativeTextureConvertPushConstants);
        VkPipelineLayoutCreateInfo layout_info{};
        layout_info.sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO;
        layout_info.setLayoutCount = 1u;
        layout_info.pSetLayouts = &texture_convert_descriptor_layout_;
        layout_info.pushConstantRangeCount = 1u;
        layout_info.pPushConstantRanges = &push_constants;
        vk_check(
            vkCreatePipelineLayout(
                device_,
                &layout_info,
                nullptr,
                &texture_convert_pipeline_layout_),
            "vkCreatePipelineLayout(texture conversion)");

        const auto shader_module_begin = std::chrono::steady_clock::now();
        const VkShaderModule shader = create_shader_module(
            options_.texture_convert_shader);
        const auto shader_module_end = std::chrono::steady_clock::now();
        VkPipelineShaderStageCreateInfo stage{};
        stage.sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;
        stage.stage = VK_SHADER_STAGE_COMPUTE_BIT;
        stage.module = shader;
        stage.pName = "main";
        VkComputePipelineCreateInfo pipeline_info{};
        pipeline_info.sType = VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO;
        pipeline_info.stage = stage;
        pipeline_info.layout = texture_convert_pipeline_layout_;
        const auto pipeline_create_begin = std::chrono::steady_clock::now();
        const VkResult create_result = vkCreateComputePipelines(
            device_,
            pipeline_cache_,
            1u,
            &pipeline_info,
            nullptr,
            &texture_convert_pipeline_);
        const auto pipeline_create_end = std::chrono::steady_clock::now();
        vkDestroyShaderModule(device_, shader, nullptr);
        vk_check(create_result, "vkCreateComputePipelines(texture conversion)");
        ++pipeline_creation_count_;
        ++pipeline_cache_miss_count_;
        log_.emit(
            "nv2a_gpu_texture_conversion_pipeline_created",
            {
                {"queue_family", std::to_string(queue_family_.index)},
                {"shader", json_string(
                    options_.texture_convert_shader.string())},
                {"shader_module_us", std::to_string(
                    std::chrono::duration_cast<std::chrono::microseconds>(
                        shader_module_end - shader_module_begin).count())},
                {"pipeline_create_us", std::to_string(
                    std::chrono::duration_cast<std::chrono::microseconds>(
                        pipeline_create_end - pipeline_create_begin).count())},
            });
        return true;
    }

    void create_native_graphics_pipeline() {
        last_pipeline_state_discovery_us_ = 0u;
        last_pipeline_candidate_draw_count_ = 0u;
        last_pipeline_unique_state_count_ = 0u;
        last_pipeline_missing_state_count_ = 0u;
        if (!texture_descriptor_layout_) {
            std::array<VkDescriptorSetLayoutBinding, 5> bindings{};
            bindings[0].binding = 0;
            bindings[0].descriptorType =
                VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
            bindings[0].descriptorCount = 1;
            bindings[0].stageFlags = VK_SHADER_STAGE_FRAGMENT_BIT;
            bindings[1].binding = 1;
            bindings[1].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
            bindings[1].descriptorCount = 1;
            bindings[1].stageFlags = VK_SHADER_STAGE_FRAGMENT_BIT;
            bindings[2].binding = 2;
            bindings[2].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
            bindings[2].descriptorCount = 1;
            bindings[2].stageFlags = VK_SHADER_STAGE_VERTEX_BIT;
            bindings[3].binding = 3;
            bindings[3].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
            bindings[3].descriptorCount = 1;
            bindings[3].stageFlags = VK_SHADER_STAGE_VERTEX_BIT;
            bindings[4].binding = 4;
            bindings[4].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
            bindings[4].descriptorCount = 1;
            bindings[4].stageFlags = VK_SHADER_STAGE_VERTEX_BIT;
            VkDescriptorSetLayoutCreateInfo descriptor_info{};
            descriptor_info.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO;
            descriptor_info.bindingCount = static_cast<uint32_t>(
                bindings.size());
            descriptor_info.pBindings = bindings.data();
            vk_check(
                vkCreateDescriptorSetLayout(device_, &descriptor_info, nullptr, &texture_descriptor_layout_),
                "vkCreateDescriptorSetLayout");

            VkPipelineLayoutCreateInfo layout_info{};
            layout_info.sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO;
            layout_info.setLayoutCount = 1;
            layout_info.pSetLayouts = &texture_descriptor_layout_;
            VkPushConstantRange push_constant_range{};
            push_constant_range.stageFlags =
                VK_SHADER_STAGE_VERTEX_BIT | VK_SHADER_STAGE_FRAGMENT_BIT;
            push_constant_range.size = sizeof(NativeFragmentPushConstants);
            layout_info.pushConstantRangeCount = 1;
            layout_info.pPushConstantRanges = &push_constant_range;
            vk_check(vkCreatePipelineLayout(device_, &layout_info, nullptr, &pipeline_layout_), "vkCreatePipelineLayout");
        }

        const auto discovery_begin = std::chrono::steady_clock::now();
        std::vector<NativePipelineState> states;
        std::unordered_set<NativePipelineState, NativePipelineStateHash>
            unique_states;
        const size_t first_draw = std::min<size_t>(interpreted_stream_.presented_draw_begin, interpreted_stream_.draws.size());
        const size_t end_draw = std::min<size_t>(first_draw + interpreted_stream_.presented_draw_count, interpreted_stream_.draws.size());
        states.reserve(end_draw - first_draw + 1u);
        unique_states.reserve((end_draw - first_draw) * 2u + 1u);
        const auto append_unique_state = [&](NativePipelineState state) {
            if (unique_states.insert(state).second) {
                states.push_back(std::move(state));
            }
        };
        const std::vector<RenderTargetFeedbackSpec>& feedback_specs =
            presented_render_target_feedback_specs();
        for (size_t draw_index = first_draw; draw_index < end_draw; ++draw_index) {
            const size_t presented_index = draw_index - first_draw;
            if (presented_index < options_.presented_draw_begin
                || presented_index >= options_.presented_draw_end) {
                continue;
            }
            const NativeDraw& draw = interpreted_stream_.draws[draw_index];
            const bool targets_offscreen_feedback = std::any_of(
                feedback_specs.begin(),
                feedback_specs.end(),
                [&](const RenderTargetFeedbackSpec& spec) {
                    return spec.offscreen_produced
                        && nv2a_canonical_resource_address(
                            spec.producer_address)
                            == nv2a_canonical_resource_address(
                                draw.surface_color_offset);
                });
            if ((!draw_targets_presented_surface(draw)
                    && !targets_offscreen_feedback)
                || !draw_has_supported_host_transform(draw)
                || (draw.primitive != 5u && draw.primitive != 6u)) {
                continue;
            }
            ++last_pipeline_candidate_draw_count_;
            append_unique_state(pipeline_state_for_draw(draw));
        }
        if (!recovered_frontend_text_.empty()) {
            append_unique_state(frontend_text_pipeline_state());
        }
        if (states.empty()) {
            append_unique_state({});
        }
        last_pipeline_unique_state_count_ = static_cast<uint32_t>(
            states.size());
        std::unordered_set<NativePipelineState, NativePipelineStateHash>
            resident_states;
        resident_states.reserve(graphics_pipelines_.size() * 2u + 1u);
        for (const HostPipeline& pipeline : graphics_pipelines_) {
            resident_states.insert(pipeline.state);
        }
        states.erase(
            std::remove_if(
                states.begin(), states.end(),
                [&](const NativePipelineState& state) {
                    return resident_states.find(state)
                        != resident_states.end();
                }),
            states.end());
        last_pipeline_missing_state_count_ = static_cast<uint32_t>(
            states.size());
        last_pipeline_state_discovery_us_ = std::chrono::duration_cast<
            std::chrono::microseconds>(
                std::chrono::steady_clock::now() - discovery_begin).count();
        if (states.empty()) {
            log_.emit(
                "nv2a_graphics_pipeline_cache_hit",
                {
                    {"pipeline_count", std::to_string(graphics_pipelines_.size())},
                    {"candidate_draws", std::to_string(
                        last_pipeline_candidate_draw_count_)},
                    {"unique_states", std::to_string(
                        last_pipeline_unique_state_count_)},
                    {"state_discovery_us", std::to_string(
                        last_pipeline_state_discovery_us_)},
                });
            return;
        }

        const auto shader_module_begin = std::chrono::steady_clock::now();
        const VkShaderModule vertex_shader = create_shader_module(options_.vertex_shader);
        const VkShaderModule fragment_shader = create_shader_module(options_.fragment_shader);
        const auto shader_module_end = std::chrono::steady_clock::now();
        VkPipelineShaderStageCreateInfo stages[2]{};
        stages[0].sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;
        stages[0].stage = VK_SHADER_STAGE_VERTEX_BIT;
        stages[0].module = vertex_shader;
        stages[0].pName = "main";
        stages[1].sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;
        stages[1].stage = VK_SHADER_STAGE_FRAGMENT_BIT;
        stages[1].module = fragment_shader;
        stages[1].pName = "main";

        VkVertexInputBindingDescription binding{};
        binding.binding = 0;
        binding.stride = sizeof(NativeVertex);
        binding.inputRate = VK_VERTEX_INPUT_RATE_VERTEX;
        std::array<VkVertexInputAttributeDescription, 5> attributes{};
        attributes[0] = {0, 0, VK_FORMAT_R32G32B32A32_SFLOAT, static_cast<uint32_t>(offsetof(NativeVertex, x))};
        attributes[1] = {1, 0, VK_FORMAT_R32G32B32A32_SFLOAT, static_cast<uint32_t>(offsetof(NativeVertex, r))};
        attributes[2] = {2, 0, VK_FORMAT_R32G32B32A32_SFLOAT, static_cast<uint32_t>(offsetof(NativeVertex, u))};
        attributes[3] = {3, 0, VK_FORMAT_R32G32B32A32_SFLOAT, static_cast<uint32_t>(offsetof(NativeVertex, secondary_r))};
        attributes[4] = {4, 0, VK_FORMAT_R32_SFLOAT, static_cast<uint32_t>(offsetof(NativeVertex, fog))};
        VkPipelineVertexInputStateCreateInfo vertex_input{};
        vertex_input.sType = VK_STRUCTURE_TYPE_PIPELINE_VERTEX_INPUT_STATE_CREATE_INFO;
        vertex_input.vertexBindingDescriptionCount = 1;
        vertex_input.pVertexBindingDescriptions = &binding;
        vertex_input.vertexAttributeDescriptionCount = static_cast<uint32_t>(attributes.size());
        vertex_input.pVertexAttributeDescriptions = attributes.data();

        VkPipelineInputAssemblyStateCreateInfo assembly{};
        assembly.sType = VK_STRUCTURE_TYPE_PIPELINE_INPUT_ASSEMBLY_STATE_CREATE_INFO;
        assembly.topology = VK_PRIMITIVE_TOPOLOGY_TRIANGLE_STRIP;
        VkViewport viewport{0.0f, 0.0f, static_cast<float>(swapchain_extent_.width), static_cast<float>(swapchain_extent_.height), 0.0f, 1.0f};
        VkRect2D scissor{{0, 0}, swapchain_extent_};
        VkPipelineViewportStateCreateInfo viewport_state{};
        viewport_state.sType = VK_STRUCTURE_TYPE_PIPELINE_VIEWPORT_STATE_CREATE_INFO;
        viewport_state.viewportCount = 1;
        viewport_state.pViewports = &viewport;
        viewport_state.scissorCount = 1;
        viewport_state.pScissors = &scissor;
        constexpr std::array<VkDynamicState, 2> dynamic_states = {
            VK_DYNAMIC_STATE_VIEWPORT,
            VK_DYNAMIC_STATE_SCISSOR,
        };
        VkPipelineDynamicStateCreateInfo dynamic_state{};
        dynamic_state.sType = VK_STRUCTURE_TYPE_PIPELINE_DYNAMIC_STATE_CREATE_INFO;
        dynamic_state.dynamicStateCount = static_cast<uint32_t>(
            dynamic_states.size());
        dynamic_state.pDynamicStates = dynamic_states.data();
        VkPipelineRasterizationStateCreateInfo raster{};
        raster.sType = VK_STRUCTURE_TYPE_PIPELINE_RASTERIZATION_STATE_CREATE_INFO;
        raster.polygonMode = VK_POLYGON_MODE_FILL;
        raster.cullMode = VK_CULL_MODE_NONE;
        raster.frontFace = VK_FRONT_FACE_CLOCKWISE;
        raster.lineWidth = 1.0f;
        VkPipelineMultisampleStateCreateInfo multisample{};
        multisample.sType = VK_STRUCTURE_TYPE_PIPELINE_MULTISAMPLE_STATE_CREATE_INFO;
        multisample.rasterizationSamples = VK_SAMPLE_COUNT_1_BIT;
        VkPipelineDepthStencilStateCreateInfo depth_stencil{};
        depth_stencil.sType =
            VK_STRUCTURE_TYPE_PIPELINE_DEPTH_STENCIL_STATE_CREATE_INFO;
        depth_stencil.depthBoundsTestEnable = VK_FALSE;
        depth_stencil.stencilTestEnable = VK_FALSE;
        VkPipelineColorBlendAttachmentState blend_attachment{};
        VkPipelineColorBlendStateCreateInfo blend{};
        blend.sType = VK_STRUCTURE_TYPE_PIPELINE_COLOR_BLEND_STATE_CREATE_INFO;
        blend.attachmentCount = 1;
        blend.pAttachments = &blend_attachment;
        VkGraphicsPipelineCreateInfo pipeline_info{};
        pipeline_info.sType = VK_STRUCTURE_TYPE_GRAPHICS_PIPELINE_CREATE_INFO;
        pipeline_info.stageCount = 2;
        pipeline_info.pStages = stages;
        pipeline_info.pVertexInputState = &vertex_input;
        pipeline_info.pInputAssemblyState = &assembly;
        pipeline_info.pViewportState = &viewport_state;
        pipeline_info.pRasterizationState = &raster;
        pipeline_info.pMultisampleState = &multisample;
        pipeline_info.pDepthStencilState = &depth_stencil;
        pipeline_info.pColorBlendState = &blend;
        pipeline_info.pDynamicState = &dynamic_state;
        pipeline_info.layout = pipeline_layout_;
        pipeline_info.renderPass = render_pass_;
        pipeline_info.subpass = 0;
        const size_t pipeline_count = states.size();
        std::vector<VkPipelineVertexInputStateCreateInfo> vertex_inputs(
            pipeline_count, vertex_input);
        std::vector<VkPipelineInputAssemblyStateCreateInfo> assemblies(
            pipeline_count, assembly);
        std::vector<VkPipelineRasterizationStateCreateInfo> rasters(
            pipeline_count, raster);
        std::vector<VkPipelineDepthStencilStateCreateInfo> depth_stencils(
            pipeline_count, depth_stencil);
        std::vector<VkPipelineColorBlendAttachmentState> blend_attachments(
            pipeline_count, blend_attachment);
        std::vector<VkPipelineColorBlendStateCreateInfo> blends(
            pipeline_count, blend);
        std::vector<VkGraphicsPipelineCreateInfo> pipeline_infos(
            pipeline_count, pipeline_info);
        std::vector<VkPipeline> pipelines(
            pipeline_count, VK_NULL_HANDLE);
        for (size_t index = 0; index < pipeline_count; ++index) {
            const NativePipelineState& state = states[index];
            VkPipelineVertexInputStateCreateInfo& state_vertex_input =
                vertex_inputs[index];
            state_vertex_input.vertexBindingDescriptionCount =
                state.raw_attribute_fetch ? 0u : 1u;
            state_vertex_input.vertexAttributeDescriptionCount =
                state.raw_attribute_fetch
                ? 0u
                : static_cast<uint32_t>(attributes.size());
            assemblies[index].topology = state.primitive == 5u
                ? VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST
                : VK_PRIMITIVE_TOPOLOGY_TRIANGLE_STRIP;
            VkPipelineColorBlendAttachmentState& state_blend =
                blend_attachments[index];
            state_blend.blendEnable = state.blend_enable ? VK_TRUE : VK_FALSE;
            state_blend.srcColorBlendFactor = nv2a_blend_factor(
                state.blend_source_factor,
                VK_BLEND_FACTOR_ONE);
            state_blend.dstColorBlendFactor = nv2a_blend_factor(
                state.blend_destination_factor,
                VK_BLEND_FACTOR_ZERO);
            state_blend.colorBlendOp = nv2a_blend_op(state.blend_equation);
            state_blend.srcAlphaBlendFactor = state_blend.srcColorBlendFactor;
            state_blend.dstAlphaBlendFactor = state_blend.dstColorBlendFactor;
            state_blend.alphaBlendOp = state_blend.colorBlendOp;
            state_blend.colorWriteMask = nv2a_color_write_mask(state.color_mask);
            depth_stencils[index].depthTestEnable =
                state.depth_test_enable ? VK_TRUE : VK_FALSE;
            depth_stencils[index].depthWriteEnable =
                state.depth_write_enable ? VK_TRUE : VK_FALSE;
            depth_stencils[index].depthCompareOp =
                nv2a_depth_compare_op(state.depth_function);
            rasters[index].cullMode =
                nv2a_cull_mode(state.cull_face_enable, state.cull_face);
            rasters[index].frontFace = nv2a_front_face(state.front_face);
            blends[index].pAttachments = &blend_attachments[index];
            pipeline_infos[index].pVertexInputState = &vertex_inputs[index];
            pipeline_infos[index].pInputAssemblyState = &assemblies[index];
            pipeline_infos[index].pRasterizationState = &rasters[index];
            pipeline_infos[index].pDepthStencilState = &depth_stencils[index];
            pipeline_infos[index].pColorBlendState = &blends[index];
        }
        const auto pipeline_create_begin = std::chrono::steady_clock::now();
        const VkResult create_result = vkCreateGraphicsPipelines(
            device_,
            pipeline_cache_,
            static_cast<uint32_t>(pipeline_infos.size()),
            pipeline_infos.data(),
            nullptr,
            pipelines.data());
        const auto pipeline_create_end = std::chrono::steady_clock::now();
        if (create_result != VK_SUCCESS) {
            for (VkPipeline pipeline : pipelines) {
                if (pipeline != VK_NULL_HANDLE) {
                    vkDestroyPipeline(device_, pipeline, nullptr);
                }
            }
            vkDestroyShaderModule(device_, fragment_shader, nullptr);
            vkDestroyShaderModule(device_, vertex_shader, nullptr);
            vk_check(create_result, "vkCreateGraphicsPipelines");
        }
        for (size_t index = 0; index < pipeline_count; ++index) {
            graphics_pipelines_.push_back({states[index], pipelines[index]});
        }
        pipeline_creation_count_ += pipeline_count;
        pipeline_cache_miss_count_ += pipeline_count;
        vkDestroyShaderModule(device_, fragment_shader, nullptr);
        vkDestroyShaderModule(device_, vertex_shader, nullptr);
        log_.emit(
            "nv2a_graphics_pipeline_created",
            {
                {"created_count", std::to_string(states.size())},
                {"pipeline_count", std::to_string(graphics_pipelines_.size())},
                {"candidate_draws", std::to_string(
                    last_pipeline_candidate_draw_count_)},
                {"unique_states", std::to_string(
                    last_pipeline_unique_state_count_)},
                {"state_discovery_us", std::to_string(
                    last_pipeline_state_discovery_us_)},
                {"shader_module_us", std::to_string(
                    std::chrono::duration_cast<std::chrono::microseconds>(
                        shader_module_end - shader_module_begin).count())},
                {"pipeline_create_us", std::to_string(
                    std::chrono::duration_cast<std::chrono::microseconds>(
                        pipeline_create_end - pipeline_create_begin).count())},
                {"batched", json_bool(true)},
            });
    }

    void create_buffer(
        VkDeviceSize size,
        VkBufferUsageFlags usage,
        VkMemoryPropertyFlags properties,
        VkBuffer& buffer,
        VkDeviceMemory& memory) {
        VkBufferCreateInfo buffer_info{};
        buffer_info.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;
        buffer_info.size = size;
        buffer_info.usage = usage;
        buffer_info.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
        vk_check(vkCreateBuffer(device_, &buffer_info, nullptr, &buffer), "vkCreateBuffer(native)");
        VkMemoryRequirements requirements{};
        vkGetBufferMemoryRequirements(device_, buffer, &requirements);
        VkMemoryAllocateInfo allocation{};
        allocation.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;
        allocation.allocationSize = requirements.size;
        allocation.memoryTypeIndex = find_memory_type(requirements.memoryTypeBits, properties);
        vk_check(vkAllocateMemory(device_, &allocation, nullptr, &memory), "vkAllocateMemory(native)");
        vk_check(vkBindBufferMemory(device_, buffer, memory, 0), "vkBindBufferMemory(native)");
    }

    void refresh_raw_vertex_buffers() {
        const auto upload_begin = std::chrono::steady_clock::now();
        const std::vector<uint8_t>& resource_bytes =
            gpu_raw_vertex_resource_cache_.gpu_raw_vertex_bytes;
        const VkDeviceSize index_offset =
            resource_bytes.size();
        const VkDeviceSize required_size = std::max<VkDeviceSize>(
            index_offset
                + interpreted_stream_.gpu_raw_vertex_indices.size()
                    * sizeof(uint32_t),
            4u);
        const VkDeviceSize minimum_live_size = options_.live_render_stream
            ? index_offset + 1024u * 1024u
            : required_size;
        bool buffer_replaced = false;
        if (raw_vertex_resource_buffer_ == VK_NULL_HANDLE
            || required_size > raw_vertex_resource_buffer_size_) {
            destroy_host_texture_bindings();
            if (raw_vertex_resource_mapped_ != nullptr) {
                vkUnmapMemory(device_, raw_vertex_resource_memory_);
                raw_vertex_resource_mapped_ = nullptr;
            }
            if (raw_vertex_resource_buffer_ != VK_NULL_HANDLE) {
                vkDestroyBuffer(
                    device_, raw_vertex_resource_buffer_, nullptr);
            }
            if (raw_vertex_resource_memory_ != VK_NULL_HANDLE) {
                vkFreeMemory(
                    device_, raw_vertex_resource_memory_, nullptr);
            }
            raw_vertex_resource_buffer_size_ = std::max(
                required_size, minimum_live_size);
            create_buffer(
                raw_vertex_resource_buffer_size_,
                VK_BUFFER_USAGE_STORAGE_BUFFER_BIT,
                VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT
                    | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT,
                raw_vertex_resource_buffer_,
                raw_vertex_resource_memory_);
            vk_check(
                vkMapMemory(
                    device_,
                    raw_vertex_resource_memory_,
                    0,
                    raw_vertex_resource_buffer_size_,
                    0,
                    &raw_vertex_resource_mapped_),
                "vkMapMemory(raw vertex resources)");
            buffer_replaced = true;
        }
        const uint32_t pending_dirty_range_count = static_cast<uint32_t>(
            gpu_raw_vertex_resource_cache_.dirty_ranges.size());
        uint64_t resource_upload_bytes = 0u;
        uint32_t resource_upload_range_count = 0u;
        if (buffer_replaced && !resource_bytes.empty()) {
            std::memcpy(
                raw_vertex_resource_mapped_,
                resource_bytes.data(),
                resource_bytes.size());
            resource_upload_bytes = resource_bytes.size();
            resource_upload_range_count = 1u;
        } else if (!buffer_replaced) {
            for (const GpuRawVertexDirtyRange& range :
                 gpu_raw_vertex_resource_cache_.dirty_ranges) {
                std::memcpy(
                    static_cast<uint8_t*>(raw_vertex_resource_mapped_)
                        + range.offset,
                    resource_bytes.data() + range.offset,
                    range.size);
                resource_upload_bytes += range.size;
                ++resource_upload_range_count;
            }
        }
        gpu_raw_vertex_resource_cache_.dirty_ranges.clear();
        if (interpreted_stream_.gpu_raw_vertex_indices.empty()) {
            if (required_size == 4u) {
                std::memset(raw_vertex_resource_mapped_, 0, 4u);
            }
        } else {
            std::memcpy(
                static_cast<uint8_t*>(raw_vertex_resource_mapped_)
                    + index_offset,
                interpreted_stream_.gpu_raw_vertex_indices.data(),
                interpreted_stream_.gpu_raw_vertex_indices.size()
                    * sizeof(uint32_t));
        }
        const uint64_t index_upload_bytes =
            interpreted_stream_.gpu_raw_vertex_indices.size()
                * sizeof(uint32_t);
        upload_bytes_ += resource_upload_bytes + index_upload_bytes;
        raw_vertex_resource_upload_bytes_ += resource_upload_bytes;
        raw_vertex_index_upload_bytes_ += index_upload_bytes;
        last_raw_vertex_upload_us_ = std::chrono::duration_cast<
            std::chrono::microseconds>(
                std::chrono::steady_clock::now() - upload_begin).count();
        log_.emit(
            "nv2a_raw_vertex_buffers_refreshed",
            {
                {"reload", std::to_string(live_render_reload_count_ + 1u)},
                {"manifest_guest_flip_count", std::to_string(
                    current_manifest_guest_flip_count_)},
                {"resources_unchanged", json_bool(
                    recovered_source_.resources_unchanged)},
                {"resource_bytes", std::to_string(
                    resource_bytes.size())},
                {"source_indices", std::to_string(
                    interpreted_stream_.gpu_raw_vertex_indices.size())},
                {"gpu_draws", std::to_string(
                    interpreted_stream_.gpu_raw_attribute_draw_count)},
                {"gpu_vertices", std::to_string(
                    interpreted_stream_.gpu_raw_attribute_vertex_count)},
                {"buffer_replaced", json_bool(buffer_replaced)},
                {"layout_rebuilt", json_bool(
                    gpu_raw_vertex_resource_cache_.layout_rebuilt)},
                {"cache_refreshes", std::to_string(
                    gpu_raw_vertex_resource_cache_.refresh_count)},
                {"layout_rebuilds", std::to_string(
                    gpu_raw_vertex_resource_cache_.layout_rebuild_count)},
                {"resource_count", std::to_string(
                    gpu_raw_vertex_resource_cache_.resource_count)},
                {"compared_resources", std::to_string(
                    gpu_raw_vertex_resource_cache_.compared_resource_count)},
                {"changed_resources", std::to_string(
                    gpu_raw_vertex_resource_cache_.changed_resource_count)},
                {"reused_resources", std::to_string(
                    gpu_raw_vertex_resource_cache_.reused_resource_count)},
                {"compared_bytes", std::to_string(
                    gpu_raw_vertex_resource_cache_.compared_bytes)},
                {"pending_dirty_ranges", std::to_string(
                    pending_dirty_range_count)},
                {"dirty_bytes", std::to_string(
                    gpu_raw_vertex_resource_cache_.dirty_bytes)},
                {"cache_refresh_us", std::to_string(
                    gpu_raw_vertex_resource_cache_.refresh_us)},
                {"resource_upload_ranges", std::to_string(
                    resource_upload_range_count)},
                {"resource_upload_bytes", std::to_string(
                    resource_upload_bytes)},
                {"index_upload_bytes", std::to_string(index_upload_bytes)},
                {"cumulative_resource_upload_bytes", std::to_string(
                    raw_vertex_resource_upload_bytes_)},
                {"cumulative_index_upload_bytes", std::to_string(
                    raw_vertex_index_upload_bytes_)},
                {"resource_bytes_uploaded", json_bool(
                    resource_upload_bytes != 0u)},
                {"upload_us", std::to_string(last_raw_vertex_upload_us_)},
            });
    }

    void refresh_fragment_states() {
        const size_t first_draw = std::min<size_t>(
            interpreted_stream_.presented_draw_begin,
            interpreted_stream_.draws.size());
        const size_t end_draw = std::min<size_t>(
            first_draw + interpreted_stream_.presented_draw_count,
            interpreted_stream_.draws.size());
        std::vector<NativeFragmentState> states;
        states.reserve(std::max<size_t>(end_draw - first_draw, 1u));
        for (size_t draw_index = first_draw;
             draw_index < end_draw;
             ++draw_index) {
            states.push_back(fragment_state_for_draw(
                interpreted_stream_.draws[draw_index]));
        }
        frontend_text_fragment_state_index_ = std::numeric_limits<uint32_t>::max();
        if (!recovered_frontend_text_.empty()) {
            frontend_text_fragment_state_index_ = static_cast<uint32_t>(
                states.size());
            states.push_back(frontend_text_fragment_state());
        }
        if (states.empty()) {
            states.emplace_back();
        }

        const VkDeviceSize required_size = states.size()
            * sizeof(NativeFragmentState);
        bool buffer_replaced = false;
        if (fragment_state_buffer_ == VK_NULL_HANDLE
            || required_size > fragment_state_buffer_size_) {
            destroy_host_texture_bindings();
            if (fragment_state_mapped_ != nullptr) {
                vkUnmapMemory(device_, fragment_state_memory_);
                fragment_state_mapped_ = nullptr;
            }
            if (fragment_state_buffer_ != VK_NULL_HANDLE) {
                vkDestroyBuffer(device_, fragment_state_buffer_, nullptr);
                fragment_state_buffer_ = VK_NULL_HANDLE;
            }
            if (fragment_state_memory_ != VK_NULL_HANDLE) {
                vkFreeMemory(device_, fragment_state_memory_, nullptr);
                fragment_state_memory_ = VK_NULL_HANDLE;
            }
            const VkDeviceSize minimum_live_size = options_.live_render_stream
                ? 4096u * sizeof(NativeFragmentState)
                : required_size;
            fragment_state_buffer_size_ = std::max(
                required_size, minimum_live_size);
            create_buffer(
                fragment_state_buffer_size_,
                VK_BUFFER_USAGE_STORAGE_BUFFER_BIT,
                VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT
                    | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT,
                fragment_state_buffer_,
                fragment_state_memory_);
            vk_check(
                vkMapMemory(
                    device_,
                    fragment_state_memory_,
                    0,
                    fragment_state_buffer_size_,
                    0,
                    &fragment_state_mapped_),
                "vkMapMemory(fragment states)");
            buffer_replaced = true;
        }
        std::memcpy(
            fragment_state_mapped_,
            states.data(),
            static_cast<size_t>(required_size));
        upload_bytes_ += static_cast<uint64_t>(required_size);
        fragment_state_count_ = static_cast<uint32_t>(states.size());
        log_.emit(
            "nv2a_fragment_states_refreshed",
            {
                {"states", std::to_string(fragment_state_count_)},
                {"state_bytes", std::to_string(required_size)},
                {"buffer_bytes", std::to_string(fragment_state_buffer_size_)},
                {"buffer_replaced", json_bool(buffer_replaced)},
            });
    }

    void refresh_vertex_program_states() {
        const auto upload_begin = std::chrono::steady_clock::now();
        const size_t first_draw = std::min<size_t>(
            interpreted_stream_.presented_draw_begin,
            interpreted_stream_.draws.size());
        const size_t end_draw = std::min<size_t>(
            first_draw + interpreted_stream_.presented_draw_count,
            interpreted_stream_.draws.size());
        const std::vector<RenderTargetFeedbackSpec>& feedback_specs =
            presented_render_target_feedback_specs();
        const bool enable_gpu_programs = !options_.cpu_vertex_programs
            && !options_.analyze_render_stream_only;
        std::vector<NativeVertexProgramState> states;
        states.reserve(std::max<size_t>(end_draw - first_draw, 1u));
        gpu_vertex_program_draw_count_ = 0u;
        gpu_vertex_program_vertex_count_ = 0u;
        gpu_raw_attribute_draw_count_ = 0u;
        gpu_raw_attribute_vertex_count_ = 0u;
        cpu_vertex_program_fallback_draw_count_ = 0u;
        cpu_vertex_program_fallback_vertex_count_ = 0u;
        for (size_t draw_index = first_draw;
             draw_index < end_draw;
             ++draw_index) {
            const NativeDraw& draw = interpreted_stream_.draws[draw_index];
            VkExtent2D target_extent = swapchain_extent_;
            if (!draw_targets_presented_surface(draw)) {
                const auto target = std::find_if(
                    feedback_specs.begin(),
                    feedback_specs.end(),
                    [&](const RenderTargetFeedbackSpec& spec) {
                        return spec.offscreen_produced
                            && nv2a_canonical_resource_address(
                                spec.producer_address)
                                == nv2a_canonical_resource_address(
                                    draw.surface_color_offset);
                    });
                if (target != feedback_specs.end()) {
                    target_extent = {target->width, target->height};
                }
            }
            NativeVertexProgramState state = vertex_program_state_for_draw(
                draw,
                target_extent,
                enable_gpu_programs);
            if ((draw.transform_execution_mode & 3u) == 2u) {
                if (state.enabled != 0u) {
                    ++gpu_vertex_program_draw_count_;
                    gpu_vertex_program_vertex_count_ += draw.vertex_count;
                    if (draw.gpu_raw_attribute_fetch) {
                        ++gpu_raw_attribute_draw_count_;
                        gpu_raw_attribute_vertex_count_ += draw.vertex_count;
                    }
                } else {
                    ++cpu_vertex_program_fallback_draw_count_;
                    cpu_vertex_program_fallback_vertex_count_ +=
                        draw.vertex_count;
                }
            }
            states.push_back(std::move(state));
        }
        if (!recovered_frontend_text_.empty()) {
            states.emplace_back();
        }
        if (states.empty()) {
            states.emplace_back();
        }

        const VkDeviceSize required_size = states.size()
            * sizeof(NativeVertexProgramState);
        bool buffer_replaced = false;
        if (vertex_program_state_buffer_ == VK_NULL_HANDLE
            || required_size > vertex_program_state_buffer_size_) {
            destroy_host_texture_bindings();
            if (vertex_program_state_mapped_ != nullptr) {
                vkUnmapMemory(device_, vertex_program_state_memory_);
                vertex_program_state_mapped_ = nullptr;
            }
            if (vertex_program_state_buffer_ != VK_NULL_HANDLE) {
                vkDestroyBuffer(
                    device_, vertex_program_state_buffer_, nullptr);
                vertex_program_state_buffer_ = VK_NULL_HANDLE;
            }
            if (vertex_program_state_memory_ != VK_NULL_HANDLE) {
                vkFreeMemory(
                    device_, vertex_program_state_memory_, nullptr);
                vertex_program_state_memory_ = VK_NULL_HANDLE;
            }
            const VkDeviceSize minimum_live_size = options_.live_render_stream
                ? 1024u * sizeof(NativeVertexProgramState)
                : required_size;
            vertex_program_state_buffer_size_ = std::max(
                required_size,
                minimum_live_size);
            create_buffer(
                vertex_program_state_buffer_size_,
                VK_BUFFER_USAGE_STORAGE_BUFFER_BIT,
                VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT
                    | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT,
                vertex_program_state_buffer_,
                vertex_program_state_memory_);
            vk_check(
                vkMapMemory(
                    device_,
                    vertex_program_state_memory_,
                    0,
                    vertex_program_state_buffer_size_,
                    0,
                    &vertex_program_state_mapped_),
                "vkMapMemory(vertex program states)");
            buffer_replaced = true;
        }
        std::memcpy(
            vertex_program_state_mapped_,
            states.data(),
            static_cast<size_t>(required_size));
        upload_bytes_ += static_cast<uint64_t>(required_size);
        vertex_program_state_count_ = static_cast<uint32_t>(states.size());
        last_vertex_state_upload_us_ = std::chrono::duration_cast<
            std::chrono::microseconds>(
                std::chrono::steady_clock::now() - upload_begin).count();
        log_.emit(
            "nv2a_vertex_program_states_refreshed",
            {
                {"states", std::to_string(vertex_program_state_count_)},
                {"state_bytes", std::to_string(required_size)},
                {"buffer_bytes", std::to_string(
                    vertex_program_state_buffer_size_)},
                {"buffer_replaced", json_bool(buffer_replaced)},
                {"gpu_draws", std::to_string(
                    gpu_vertex_program_draw_count_)},
                {"gpu_vertices", std::to_string(
                    gpu_vertex_program_vertex_count_)},
                {"gpu_raw_attribute_draws", std::to_string(
                    gpu_raw_attribute_draw_count_)},
                {"gpu_raw_attribute_vertices", std::to_string(
                    gpu_raw_attribute_vertex_count_)},
                {"cpu_fallback_draws", std::to_string(
                    cpu_vertex_program_fallback_draw_count_)},
                {"cpu_fallback_vertices", std::to_string(
                    cpu_vertex_program_fallback_vertex_count_)},
                {"upload_us", std::to_string(last_vertex_state_upload_us_)},
            });
    }

    std::vector<uint8_t> decompress_dxt1(const RecoveredTextureResource& resource) const {
        std::vector<uint8_t> rgba(static_cast<size_t>(resource.width) * resource.height * 4u, 255u);
        const uint32_t blocks_x = (resource.width + 3u) / 4u;
        const uint32_t blocks_y = (resource.height + 3u) / 4u;
        auto expand565 = [](uint16_t value) {
            return std::array<uint8_t, 3>{
                static_cast<uint8_t>(((value >> 11u) & 31u) * 255u / 31u),
                static_cast<uint8_t>(((value >> 5u) & 63u) * 255u / 63u),
                static_cast<uint8_t>((value & 31u) * 255u / 31u),
            };
        };
        for (uint32_t block_index = 0; block_index < blocks_x * blocks_y; ++block_index) {
            const size_t offset = static_cast<size_t>(block_index) * 8u;
            if (offset + 8u > resource.payload.size()) {
                break;
            }
            // The guest upload has already converted these DXT1 surfaces to
            // row-major block order. Applying an NV2A Morton unswizzle here
            // scrambles the recovered title and publisher textures.
            const uint32_t block_x = block_index % blocks_x;
            const uint32_t block_y = block_index / blocks_x;
            const uint16_t color0 = static_cast<uint16_t>(resource.payload[offset] | (resource.payload[offset + 1u] << 8u));
            const uint16_t color1 = static_cast<uint16_t>(resource.payload[offset + 2u] | (resource.payload[offset + 3u] << 8u));
            std::array<std::array<uint8_t, 4>, 4> colors{};
            const auto rgb0 = expand565(color0);
            const auto rgb1 = expand565(color1);
            colors[0] = {rgb0[0], rgb0[1], rgb0[2], 255};
            colors[1] = {rgb1[0], rgb1[1], rgb1[2], 255};
            if (color0 > color1) {
                for (uint32_t channel = 0; channel < 3u; ++channel) {
                    colors[2][channel] = static_cast<uint8_t>((2u * colors[0][channel] + colors[1][channel]) / 3u);
                    colors[3][channel] = static_cast<uint8_t>((colors[0][channel] + 2u * colors[1][channel]) / 3u);
                }
                colors[2][3] = colors[3][3] = 255;
            } else {
                for (uint32_t channel = 0; channel < 3u; ++channel) {
                    colors[2][channel] = static_cast<uint8_t>((colors[0][channel] + colors[1][channel]) / 2u);
                }
                colors[2][3] = 255;
                colors[3] = {0, 0, 0, 0};
            }
            uint32_t indices = 0;
            std::memcpy(&indices, resource.payload.data() + offset + 4u, sizeof(indices));
            for (uint32_t y = 0; y < 4u; ++y) {
                for (uint32_t x = 0; x < 4u; ++x) {
                    const uint32_t pixel_x = block_x * 4u + x;
                    const uint32_t pixel_y = block_y * 4u + y;
                    if (pixel_x >= resource.width || pixel_y >= resource.height) {
                        continue;
                    }
                    const auto& color = colors[(indices >> (2u * (y * 4u + x))) & 3u];
                    const size_t destination = (static_cast<size_t>(pixel_y) * resource.width + pixel_x) * 4u;
                    std::copy(color.begin(), color.end(), rgba.begin() + destination);
                }
            }
        }
        return rgba;
    }

    std::vector<uint8_t> decompress_dxt5(const RecoveredTextureResource& resource) const {
        std::vector<uint8_t> rgba(static_cast<size_t>(resource.width) * resource.height * 4u, 255u);
        const uint32_t blocks_x = (resource.width + 3u) / 4u;
        const uint32_t blocks_y = (resource.height + 3u) / 4u;
        auto expand565 = [](uint16_t value) {
            return std::array<uint8_t, 3>{
                static_cast<uint8_t>(((value >> 11u) & 31u) * 255u / 31u),
                static_cast<uint8_t>(((value >> 5u) & 63u) * 255u / 63u),
                static_cast<uint8_t>((value & 31u) * 255u / 31u),
            };
        };
        for (uint32_t block_index = 0; block_index < blocks_x * blocks_y; ++block_index) {
            const size_t offset = static_cast<size_t>(block_index) * 16u;
            if (offset + 16u > resource.payload.size()) {
                break;
            }
            const uint32_t block_x = block_index % blocks_x;
            const uint32_t block_y = block_index / blocks_x;

            std::array<uint8_t, 8> alphas{};
            alphas[0] = resource.payload[offset];
            alphas[1] = resource.payload[offset + 1u];
            if (alphas[0] > alphas[1]) {
                for (uint32_t index = 1u; index <= 6u; ++index) {
                    alphas[index + 1u] = static_cast<uint8_t>(
                        ((7u - index) * alphas[0] + index * alphas[1]) / 7u);
                }
            } else {
                for (uint32_t index = 1u; index <= 4u; ++index) {
                    alphas[index + 1u] = static_cast<uint8_t>(
                        ((5u - index) * alphas[0] + index * alphas[1]) / 5u);
                }
                alphas[6] = 0u;
                alphas[7] = 255u;
            }
            uint64_t alpha_indices = 0u;
            for (uint32_t byte_index = 0; byte_index < 6u; ++byte_index) {
                alpha_indices |= static_cast<uint64_t>(resource.payload[offset + 2u + byte_index])
                    << (byte_index * 8u);
            }

            const size_t color_offset = offset + 8u;
            const uint16_t color0 = static_cast<uint16_t>(
                resource.payload[color_offset] | (resource.payload[color_offset + 1u] << 8u));
            const uint16_t color1 = static_cast<uint16_t>(
                resource.payload[color_offset + 2u] | (resource.payload[color_offset + 3u] << 8u));
            const auto rgb0 = expand565(color0);
            const auto rgb1 = expand565(color1);
            std::array<std::array<uint8_t, 3>, 4> colors{};
            colors[0] = rgb0;
            colors[1] = rgb1;
            for (uint32_t channel = 0; channel < 3u; ++channel) {
                colors[2][channel] = static_cast<uint8_t>((2u * rgb0[channel] + rgb1[channel]) / 3u);
                colors[3][channel] = static_cast<uint8_t>((rgb0[channel] + 2u * rgb1[channel]) / 3u);
            }
            uint32_t color_indices = 0u;
            std::memcpy(
                &color_indices,
                resource.payload.data() + color_offset + 4u,
                sizeof(color_indices));

            for (uint32_t y = 0; y < 4u; ++y) {
                for (uint32_t x = 0; x < 4u; ++x) {
                    const uint32_t pixel_index = y * 4u + x;
                    const uint32_t pixel_x = block_x * 4u + x;
                    const uint32_t pixel_y = block_y * 4u + y;
                    if (pixel_x >= resource.width || pixel_y >= resource.height) {
                        continue;
                    }
                    const auto& color = colors[(color_indices >> (2u * pixel_index)) & 3u];
                    const uint8_t alpha = alphas[(alpha_indices >> (3u * pixel_index)) & 7u];
                    const size_t destination =
                        (static_cast<size_t>(pixel_y) * resource.width + pixel_x) * 4u;
                    rgba[destination] = color[0];
                    rgba[destination + 1u] = color[1];
                    rgba[destination + 2u] = color[2];
                    rgba[destination + 3u] = alpha;
                }
            }
        }
        return rgba;
    }

    std::vector<std::vector<uint8_t>> decompress_dxt_mip_chain(
        const RecoveredTextureResource& resource) const {
        const uint32_t block_bytes = resource.format == "DXT1" ? 8u : 16u;
        std::vector<std::vector<uint8_t>> mips;
        size_t payload_offset = 0u;
        uint32_t width = resource.width;
        uint32_t height = resource.height;
        while (width != 0u && height != 0u) {
            const size_t payload_size = static_cast<size_t>((width + 3u) / 4u)
                * ((height + 3u) / 4u) * block_bytes;
            if (payload_offset + payload_size > resource.payload.size()) {
                break;
            }
            RecoveredTextureResource level = resource;
            level.width = width;
            level.height = height;
            level.payload.assign(
                resource.payload.begin()
                    + static_cast<std::ptrdiff_t>(payload_offset),
                resource.payload.begin()
                    + static_cast<std::ptrdiff_t>(
                        payload_offset + payload_size));
            mips.push_back(resource.format == "DXT1"
                ? decompress_dxt1(level)
                : decompress_dxt5(level));
            payload_offset += payload_size;
            if (width == 1u && height == 1u) {
                break;
            }
            width = std::max(width / 2u, 1u);
            height = std::max(height / 2u, 1u);
        }
        return mips;
    }

    std::vector<std::vector<uint8_t>> build_cpu_dxt_conversion_mips(
        const RecoveredTextureResource& resource) const {
        std::vector<std::vector<uint8_t>> mips =
            decompress_dxt_mip_chain(resource);
        if (mips.size() != 1u) {
            return mips;
        }
        uint32_t width = resource.width;
        uint32_t height = resource.height;
        while (width != 1u || height != 1u) {
            const std::vector<uint8_t>& source = mips.back();
            const uint32_t next_width = std::max(width / 2u, 1u);
            const uint32_t next_height = std::max(height / 2u, 1u);
            std::vector<uint8_t> next(
                static_cast<size_t>(next_width) * next_height * 4u,
                0u);
            for (uint32_t y = 0u; y < next_height; ++y) {
                for (uint32_t x = 0u; x < next_width; ++x) {
                    for (uint32_t channel = 0u; channel < 4u; ++channel) {
                        uint32_t sum = 0u;
                        uint32_t count = 0u;
                        for (uint32_t dy = 0u; dy < 2u; ++dy) {
                            for (uint32_t dx = 0u; dx < 2u; ++dx) {
                                const uint32_t source_x = x * 2u + dx;
                                const uint32_t source_y = y * 2u + dy;
                                if (source_x >= width || source_y >= height) {
                                    continue;
                                }
                                const size_t source_offset =
                                    (static_cast<size_t>(source_y) * width
                                        + source_x) * 4u + channel;
                                sum += source[source_offset];
                                ++count;
                            }
                        }
                        const size_t destination =
                            (static_cast<size_t>(y) * next_width + x) * 4u
                            + channel;
                        next[destination] = static_cast<uint8_t>(
                            sum / std::max(count, 1u));
                    }
                }
            }
            mips.push_back(std::move(next));
            width = next_width;
            height = next_height;
        }
        return mips;
    }

    std::vector<uint8_t> unswizzle_texture_2d(
        const RecoveredTextureResource& resource,
        uint32_t bytes_per_pixel) const {
        uint32_t mask_x = 0u;
        uint32_t mask_y = 0u;
        uint32_t dimension_bit = 1u;
        uint32_t mask_bit = 1u;
        bool done = false;
        while (!done) {
            done = true;
            if (dimension_bit < resource.width) {
                mask_x |= mask_bit;
                mask_bit <<= 1u;
                done = false;
            }
            if (dimension_bit < resource.height) {
                mask_y |= mask_bit;
                mask_bit <<= 1u;
                done = false;
            }
            dimension_bit <<= 1u;
        }
        std::vector<uint8_t> linear(resource.payload.size(), 0u);
        uint32_t offset_y = 0u;
        for (uint32_t y = 0u; y < resource.height; ++y) {
            uint32_t offset_x = 0u;
            for (uint32_t x = 0u; x < resource.width; ++x) {
                const size_t source = static_cast<size_t>(
                    offset_x + offset_y) * bytes_per_pixel;
                const size_t destination =
                    (static_cast<size_t>(y) * resource.width + x)
                    * bytes_per_pixel;
                if (source + bytes_per_pixel <= resource.payload.size()
                    && destination + bytes_per_pixel <= linear.size()) {
                    std::copy_n(
                        resource.payload.begin()
                            + static_cast<std::ptrdiff_t>(source),
                        bytes_per_pixel,
                        linear.begin()
                            + static_cast<std::ptrdiff_t>(destination));
                }
                offset_x = (offset_x - mask_x) & mask_x;
            }
            offset_y = (offset_y - mask_y) & mask_y;
        }
        return linear;
    }

    std::vector<uint8_t> convert_bgra8_texture(
        const RecoveredTextureResource& resource,
        bool opaque_alpha,
        bool swizzled) const {
        const size_t pixel_count = static_cast<size_t>(resource.width)
            * resource.height;
        const std::vector<uint8_t> linear = swizzled
            ? unswizzle_texture_2d(resource, 4u)
            : std::vector<uint8_t>{};
        const std::vector<uint8_t>& source = swizzled
            ? linear
            : resource.payload;
        std::vector<uint8_t> rgba(pixel_count * 4u, 0u);
        for (size_t pixel = 0; pixel < pixel_count; ++pixel) {
            const size_t offset = pixel * 4u;
            rgba[offset] = source[offset + 2u];
            rgba[offset + 1u] = source[offset + 1u];
            rgba[offset + 2u] = source[offset];
            rgba[offset + 3u] = opaque_alpha
                ? 255u
                : source[offset + 3u];
        }
        return rgba;
    }

    std::vector<uint8_t> convert_r5g6b5_texture(
        const RecoveredTextureResource& resource) const {
        const size_t pixel_count = static_cast<size_t>(resource.width)
            * resource.height;
        const std::vector<uint8_t> linear =
            unswizzle_texture_2d(resource, 2u);
        std::vector<uint8_t> rgba(pixel_count * 4u, 255u);
        for (size_t pixel = 0; pixel < pixel_count; ++pixel) {
            uint16_t packed = 0u;
            std::memcpy(
                &packed,
                linear.data() + pixel * 2u,
                sizeof(packed));
            const size_t offset = pixel * 4u;
            rgba[offset] = static_cast<uint8_t>(
                ((packed >> 11u) & 31u) * 255u / 31u);
            rgba[offset + 1u] = static_cast<uint8_t>(
                ((packed >> 5u) & 63u) * 255u / 63u);
            rgba[offset + 2u] = static_cast<uint8_t>(
                (packed & 31u) * 255u / 31u);
        }
        return rgba;
    }

    void submit_immediate(const std::function<void(VkCommandBuffer)>& record) {
        VkCommandBufferAllocateInfo allocation{};
        allocation.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;
        allocation.commandPool = command_pool_;
        allocation.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
        allocation.commandBufferCount = 1;
        VkCommandBuffer command = VK_NULL_HANDLE;
        vk_check(vkAllocateCommandBuffers(device_, &allocation, &command), "vkAllocateCommandBuffers(immediate)");
        ++command_buffer_allocation_count_;
        VkCommandBufferBeginInfo begin{};
        begin.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;
        begin.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
        vk_check(vkBeginCommandBuffer(command, &begin), "vkBeginCommandBuffer(immediate)");
        recording_barrier_count_ = 0u;
        record(command);
        vk_check(vkEndCommandBuffer(command), "vkEndCommandBuffer(immediate)");
        VkSubmitInfo submit{};
        submit.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO;
        submit.commandBufferCount = 1;
        submit.pCommandBuffers = &command;
        vk_check(vkQueueSubmit(graphics_queue_, 1, &submit, VK_NULL_HANDLE), "vkQueueSubmit(immediate)");
        ++queue_submission_count_;
        vk_check(vkQueueWaitIdle(graphics_queue_), "vkQueueWaitIdle(immediate)");
        barrier_count_ += recording_barrier_count_;
        recording_barrier_count_ = 0u;
        vkFreeCommandBuffers(device_, command_pool_, 1, &command);
    }

    HostTexture create_host_texture(
        uint32_t guest_address,
        uint32_t width,
        uint32_t height,
        std::string format,
        std::string content_hash,
        const std::vector<uint8_t>& rgba,
        VkFormat image_format = VK_FORMAT_R8G8B8A8_UNORM,
        bool render_target_feedback = false,
        const std::vector<std::vector<uint8_t>>& recovered_mips = {}) {
        HostTexture texture{};
        texture.guest_address = guest_address;
        texture.width = width;
        texture.height = height;
        texture.format = std::move(format);
        texture.content_hash = std::move(content_hash);
        texture.image_format = image_format;
        texture.render_target_feedback = render_target_feedback;
        const bool use_recovered_mips = recovered_mips.size() > 1u;
        std::vector<uint8_t> upload_rgba = use_recovered_mips
            ? recovered_mips.front()
            : rgba;
        std::vector<VkBufferImageCopy> upload_regions;
        uint32_t mip_width = width;
        uint32_t mip_height = height;
        size_t mip_offset = 0u;
        while (true) {
            VkBufferImageCopy region{};
            region.bufferOffset = mip_offset;
            region.imageSubresource.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
            region.imageSubresource.mipLevel = static_cast<uint32_t>(
                upload_regions.size());
            region.imageSubresource.layerCount = 1;
            region.imageExtent = {mip_width, mip_height, 1};
            upload_regions.push_back(region);
            if (render_target_feedback
                || (mip_width == 1u && mip_height == 1u)
                || (use_recovered_mips
                    && upload_regions.size() >= recovered_mips.size())) {
                break;
            }
            const uint32_t next_width = std::max(mip_width / 2u, 1u);
            const uint32_t next_height = std::max(mip_height / 2u, 1u);
            if (use_recovered_mips) {
                const std::vector<uint8_t>& next =
                    recovered_mips[upload_regions.size()];
                const size_t expected_size =
                    static_cast<size_t>(next_width) * next_height * 4u;
                if (next.size() != expected_size) {
                    throw std::runtime_error(
                        "invalid recovered texture mip payload");
                }
                mip_offset = upload_rgba.size();
                upload_rgba.insert(
                    upload_rgba.end(), next.begin(), next.end());
                mip_width = next_width;
                mip_height = next_height;
                continue;
            }
            std::vector<uint8_t> next(
                static_cast<size_t>(next_width) * next_height * 4u,
                0u);
            for (uint32_t y = 0u; y < next_height; ++y) {
                for (uint32_t x = 0u; x < next_width; ++x) {
                    for (uint32_t channel = 0u; channel < 4u; ++channel) {
                        uint32_t sum = 0u;
                        uint32_t count = 0u;
                        for (uint32_t dy = 0u; dy < 2u; ++dy) {
                            for (uint32_t dx = 0u; dx < 2u; ++dx) {
                                const uint32_t source_x = x * 2u + dx;
                                const uint32_t source_y = y * 2u + dy;
                                if (source_x >= mip_width
                                    || source_y >= mip_height) {
                                    continue;
                                }
                                const size_t source = mip_offset
                                    + (static_cast<size_t>(source_y)
                                        * mip_width + source_x) * 4u
                                    + channel;
                                sum += upload_rgba[source];
                                ++count;
                            }
                        }
                        const size_t destination =
                            (static_cast<size_t>(y) * next_width + x) * 4u
                            + channel;
                        next[destination] = static_cast<uint8_t>(
                            sum / std::max(count, 1u));
                    }
                }
            }
            mip_offset = upload_rgba.size();
            upload_rgba.insert(
                upload_rgba.end(), next.begin(), next.end());
            mip_width = next_width;
            mip_height = next_height;
        }
        texture.mip_levels = static_cast<uint32_t>(upload_regions.size());
        VkBuffer staging = VK_NULL_HANDLE;
        VkDeviceMemory staging_memory = VK_NULL_HANDLE;
        create_buffer(
            upload_rgba.size(),
            VK_BUFFER_USAGE_TRANSFER_SRC_BIT,
            VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT,
            staging,
            staging_memory);
        void* mapped = nullptr;
        vk_check(vkMapMemory(device_, staging_memory, 0, upload_rgba.size(), 0, &mapped), "vkMapMemory(texture)");
        std::memcpy(mapped, upload_rgba.data(), upload_rgba.size());
        upload_bytes_ += upload_rgba.size();
        vkUnmapMemory(device_, staging_memory);

        VkImageCreateInfo image_info{};
        image_info.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO;
        image_info.imageType = VK_IMAGE_TYPE_2D;
        image_info.extent = {width, height, 1};
        image_info.mipLevels = texture.mip_levels;
        image_info.arrayLayers = 1;
        image_info.format = image_format;
        image_info.tiling = VK_IMAGE_TILING_OPTIMAL;
        image_info.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
        image_info.usage = VK_IMAGE_USAGE_TRANSFER_DST_BIT
            | VK_IMAGE_USAGE_SAMPLED_BIT
            | (render_target_feedback
                ? VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT
                    | VK_IMAGE_USAGE_TRANSFER_SRC_BIT
                : 0u);
        image_info.samples = VK_SAMPLE_COUNT_1_BIT;
        image_info.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
        vk_check(vkCreateImage(device_, &image_info, nullptr, &texture.image), "vkCreateImage(texture)");
        VkMemoryRequirements requirements{};
        vkGetImageMemoryRequirements(device_, texture.image, &requirements);
        VkMemoryAllocateInfo image_allocation{};
        image_allocation.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;
        image_allocation.allocationSize = requirements.size;
        image_allocation.memoryTypeIndex = find_memory_type(requirements.memoryTypeBits, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
        vk_check(vkAllocateMemory(device_, &image_allocation, nullptr, &texture.memory), "vkAllocateMemory(texture)");
        vk_check(vkBindImageMemory(device_, texture.image, texture.memory, 0), "vkBindImageMemory(texture)");

        submit_immediate([&](VkCommandBuffer command) {
            VkImageMemoryBarrier to_transfer{};
            to_transfer.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
            to_transfer.srcAccessMask = 0;
            to_transfer.dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
            to_transfer.oldLayout = VK_IMAGE_LAYOUT_UNDEFINED;
            to_transfer.newLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
            to_transfer.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
            to_transfer.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
            to_transfer.image = texture.image;
            to_transfer.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
            to_transfer.subresourceRange.levelCount = texture.mip_levels;
            to_transfer.subresourceRange.layerCount = 1;
            ++recording_barrier_count_;
            vkCmdPipelineBarrier(command, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0, nullptr, 0, nullptr, 1, &to_transfer);
            vkCmdCopyBufferToImage(
                command,
                staging,
                texture.image,
                VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                static_cast<uint32_t>(upload_regions.size()),
                upload_regions.data());
            VkImageMemoryBarrier to_shader{};
            to_shader.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
            to_shader.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
            to_shader.dstAccessMask = VK_ACCESS_SHADER_READ_BIT;
            to_shader.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
            to_shader.newLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
            to_shader.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
            to_shader.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
            to_shader.image = texture.image;
            to_shader.subresourceRange = to_transfer.subresourceRange;
            ++recording_barrier_count_;
            vkCmdPipelineBarrier(command, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT, 0, 0, nullptr, 0, nullptr, 1, &to_shader);
        });
        vkDestroyBuffer(device_, staging, nullptr);
        vkFreeMemory(device_, staging_memory, nullptr);

        VkImageViewCreateInfo view_info{};
        view_info.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;
        view_info.image = texture.image;
        view_info.viewType = VK_IMAGE_VIEW_TYPE_2D;
        view_info.format = image_format;
        view_info.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
        view_info.subresourceRange.levelCount = texture.mip_levels;
        view_info.subresourceRange.layerCount = 1;
        vk_check(vkCreateImageView(device_, &view_info, nullptr, &texture.view), "vkCreateImageView(texture)");
        return texture;
    }

    bool build_gpu_texture_conversion_job(
        const RecoveredTextureResource& resource,
        size_t host_texture_index,
        GpuTextureConversionJob& job) const {
        if ((resource.format != "DXT1" && resource.format != "DXT5")
            || resource.width == 0u
            || resource.height == 0u
            || resource.payload.size() > std::numeric_limits<uint32_t>::max()) {
            return false;
        }
        const uint32_t block_bytes = resource.format == "DXT1" ? 8u : 16u;
        uint64_t payload_offset = 0u;
        uint32_t width = resource.width;
        uint32_t height = resource.height;
        while (width != 0u && height != 0u) {
            const uint64_t payload_size = static_cast<uint64_t>(
                (width + 3u) / 4u) * ((height + 3u) / 4u) * block_bytes;
            if (payload_offset + payload_size > resource.payload.size()) {
                break;
            }
            job.mips.push_back({
                width,
                height,
                static_cast<uint32_t>(payload_offset),
                0u,
                0u,
                false,
            });
            payload_offset += payload_size;
            if (width == 1u && height == 1u) {
                break;
            }
            width = std::max(width / 2u, 1u);
            height = std::max(height / 2u, 1u);
        }
        if (job.mips.empty()) {
            return false;
        }
        // Match create_host_texture exactly: a resource containing only its
        // base level receives a complete box-filtered mip chain, while a
        // recovered multi-level chain is authoritative and stops where the
        // guest payload stops.
        if (job.mips.size() == 1u) {
            width = resource.width;
            height = resource.height;
            while (width != 1u || height != 1u) {
                width = std::max(width / 2u, 1u);
                height = std::max(height / 2u, 1u);
                job.mips.push_back({
                    width,
                    height,
                    0u,
                    0u,
                    0u,
                    true,
                });
            }
        }
        job.resource = &resource;
        job.host_texture_index = host_texture_index;
        return true;
    }

    bool execute_gpu_texture_conversion_batch(
        std::vector<GpuTextureConversionJob>& jobs) {
        if (jobs.empty() || !ensure_texture_conversion_pipeline()) {
            return false;
        }
        const auto conversion_begin = std::chrono::steady_clock::now();
        uint64_t input_size = 0u;
        uint64_t output_size = 0u;
        uint32_t mip_count = 0u;
        uint32_t generated_mip_count = 0u;
        uint32_t dxt1_texture_count = 0u;
        uint32_t dxt5_texture_count = 0u;
        uint32_t recovered_chain_texture_count = 0u;
        uint32_t generated_chain_texture_count = 0u;
        uint64_t dxt1_input_bytes = 0u;
        uint64_t dxt5_input_bytes = 0u;
        for (GpuTextureConversionJob& job : jobs) {
            input_size = (input_size + 3u) & ~uint64_t{3u};
            if (input_size > std::numeric_limits<uint32_t>::max()
                || job.resource->payload.size()
                    > std::numeric_limits<uint32_t>::max() - input_size) {
                return false;
            }
            job.input_byte_offset = static_cast<uint32_t>(input_size);
            input_size += job.resource->payload.size();
            if (job.resource->format == "DXT1") {
                ++dxt1_texture_count;
                dxt1_input_bytes += job.resource->payload.size();
            } else {
                ++dxt5_texture_count;
                dxt5_input_bytes += job.resource->payload.size();
            }
            const bool generated_chain = std::any_of(
                job.mips.begin(),
                job.mips.end(),
                [](const GpuTextureConversionMip& mip) {
                    return mip.generated;
                });
            generated_chain_texture_count += generated_chain ? 1u : 0u;
            recovered_chain_texture_count +=
                job.mips.size() > 1u && !generated_chain ? 1u : 0u;
            for (size_t mip_index = 0u;
                 mip_index < job.mips.size();
                 ++mip_index) {
                GpuTextureConversionMip& mip = job.mips[mip_index];
                output_size = (output_size + 3u) & ~uint64_t{3u};
                const uint64_t mip_size = static_cast<uint64_t>(mip.width)
                    * mip.height * 4u;
                if (output_size > std::numeric_limits<uint32_t>::max()
                    || mip_size
                        > std::numeric_limits<uint32_t>::max() - output_size) {
                    return false;
                }
                mip.output_byte_offset = static_cast<uint32_t>(output_size);
                if (mip.generated) {
                    mip.source_output_byte_offset =
                        job.mips[mip_index - 1u].output_byte_offset;
                    ++generated_mip_count;
                }
                output_size += mip_size;
                ++mip_count;
            }
        }
        input_size = (input_size + 3u) & ~uint64_t{3u};
        output_size = (output_size + 3u) & ~uint64_t{3u};
        VkPhysicalDeviceProperties properties{};
        vkGetPhysicalDeviceProperties(physical_device_, &properties);
        if (input_size == 0u
            || output_size == 0u
            || input_size > properties.limits.maxStorageBufferRange
            || output_size > properties.limits.maxStorageBufferRange) {
            return false;
        }

        GpuTextureValidationCoverage pending_validation_coverage =
            gpu_texture_validation_coverage_;
        std::vector<size_t> validation_job_indices;
        uint32_t validation_mip_count = 0u;
        uint64_t validation_byte_count = 0u;
        const bool validate_gpu_texture_conversion =
            options_.strict_render_validation
            || !options_.live_render_stream;
        if (validate_gpu_texture_conversion
            && !gpu_texture_validation_coverage_.complete()) {
            const auto generated_mips = [](
                const GpuTextureConversionJob& job) {
                return std::any_of(
                    job.mips.begin(),
                    job.mips.end(),
                    [](const GpuTextureConversionMip& mip) {
                        return mip.generated;
                    });
            };
            const auto converted_bytes = [](
                const GpuTextureConversionJob& job) {
                uint64_t bytes = 0u;
                for (const GpuTextureConversionMip& mip : job.mips) {
                    bytes += static_cast<uint64_t>(mip.width)
                        * mip.height * 4u;
                }
                return bytes;
            };
            const auto add_smallest_candidate = [&](const auto& predicate) {
                size_t selected = jobs.size();
                uint64_t selected_bytes =
                    std::numeric_limits<uint64_t>::max();
                for (size_t job_index = 0u;
                     job_index < jobs.size();
                     ++job_index) {
                    const GpuTextureConversionJob& job = jobs[job_index];
                    const uint64_t bytes = converted_bytes(job);
                    if (predicate(job) && bytes < selected_bytes) {
                        selected = job_index;
                        selected_bytes = bytes;
                    }
                }
                if (selected != jobs.size()
                    && std::find(
                        validation_job_indices.begin(),
                        validation_job_indices.end(),
                        selected) == validation_job_indices.end()) {
                    validation_job_indices.push_back(selected);
                }
            };
            if (!gpu_texture_validation_coverage_.dxt1) {
                add_smallest_candidate([](
                    const GpuTextureConversionJob& job) {
                    return job.resource->format == "DXT1";
                });
            }
            if (!gpu_texture_validation_coverage_.dxt5) {
                add_smallest_candidate([](
                    const GpuTextureConversionJob& job) {
                    return job.resource->format == "DXT5";
                });
            }
            if (!gpu_texture_validation_coverage_.recovered_mips) {
                add_smallest_candidate([&](
                    const GpuTextureConversionJob& job) {
                    return job.mips.size() > 1u && !generated_mips(job);
                });
            }
            if (!gpu_texture_validation_coverage_.generated_mips) {
                add_smallest_candidate([&](
                    const GpuTextureConversionJob& job) {
                    return generated_mips(job);
                });
            }
            for (const size_t job_index : validation_job_indices) {
                const GpuTextureConversionJob& job = jobs[job_index];
                const bool has_generated_mips = generated_mips(job);
                const bool has_recovered_mips =
                    job.mips.size() > 1u && !has_generated_mips;
                validation_mip_count += static_cast<uint32_t>(
                    job.mips.size());
                validation_byte_count += converted_bytes(job);
                pending_validation_coverage.dxt1 =
                    pending_validation_coverage.dxt1
                    || job.resource->format == "DXT1";
                pending_validation_coverage.dxt5 =
                    pending_validation_coverage.dxt5
                    || job.resource->format == "DXT5";
                pending_validation_coverage.recovered_mips =
                    pending_validation_coverage.recovered_mips
                    || has_recovered_mips;
                pending_validation_coverage.generated_mips =
                    pending_validation_coverage.generated_mips
                    || has_generated_mips;
            }
        }
        std::vector<VkBufferCopy> validation_copies;
        uint64_t validation_buffer_size = 0u;
        for (const size_t job_index : validation_job_indices) {
            for (const GpuTextureConversionMip& mip : jobs[job_index].mips) {
                VkBufferCopy copy{};
                copy.srcOffset = mip.output_byte_offset;
                copy.dstOffset = validation_buffer_size;
                copy.size = static_cast<uint64_t>(mip.width)
                    * mip.height * 4u;
                validation_copies.push_back(copy);
                validation_buffer_size += copy.size;
            }
        }

        std::vector<uint8_t> input_bytes(static_cast<size_t>(input_size), 0u);
        for (const GpuTextureConversionJob& job : jobs) {
            std::memcpy(
                input_bytes.data() + job.input_byte_offset,
                job.resource->payload.data(),
                job.resource->payload.size());
            HostTexture& texture = host_textures_[job.host_texture_index];
            texture.image_format = VK_FORMAT_R8G8B8A8_UNORM;
            texture.mip_levels = static_cast<uint32_t>(job.mips.size());
            VkImageCreateInfo image_info{};
            image_info.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO;
            image_info.imageType = VK_IMAGE_TYPE_2D;
            image_info.extent = {texture.width, texture.height, 1u};
            image_info.mipLevels = texture.mip_levels;
            image_info.arrayLayers = 1u;
            image_info.format = texture.image_format;
            image_info.tiling = VK_IMAGE_TILING_OPTIMAL;
            image_info.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
            image_info.usage = VK_IMAGE_USAGE_TRANSFER_DST_BIT
                | VK_IMAGE_USAGE_SAMPLED_BIT;
            image_info.samples = VK_SAMPLE_COUNT_1_BIT;
            image_info.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
            vk_check(
                vkCreateImage(
                    device_, &image_info, nullptr, &texture.image),
                "vkCreateImage(GPU-converted texture)");
            VkMemoryRequirements requirements{};
            vkGetImageMemoryRequirements(
                device_, texture.image, &requirements);
            VkMemoryAllocateInfo allocation{};
            allocation.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;
            allocation.allocationSize = requirements.size;
            allocation.memoryTypeIndex = find_memory_type(
                requirements.memoryTypeBits,
                VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
            vk_check(
                vkAllocateMemory(
                    device_, &allocation, nullptr, &texture.memory),
                "vkAllocateMemory(GPU-converted texture)");
            vk_check(
                vkBindImageMemory(
                    device_, texture.image, texture.memory, 0u),
                "vkBindImageMemory(GPU-converted texture)");
            VkImageViewCreateInfo view_info{};
            view_info.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;
            view_info.image = texture.image;
            view_info.viewType = VK_IMAGE_VIEW_TYPE_2D;
            view_info.format = texture.image_format;
            view_info.subresourceRange.aspectMask =
                VK_IMAGE_ASPECT_COLOR_BIT;
            view_info.subresourceRange.levelCount = texture.mip_levels;
            view_info.subresourceRange.layerCount = 1u;
            vk_check(
                vkCreateImageView(
                    device_, &view_info, nullptr, &texture.view),
                "vkCreateImageView(GPU-converted texture)");
        }

        VkBuffer input_buffer = VK_NULL_HANDLE;
        VkDeviceMemory input_memory = VK_NULL_HANDLE;
        VkBuffer output_buffer = VK_NULL_HANDLE;
        VkDeviceMemory output_memory = VK_NULL_HANDLE;
        VkBuffer validation_buffer = VK_NULL_HANDLE;
        VkDeviceMemory validation_memory = VK_NULL_HANDLE;
        create_buffer(
            input_size,
            VK_BUFFER_USAGE_STORAGE_BUFFER_BIT,
            VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT
                | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT,
            input_buffer,
            input_memory);
        create_buffer(
            output_size,
            VK_BUFFER_USAGE_STORAGE_BUFFER_BIT
                | VK_BUFFER_USAGE_TRANSFER_SRC_BIT,
            VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT,
            output_buffer,
            output_memory);
        if (!validation_job_indices.empty()) {
            create_buffer(
                validation_buffer_size,
                VK_BUFFER_USAGE_TRANSFER_DST_BIT,
                VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT
                    | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT,
                validation_buffer,
                validation_memory);
        }
        void* mapped = nullptr;
        vk_check(
            vkMapMemory(
                device_, input_memory, 0u, input_size, 0u, &mapped),
            "vkMapMemory(texture conversion input)");
        std::memcpy(mapped, input_bytes.data(), input_bytes.size());
        upload_bytes_ += input_bytes.size();
        vkUnmapMemory(device_, input_memory);

        VkDescriptorPoolSize pool_size{};
        pool_size.type = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
        pool_size.descriptorCount = 2u;
        VkDescriptorPoolCreateInfo pool_info{};
        pool_info.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO;
        pool_info.maxSets = 1u;
        pool_info.poolSizeCount = 1u;
        pool_info.pPoolSizes = &pool_size;
        VkDescriptorPool descriptor_pool = VK_NULL_HANDLE;
        vk_check(
            vkCreateDescriptorPool(
                device_, &pool_info, nullptr, &descriptor_pool),
            "vkCreateDescriptorPool(texture conversion)");
        VkDescriptorSetAllocateInfo descriptor_allocation{};
        descriptor_allocation.sType =
            VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO;
        descriptor_allocation.descriptorPool = descriptor_pool;
        descriptor_allocation.descriptorSetCount = 1u;
        descriptor_allocation.pSetLayouts =
            &texture_convert_descriptor_layout_;
        VkDescriptorSet descriptor_set = VK_NULL_HANDLE;
        vk_check(
            vkAllocateDescriptorSets(
                device_, &descriptor_allocation, &descriptor_set),
            "vkAllocateDescriptorSets(texture conversion)");
        ++descriptor_allocation_count_;
        const std::array<VkDescriptorBufferInfo, 2> buffer_infos{{
            {input_buffer, 0u, input_size},
            {output_buffer, 0u, output_size},
        }};
        std::array<VkWriteDescriptorSet, 2> descriptor_writes{};
        for (uint32_t index = 0u; index < descriptor_writes.size(); ++index) {
            descriptor_writes[index].sType =
                VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
            descriptor_writes[index].dstSet = descriptor_set;
            descriptor_writes[index].dstBinding = index;
            descriptor_writes[index].descriptorCount = 1u;
            descriptor_writes[index].descriptorType =
                VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
            descriptor_writes[index].pBufferInfo = &buffer_infos[index];
        }
        vkUpdateDescriptorSets(
            device_,
            static_cast<uint32_t>(descriptor_writes.size()),
            descriptor_writes.data(),
            0u,
            nullptr);

        const auto submission_begin = std::chrono::steady_clock::now();
        submit_immediate([&](VkCommandBuffer command) {
            vkCmdBindPipeline(
                command,
                VK_PIPELINE_BIND_POINT_COMPUTE,
                texture_convert_pipeline_);
            vkCmdBindDescriptorSets(
                command,
                VK_PIPELINE_BIND_POINT_COMPUTE,
                texture_convert_pipeline_layout_,
                0u,
                1u,
                &descriptor_set,
                0u,
                nullptr);
            for (const GpuTextureConversionJob& job : jobs) {
                for (size_t mip_index = 0u;
                     mip_index < job.mips.size();
                     ++mip_index) {
                    const GpuTextureConversionMip& mip = job.mips[mip_index];
                    if (mip.generated) {
                        VkBufferMemoryBarrier dependency{};
                        dependency.sType =
                            VK_STRUCTURE_TYPE_BUFFER_MEMORY_BARRIER;
                        dependency.srcAccessMask =
                            VK_ACCESS_SHADER_WRITE_BIT;
                        dependency.dstAccessMask =
                            VK_ACCESS_SHADER_READ_BIT
                            | VK_ACCESS_SHADER_WRITE_BIT;
                        dependency.srcQueueFamilyIndex =
                            VK_QUEUE_FAMILY_IGNORED;
                        dependency.dstQueueFamilyIndex =
                            VK_QUEUE_FAMILY_IGNORED;
                        dependency.buffer = output_buffer;
                        dependency.offset = 0u;
                        dependency.size = output_size;
                        ++recording_barrier_count_;
                        vkCmdPipelineBarrier(
                            command,
                            VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
                            VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
                            0u,
                            0u,
                            nullptr,
                            1u,
                            &dependency,
                            0u,
                            nullptr);
                    }
                    NativeTextureConvertPushConstants push{};
                    push.input_byte_offset = job.input_byte_offset
                        + mip.input_byte_offset;
                    push.output_word_offset = mip.output_byte_offset
                        / sizeof(uint32_t);
                    push.source_word_offset = mip.source_output_byte_offset
                        / sizeof(uint32_t);
                    push.width = mip.width;
                    push.height = mip.height;
                    if (mip.generated) {
                        const GpuTextureConversionMip& source =
                            job.mips[mip_index - 1u];
                        push.source_width = source.width;
                        push.source_height = source.height;
                        push.mode = 2u;
                    } else {
                        push.mode = job.resource->format == "DXT1" ? 0u : 1u;
                    }
                    vkCmdPushConstants(
                        command,
                        texture_convert_pipeline_layout_,
                        VK_SHADER_STAGE_COMPUTE_BIT,
                        0u,
                        sizeof(push),
                        &push);
                    vkCmdDispatch(
                        command,
                        (mip.width + 7u) / 8u,
                        (mip.height + 7u) / 8u,
                        1u);
                }
            }
            VkBufferMemoryBarrier output_ready{};
            output_ready.sType = VK_STRUCTURE_TYPE_BUFFER_MEMORY_BARRIER;
            output_ready.srcAccessMask = VK_ACCESS_SHADER_WRITE_BIT;
            output_ready.dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
            output_ready.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
            output_ready.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
            output_ready.buffer = output_buffer;
            output_ready.offset = 0u;
            output_ready.size = output_size;
            ++recording_barrier_count_;
            vkCmdPipelineBarrier(
                command,
                VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
                VK_PIPELINE_STAGE_TRANSFER_BIT,
                0u,
                0u,
                nullptr,
                1u,
                &output_ready,
                0u,
                nullptr);
            if (validation_buffer != VK_NULL_HANDLE) {
                vkCmdCopyBuffer(
                    command,
                    output_buffer,
                    validation_buffer,
                    static_cast<uint32_t>(validation_copies.size()),
                    validation_copies.data());
            }

            std::vector<VkImageMemoryBarrier> to_transfer;
            to_transfer.reserve(jobs.size());
            for (const GpuTextureConversionJob& job : jobs) {
                const HostTexture& texture =
                    host_textures_[job.host_texture_index];
                VkImageMemoryBarrier barrier{};
                barrier.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
                barrier.dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
                barrier.oldLayout = VK_IMAGE_LAYOUT_UNDEFINED;
                barrier.newLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
                barrier.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
                barrier.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
                barrier.image = texture.image;
                barrier.subresourceRange.aspectMask =
                    VK_IMAGE_ASPECT_COLOR_BIT;
                barrier.subresourceRange.levelCount = texture.mip_levels;
                barrier.subresourceRange.layerCount = 1u;
                to_transfer.push_back(barrier);
            }
            ++recording_barrier_count_;
            vkCmdPipelineBarrier(
                command,
                VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,
                VK_PIPELINE_STAGE_TRANSFER_BIT,
                0u,
                0u,
                nullptr,
                0u,
                nullptr,
                static_cast<uint32_t>(to_transfer.size()),
                to_transfer.data());
            for (const GpuTextureConversionJob& job : jobs) {
                const HostTexture& texture =
                    host_textures_[job.host_texture_index];
                std::vector<VkBufferImageCopy> regions;
                regions.reserve(job.mips.size());
                for (uint32_t mip_index = 0u;
                     mip_index < job.mips.size();
                     ++mip_index) {
                    const GpuTextureConversionMip& mip = job.mips[mip_index];
                    VkBufferImageCopy region{};
                    region.bufferOffset = mip.output_byte_offset;
                    region.imageSubresource.aspectMask =
                        VK_IMAGE_ASPECT_COLOR_BIT;
                    region.imageSubresource.mipLevel = mip_index;
                    region.imageSubresource.layerCount = 1u;
                    region.imageExtent = {mip.width, mip.height, 1u};
                    regions.push_back(region);
                }
                vkCmdCopyBufferToImage(
                    command,
                    output_buffer,
                    texture.image,
                    VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                    static_cast<uint32_t>(regions.size()),
                    regions.data());
            }
            std::vector<VkImageMemoryBarrier> to_shader = to_transfer;
            for (VkImageMemoryBarrier& barrier : to_shader) {
                barrier.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
                barrier.dstAccessMask = VK_ACCESS_SHADER_READ_BIT;
                barrier.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
                barrier.newLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
            }
            ++recording_barrier_count_;
            vkCmdPipelineBarrier(
                command,
                VK_PIPELINE_STAGE_TRANSFER_BIT,
                VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT,
                0u,
                0u,
                nullptr,
                0u,
                nullptr,
                static_cast<uint32_t>(to_shader.size()),
                to_shader.data());
            if (validation_buffer != VK_NULL_HANDLE) {
                VkBufferMemoryBarrier validation_ready{};
                validation_ready.sType =
                    VK_STRUCTURE_TYPE_BUFFER_MEMORY_BARRIER;
                validation_ready.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
                validation_ready.dstAccessMask = VK_ACCESS_HOST_READ_BIT;
                validation_ready.srcQueueFamilyIndex =
                    VK_QUEUE_FAMILY_IGNORED;
                validation_ready.dstQueueFamilyIndex =
                    VK_QUEUE_FAMILY_IGNORED;
                validation_ready.buffer = validation_buffer;
                validation_ready.offset = 0u;
                validation_ready.size = validation_buffer_size;
                ++recording_barrier_count_;
                vkCmdPipelineBarrier(
                    command,
                    VK_PIPELINE_STAGE_TRANSFER_BIT,
                    VK_PIPELINE_STAGE_HOST_BIT,
                    0u,
                    0u,
                    nullptr,
                    1u,
                    &validation_ready,
                    0u,
                nullptr);
            }
        });
        const uint64_t gpu_submission_us = std::chrono::duration_cast<
            std::chrono::microseconds>(
                std::chrono::steady_clock::now() - submission_begin).count();
        const uint64_t setup_us = std::chrono::duration_cast<
            std::chrono::microseconds>(
                submission_begin - conversion_begin).count();

        bool validation_passed = true;
        uint64_t validation_mismatch_bytes = 0u;
        uint32_t first_mismatch_address = 0u;
        uint32_t first_mismatch_mip = 0u;
        uint64_t first_mismatch_byte = 0u;
        uint32_t first_mismatch_expected = 0u;
        uint32_t first_mismatch_actual = 0u;
        uint64_t validation_cpu_us = 0u;
        if (validation_buffer != VK_NULL_HANDLE) {
            readback_bytes_ += static_cast<uint64_t>(validation_buffer_size);
            const auto validation_begin = std::chrono::steady_clock::now();
            void* validation_mapped = nullptr;
            vk_check(
                vkMapMemory(
                    device_,
                    validation_memory,
                    0u,
                    validation_buffer_size,
                    0u,
                    &validation_mapped),
                "vkMapMemory(texture conversion validation)");
            const auto* actual = static_cast<const uint8_t*>(
                validation_mapped);
            bool first_mismatch_recorded = false;
            size_t validation_copy_index = 0u;
            for (const size_t job_index : validation_job_indices) {
                const GpuTextureConversionJob& job = jobs[job_index];
                const std::vector<std::vector<uint8_t>> expected_mips =
                    build_cpu_dxt_conversion_mips(*job.resource);
                if (expected_mips.size() != job.mips.size()) {
                    validation_passed = false;
                    ++validation_mismatch_bytes;
                    if (!first_mismatch_recorded) {
                        first_mismatch_recorded = true;
                        first_mismatch_address = job.resource->address;
                        first_mismatch_mip = static_cast<uint32_t>(
                            std::min(expected_mips.size(), job.mips.size()));
                    }
                    validation_copy_index += job.mips.size();
                    continue;
                }
                for (size_t mip_index = 0u;
                     mip_index < job.mips.size();
                     ++mip_index) {
                    const GpuTextureConversionMip& mip =
                        job.mips[mip_index];
                    const std::vector<uint8_t>& expected =
                        expected_mips[mip_index];
                    const VkBufferCopy& validation_copy =
                        validation_copies[validation_copy_index++];
                    const size_t expected_size = static_cast<size_t>(
                        mip.width) * mip.height * 4u;
                    if (expected.size() != expected_size) {
                        validation_passed = false;
                        ++validation_mismatch_bytes;
                        if (!first_mismatch_recorded) {
                            first_mismatch_recorded = true;
                            first_mismatch_address = job.resource->address;
                            first_mismatch_mip = static_cast<uint32_t>(
                                mip_index);
                        }
                        continue;
                    }
                    const uint8_t* actual_mip = actual
                        + validation_copy.dstOffset;
                    for (size_t byte_index = 0u;
                         byte_index < expected.size();
                         ++byte_index) {
                        if (actual_mip[byte_index] == expected[byte_index]) {
                            continue;
                        }
                        validation_passed = false;
                        ++validation_mismatch_bytes;
                        if (!first_mismatch_recorded) {
                            first_mismatch_recorded = true;
                            first_mismatch_address = job.resource->address;
                            first_mismatch_mip = static_cast<uint32_t>(
                                mip_index);
                            first_mismatch_byte = byte_index;
                            first_mismatch_expected = expected[byte_index];
                            first_mismatch_actual = actual_mip[byte_index];
                        }
                    }
                }
            }
            vkUnmapMemory(device_, validation_memory);
            validation_cpu_us = std::chrono::duration_cast<
                std::chrono::microseconds>(
                    std::chrono::steady_clock::now()
                    - validation_begin).count();
            if (validation_passed) {
                gpu_texture_validation_coverage_ =
                    pending_validation_coverage;
            }
            log_.emit(
                "nv2a_gpu_texture_conversion_validation",
                {
                    {"passed", json_bool(validation_passed)},
                    {"textures", std::to_string(
                        validation_job_indices.size())},
                    {"mips", std::to_string(validation_mip_count)},
                    {"bytes", std::to_string(validation_byte_count)},
                    {"mismatch_bytes", std::to_string(
                        validation_mismatch_bytes)},
                    {"first_mismatch_address", std::to_string(
                        first_mismatch_address)},
                    {"first_mismatch_mip", std::to_string(
                        first_mismatch_mip)},
                    {"first_mismatch_byte", std::to_string(
                        first_mismatch_byte)},
                    {"first_mismatch_expected", std::to_string(
                        first_mismatch_expected)},
                    {"first_mismatch_actual", std::to_string(
                        first_mismatch_actual)},
                    {"validation_cpu_us", std::to_string(
                        validation_cpu_us)},
                    {"dxt1_covered", json_bool(
                        gpu_texture_validation_coverage_.dxt1)},
                    {"dxt5_covered", json_bool(
                        gpu_texture_validation_coverage_.dxt5)},
                    {"recovered_mips_covered", json_bool(
                        gpu_texture_validation_coverage_.recovered_mips)},
                    {"generated_mips_covered", json_bool(
                        gpu_texture_validation_coverage_.generated_mips)},
                    {"coverage_complete", json_bool(
                        gpu_texture_validation_coverage_.complete())},
                    {"failure_action", json_string(
                        validation_passed
                            ? "accept_gpu_batch"
                            : "destroy_gpu_batch_and_use_cpu")},
                });
        }

        vkDestroyDescriptorPool(device_, descriptor_pool, nullptr);
        if (validation_buffer != VK_NULL_HANDLE) {
            vkDestroyBuffer(device_, validation_buffer, nullptr);
            vkFreeMemory(device_, validation_memory, nullptr);
        }
        vkDestroyBuffer(device_, output_buffer, nullptr);
        vkFreeMemory(device_, output_memory, nullptr);
        vkDestroyBuffer(device_, input_buffer, nullptr);
        vkFreeMemory(device_, input_memory, nullptr);
        if (!validation_passed) {
            for (const GpuTextureConversionJob& job : jobs) {
                destroy_host_texture(
                    host_textures_[job.host_texture_index]);
            }
            ++gpu_texture_conversion_rejected_batch_count_;
            return false;
        }
        last_gpu_texture_conversion_us_ = std::chrono::duration_cast<
            std::chrono::microseconds>(
                std::chrono::steady_clock::now() - conversion_begin).count();
        gpu_texture_conversion_batch_count_ += 1u;
        gpu_texture_conversion_texture_count_ += jobs.size();
        gpu_texture_conversion_mip_count_ += mip_count;
        gpu_texture_conversion_input_bytes_ += input_size;
        gpu_texture_conversion_output_bytes_ += output_size;
        log_.emit(
            "nv2a_gpu_texture_conversion_batch",
            {
                {"textures", std::to_string(jobs.size())},
                {"dxt1_textures", std::to_string(dxt1_texture_count)},
                {"dxt5_textures", std::to_string(dxt5_texture_count)},
                {"recovered_chain_textures", std::to_string(
                    recovered_chain_texture_count)},
                {"generated_chain_textures", std::to_string(
                    generated_chain_texture_count)},
                {"mips", std::to_string(mip_count)},
                {"generated_mips", std::to_string(generated_mip_count)},
                {"input_bytes", std::to_string(input_size)},
                {"dxt1_input_bytes", std::to_string(dxt1_input_bytes)},
                {"dxt5_input_bytes", std::to_string(dxt5_input_bytes)},
                {"output_bytes", std::to_string(output_size)},
                {"dispatches", std::to_string(mip_count)},
                {"setup_us", std::to_string(setup_us)},
                {"gpu_submission_us", std::to_string(gpu_submission_us)},
                {"validation_cpu_us", std::to_string(validation_cpu_us)},
                {"conversion_us", std::to_string(
                    last_gpu_texture_conversion_us_)},
                {"cumulative_batches", std::to_string(
                    gpu_texture_conversion_batch_count_)},
                {"cumulative_textures", std::to_string(
                    gpu_texture_conversion_texture_count_)},
                {"cumulative_mips", std::to_string(
                    gpu_texture_conversion_mip_count_)},
                {"cumulative_input_bytes", std::to_string(
                    gpu_texture_conversion_input_bytes_)},
                {"cumulative_output_bytes", std::to_string(
                    gpu_texture_conversion_output_bytes_)},
                {"cumulative_rejected_batches", std::to_string(
                    gpu_texture_conversion_rejected_batch_count_)},
            });
        return true;
    }

    std::optional<HostTexture> create_cpu_converted_host_texture(
        const RecoveredTextureResource& resource,
        const std::string& content_identity) {
        std::vector<uint8_t> rgba;
        std::vector<std::vector<uint8_t>> recovered_mips;
        if (resource.format == "DXT1" || resource.format == "DXT5") {
            recovered_mips = build_cpu_dxt_conversion_mips(resource);
            if (recovered_mips.empty()) {
                return std::nullopt;
            }
            rgba = recovered_mips.front();
        } else if (resource.format == "R5G6B5") {
            rgba = convert_r5g6b5_texture(resource);
        } else if (resource.format == "A8R8G8B8"
                   || resource.format == "A8R8G8B8_LINEAR") {
            rgba = convert_bgra8_texture(
                resource,
                false,
                resource.format == "A8R8G8B8");
        } else if (resource.format == "X8R8G8B8"
                   || resource.format == "X8R8G8B8_LINEAR") {
            rgba = convert_bgra8_texture(
                resource,
                true,
                resource.format == "X8R8G8B8");
        } else {
            const size_t rgba_size = static_cast<size_t>(resource.width)
                * resource.height * 4u;
            rgba.assign(
                resource.payload.begin(),
                resource.payload.begin()
                    + static_cast<std::ptrdiff_t>(rgba_size));
        }
        return create_host_texture(
            resource.address,
            resource.width,
            resource.height,
            resource.format,
            content_identity,
            rgba,
            VK_FORMAT_R8G8B8A8_UNORM,
            false,
            recovered_mips);
    }

    static void clear_moved_texture_handles(HostTexture& texture) {
        texture.image = VK_NULL_HANDLE;
        texture.memory = VK_NULL_HANDLE;
        texture.view = VK_NULL_HANDLE;
    }

    void destroy_host_texture(HostTexture& texture) {
        if (texture.view) vkDestroyImageView(device_, texture.view, nullptr);
        if (texture.image) vkDestroyImage(device_, texture.image, nullptr);
        if (texture.memory) vkFreeMemory(device_, texture.memory, nullptr);
        clear_moved_texture_handles(texture);
    }

    static std::string texture_content_identity(
        const RecoveredTextureResource& resource) {
        if (!resource.content_hash.empty()) {
            return resource.content_hash;
        }
        uint64_t fingerprint = 14695981039346656037ull;
        for (const uint8_t byte : resource.payload) {
            fingerprint ^= byte;
            fingerprint *= 1099511628211ull;
        }
        std::ostringstream value;
        value << "fnv64:" << std::hex << std::uppercase << std::setfill('0')
              << std::setw(16) << fingerprint;
        return value.str();
    }

    std::pair<uint32_t, uint32_t> draw_surface_extent(
        const NativeDraw& draw) const {
        const uint32_t clip_x = draw.surface_clip_horizontal & 0xFFFFu;
        const uint32_t clip_y = draw.surface_clip_vertical & 0xFFFFu;
        const uint32_t clip_width =
            (draw.surface_clip_horizontal >> 16u) & 0xFFFFu;
        const uint32_t clip_height =
            (draw.surface_clip_vertical >> 16u) & 0xFFFFu;
        const uint32_t color_pitch = draw.surface_pitch & 0xFFFFu;
        uint32_t width = color_pitch / 4u;
        if (width == 0u) {
            width = clip_x == 0u ? clip_width : swapchain_extent_.width;
        }
        uint32_t height = clip_y == 0u
            ? clip_height
            : swapchain_extent_.height;
        if (height == 0u) {
            height = swapchain_extent_.height;
        }
        return {width, height};
    }

    uint32_t select_presented_surface_color_offset() const {
        std::unordered_map<uint32_t, uint32_t> target_counts;
        const size_t first_draw = std::min<size_t>(
            interpreted_stream_.presented_draw_begin,
            interpreted_stream_.draws.size());
        const size_t end_draw = std::min<size_t>(
            first_draw + interpreted_stream_.presented_draw_count,
            interpreted_stream_.draws.size());
        uint32_t selected = 0u;
        uint32_t selected_count = 0u;
        for (size_t draw_index = first_draw; draw_index < end_draw; ++draw_index) {
            const NativeDraw& draw = interpreted_stream_.draws[draw_index];
            const auto [width, height] = draw_surface_extent(draw);
            if (draw.surface_color_offset == 0u
                || width != swapchain_extent_.width
                || height != swapchain_extent_.height) {
                continue;
            }
            const uint32_t count = ++target_counts[draw.surface_color_offset];
            if (count >= selected_count) {
                selected = draw.surface_color_offset;
                selected_count = count;
            }
        }
        return selected;
    }

    bool draw_surface_clip_is_subsurface_viewport(
        const NativeDraw& draw) const {
        const uint32_t clip_x = draw.surface_clip_horizontal & 0xFFFFu;
        const uint32_t clip_width = draw.surface_clip_horizontal >> 16u;
        const uint32_t clip_y = draw.surface_clip_vertical & 0xFFFFu;
        const uint32_t clip_height = draw.surface_clip_vertical >> 16u;
        if (clip_width == 0u || clip_height == 0u
            || (clip_width >= swapchain_extent_.width
                && clip_height >= swapchain_extent_.height)
            || (draw.transform_execution_mode & 3u) != 2u) {
            return false;
        }

        const float viewport_scale_x =
            float_from_u32(draw.transform_constants[58][0]);
        const float viewport_scale_y =
            float_from_u32(draw.transform_constants[58][1]);
        const float viewport_offset_x =
            float_from_u32(draw.transform_constants[59][0]);
        const float viewport_offset_y =
            float_from_u32(draw.transform_constants[59][1]);
        if (!std::isfinite(viewport_scale_x)
            || !std::isfinite(viewport_scale_y)
            || !std::isfinite(viewport_offset_x)
            || !std::isfinite(viewport_offset_y)) {
            return false;
        }

        constexpr float kViewportBias = 0.53125f;
        constexpr float kViewportTolerance = 0.01f;
        const float half_width = static_cast<float>(clip_width) * 0.5f;
        const float half_height = static_cast<float>(clip_height) * 0.5f;
        return std::abs(viewport_scale_x - half_width)
                <= kViewportTolerance
            && std::abs(viewport_scale_y + half_height)
                <= kViewportTolerance
            && std::abs(
                viewport_offset_x
                    - (static_cast<float>(clip_x) + half_width
                        + kViewportBias))
                <= kViewportTolerance
            && std::abs(
                viewport_offset_y
                    - (static_cast<float>(clip_y) + half_height
                        + kViewportBias))
                <= kViewportTolerance;
    }

    bool draw_targets_presented_surface(const NativeDraw& draw) const {
        // The title builds camera textures in atlas-shaped regions of the
        // presented allocation. SET_SURFACE_CLIP alone cannot distinguish
        // those passes from UI scissors, but their programmable viewport is
        // centered and scaled to the clip rectangle exactly.
        if (draw_surface_clip_is_subsurface_viewport(draw)) {
            return false;
        }
        if (presented_surface_color_offset_ != 0u) {
            return draw.surface_color_offset == presented_surface_color_offset_;
        }
        if (draw.surface_clip_horizontal == 0u
            || draw.surface_clip_vertical == 0u) {
            return true;
        }
        const uint32_t clip_x = draw.surface_clip_horizontal & 0xFFFFu;
        const uint32_t clip_width = draw.surface_clip_horizontal >> 16u;
        const uint32_t clip_y = draw.surface_clip_vertical & 0xFFFFu;
        const uint32_t clip_height = draw.surface_clip_vertical >> 16u;
        // SET_SURFACE_CLIP is also the title's per-widget scissor. Once the
        // viewport-matched subpasses above are excluded, retain non-zero-origin
        // clips as draws on the current surface. Only an origin-zero
        // sub-surface remains a useful fallback discriminator when an address
        // was not captured.
        if (clip_x != 0u || clip_y != 0u) {
            return true;
        }
        return clip_width >= swapchain_extent_.width
            && clip_height >= swapchain_extent_.height;
    }

    VkRect2D draw_scissor(
        const NativeDraw& draw,
        VkExtent2D target_extent) const {
        const uint32_t clip_x = draw.surface_clip_horizontal & 0xFFFFu;
        const uint32_t clip_width = draw.surface_clip_horizontal >> 16u;
        const uint32_t clip_y = draw.surface_clip_vertical & 0xFFFFu;
        const uint32_t clip_height = draw.surface_clip_vertical >> 16u;
        if (clip_width == 0u || clip_height == 0u) {
            return {{0, 0}, target_extent};
        }
        const uint32_t bounded_x = std::min(clip_x, target_extent.width);
        const uint32_t bounded_y = std::min(clip_y, target_extent.height);
        return {
            {
                static_cast<int32_t>(bounded_x),
                static_cast<int32_t>(bounded_y),
            },
            {
                std::min(clip_width, target_extent.width - bounded_x),
                std::min(clip_height, target_extent.height - bounded_y),
            },
        };
    }

    static bool draw_has_supported_host_transform(const NativeDraw& draw) {
        const uint32_t mode = draw.transform_execution_mode & 3u;
        return mode == 2u
            || (mode == 0u && fixed_function_composite_matrix_valid(draw));
    }

    const std::vector<RenderTargetFeedbackSpec>&
    presented_render_target_feedback_specs() const {
        if (feedback_spec_cache_generation_ == render_work_generation_) {
            ++feedback_spec_cache_hit_count_;
            return feedback_spec_cache_;
        }
        const auto build_begin = std::chrono::steady_clock::now();
        feedback_spec_cache_.clear();
        std::vector<RenderTargetFeedbackSpec>& specs = feedback_spec_cache_;
        auto append_unique = [&](RenderTargetFeedbackSpec spec) {
            if (spec.address == 0u
                || spec.width == 0u
                || spec.height == 0u) {
                return;
            }
            const auto duplicate = std::find_if(
                specs.begin(),
                specs.end(),
                [&](const RenderTargetFeedbackSpec& existing) {
                    return existing.address == spec.address
                        && existing.width == spec.width
                        && existing.height == spec.height
                        && existing.format == spec.format;
                });
            if (duplicate == specs.end()) {
                specs.push_back(std::move(spec));
            }
        };

        const size_t first_draw = std::min<size_t>(
            interpreted_stream_.presented_draw_begin,
            interpreted_stream_.draws.size());
        const size_t end_draw = std::min<size_t>(
            first_draw + interpreted_stream_.presented_draw_count,
            interpreted_stream_.draws.size());
        for (size_t draw_index = first_draw; draw_index < end_draw; ++draw_index) {
            const size_t presented_index = draw_index - first_draw;
            if (presented_index < options_.presented_draw_begin
                || presented_index >= options_.presented_draw_end) {
                continue;
            }
            const NativeDraw& draw = interpreted_stream_.draws[draw_index];
            if (!draw_targets_presented_surface(draw)) {
                continue;
            }
            const auto [width, height] = draw_surface_extent(draw);
            std::string format = "A8R8G8B8_LINEAR";
            if (draw.texture_enabled
                && draw.texture_stage < draw.texture_formats.size()
                && ((draw.texture_formats[draw.texture_stage] >> 8u) & 0xFFu)
                    == 0x1Eu) {
                format = "X8R8G8B8_LINEAR";
            }
            append_unique({
                draw.surface_color_offset,
                width,
                height,
                format,
            });

            if (!draw.texture_enabled
                || draw.texture_address == 0u
                || draw.texture_stage >= draw.texture_formats.size()) {
                continue;
            }
            const uint32_t format_raw =
                draw.texture_formats[draw.texture_stage];
            if (!nv2a_texture_format_is_linear(format_raw)) {
                continue;
            }
            const auto [texture_width, texture_height] = nv2a_texture_extent(
                format_raw,
                draw.texture_image_rects[draw.texture_stage]);
            const uint64_t surface_bytes = static_cast<uint64_t>(width)
                * static_cast<uint64_t>(height) * 4u;
            const uint64_t address_delta =
                draw.texture_address > draw.surface_color_offset
                ? static_cast<uint64_t>(
                    draw.texture_address - draw.surface_color_offset)
                : static_cast<uint64_t>(
                    draw.surface_color_offset - draw.texture_address);
            if (texture_width == width
                && texture_height == height
                && address_delta == surface_bytes) {
                append_unique({
                    draw.texture_address,
                    texture_width,
                    texture_height,
                    ((format_raw >> 8u) & 0xFFu) == 0x1Eu
                        ? "X8R8G8B8_LINEAR"
                        : "A8R8G8B8_LINEAR",
                });
            }
        }

        // Surface offsets and texture DMA offsets can name the same allocation
        // through different NV2A windows. Retain any offscreen surface that is
        // sampled later in the frame so the producer pass can be replayed into
        // the host texture instead of uploading stale CPU backing bytes.
        std::map<std::array<uint32_t, 3>, const NativeDraw*>
            latest_offscreen_producers;
        for (size_t consumer_index = first_draw;
             consumer_index < end_draw;
             ++consumer_index) {
            const NativeDraw& consumer =
                interpreted_stream_.draws[consumer_index];
            if (consumer.texture_enabled
                && consumer.texture_stage < consumer.texture_formats.size()
                && consumer.texture_address != 0u) {
                const uint32_t format_raw =
                    consumer.texture_formats[consumer.texture_stage];
                const uint32_t color_format = (format_raw >> 8u) & 0xFFu;
                std::string format;
                switch (color_format) {
                case 0x06u: format = "A8R8G8B8"; break;
                case 0x07u: format = "X8R8G8B8"; break;
                case 0x12u: format = "A8R8G8B8_LINEAR"; break;
                case 0x1Eu: format = "X8R8G8B8_LINEAR"; break;
                default: break;
                }
                if (!format.empty()) {
                    const auto [texture_width, texture_height] =
                        nv2a_texture_extent(
                            format_raw,
                            consumer.texture_image_rects[
                                consumer.texture_stage]);
                    const std::array<uint32_t, 3> producer_key{
                        nv2a_canonical_resource_address(
                            consumer.texture_address),
                        texture_width,
                        texture_height,
                    };
                    const auto producer = latest_offscreen_producers.find(
                        producer_key);
                    if (producer != latest_offscreen_producers.end()) {
                        append_unique({
                            consumer.texture_address,
                            texture_width,
                            texture_height,
                            std::move(format),
                            producer->second->surface_color_offset,
                            true,
                        });
                    }
                }
            }
            if (!draw_targets_presented_surface(consumer)) {
                const auto [surface_width, surface_height] =
                    draw_surface_extent(consumer);
                latest_offscreen_producers[{
                    nv2a_canonical_resource_address(
                        consumer.surface_color_offset),
                    surface_width,
                    surface_height,
                }] = &consumer;
            }
        }
        feedback_spec_cache_generation_ = render_work_generation_;
        ++feedback_spec_cache_build_count_;
        last_feedback_spec_build_us_ = std::chrono::duration_cast<
            std::chrono::microseconds>(
                std::chrono::steady_clock::now() - build_begin).count();
        return feedback_spec_cache_;
    }

    static bool render_target_feedback_texture_matches_spec(
        const HostTexture& texture,
        const RenderTargetFeedbackSpec& spec) {
        return texture.render_target_feedback
            && texture.guest_address == spec.address
            && texture.width == spec.width
            && texture.height == spec.height
            && texture.format == spec.format;
    }

    void cache_render_target_feedback_texture(HostTexture& texture) {
        if (!texture.render_target_feedback
            || texture.image == VK_NULL_HANDLE
            || texture.memory == VK_NULL_HANDLE
            || texture.view == VK_NULL_HANDLE) {
            return;
        }
        const auto duplicate = std::find_if(
            render_target_feedback_image_cache_.begin(),
            render_target_feedback_image_cache_.end(),
            [&](const HostTexture& cached) {
                return cached.guest_address == texture.guest_address
                    && cached.width == texture.width
                    && cached.height == texture.height
                    && cached.format == texture.format;
            });
        if (duplicate != render_target_feedback_image_cache_.end()) {
            destroy_host_texture(*duplicate);
            render_target_feedback_image_cache_.erase(duplicate);
            ++last_render_target_feedback_image_cache_eviction_count_;
            ++render_target_feedback_image_cache_eviction_count_;
        }
        render_target_feedback_image_cache_.push_back(std::move(texture));
        clear_moved_texture_handles(texture);
        ++last_render_target_feedback_image_cache_store_count_;
        ++render_target_feedback_image_cache_store_count_;
    }

    void trim_render_target_feedback_image_cache() {
        while (render_target_feedback_image_cache_.size()
               > kRenderTargetFeedbackImageCacheCapacity) {
            destroy_host_texture(render_target_feedback_image_cache_.front());
            render_target_feedback_image_cache_.erase(
                render_target_feedback_image_cache_.begin());
            ++last_render_target_feedback_image_cache_eviction_count_;
            ++render_target_feedback_image_cache_eviction_count_;
        }
    }

    bool render_target_feedback_refresh_required() const {
        const std::vector<RenderTargetFeedbackSpec>& specs =
            presented_render_target_feedback_specs();
        const size_t active_count = static_cast<size_t>(std::count_if(
            host_textures_.begin(),
            host_textures_.end(),
            [](const HostTexture& texture) {
                return texture.render_target_feedback;
            }));
        if (active_count != specs.size()) {
            return true;
        }
        return std::any_of(
            specs.begin(),
            specs.end(),
            [&](const RenderTargetFeedbackSpec& spec) {
                return std::none_of(
                    host_textures_.begin(),
                    host_textures_.end(),
                    [&](const HostTexture& texture) {
                        return render_target_feedback_texture_matches_spec(
                            texture,
                            spec);
                    });
            });
    }

    HostTexture create_render_target_feedback_texture(
        const RenderTargetFeedbackSpec& spec) {
        return create_host_texture(
            spec.address,
            spec.width,
            spec.height,
            spec.format,
            "render-target-feedback",
            std::vector<uint8_t>(
                static_cast<size_t>(spec.width) * spec.height * 4u,
                0u),
            swapchain_format_,
            true);
    }

    void destroy_offscreen_render_targets() {
        for (OffscreenRenderTarget& target : offscreen_render_targets_) {
            if (target.framebuffer != VK_NULL_HANDLE) {
                vkDestroyFramebuffer(device_, target.framebuffer, nullptr);
                target.framebuffer = VK_NULL_HANDLE;
            }
            destroy_depth_attachment(
                target.depth_image,
                target.depth_memory,
                target.depth_view);
            target.color_view = VK_NULL_HANDLE;
        }
        offscreen_render_targets_.clear();
    }

    bool offscreen_render_targets_match_presented_specs() const {
        const std::vector<RenderTargetFeedbackSpec>& specs =
            presented_render_target_feedback_specs();
        std::vector<RenderTargetFeedbackSpec> expected;
        for (const RenderTargetFeedbackSpec& spec : specs) {
            if (spec.offscreen_produced) {
                expected.push_back(spec);
            }
        }
        if (expected.size() != offscreen_render_targets_.size()) {
            return false;
        }
        for (size_t index = 0; index < expected.size(); ++index) {
            const OffscreenRenderTarget& target = offscreen_render_targets_[index];
            if (!(target.spec == expected[index])
                || target.framebuffer == VK_NULL_HANDLE
                || target.color_view == VK_NULL_HANDLE
                || target.depth_image == VK_NULL_HANDLE
                || target.depth_memory == VK_NULL_HANDLE
                || target.depth_view == VK_NULL_HANDLE) {
                return false;
            }
            const auto backing_texture = std::find_if(
                host_textures_.begin(),
                host_textures_.end(),
                [&](const HostTexture& texture) {
                    return texture.image != VK_NULL_HANDLE
                        && texture.memory != VK_NULL_HANDLE
                        && texture.view == target.color_view
                        && render_target_feedback_texture_matches_spec(
                            texture,
                            expected[index]);
                });
            if (backing_texture == host_textures_.end()) {
                return false;
            }
        }
        return true;
    }

    void create_offscreen_render_targets() {
        destroy_offscreen_render_targets();
        const std::vector<RenderTargetFeedbackSpec>& specs =
            presented_render_target_feedback_specs();
        offscreen_render_targets_.reserve(specs.size());
        for (const RenderTargetFeedbackSpec& spec : specs) {
            if (!spec.offscreen_produced) {
                continue;
            }
            const auto texture = std::find_if(
                host_textures_.begin(),
                host_textures_.end(),
                [&](const HostTexture& candidate) {
                    return candidate.render_target_feedback
                        && nv2a_canonical_resource_address(
                            candidate.guest_address)
                            == nv2a_canonical_resource_address(spec.address)
                        && candidate.width == spec.width
                        && candidate.height == spec.height;
                });
            if (texture == host_textures_.end()) {
                continue;
            }
            OffscreenRenderTarget target{};
            target.spec = spec;
            target.color_view = texture->view;
            try {
                create_depth_attachment(
                    spec.width,
                    spec.height,
                    target.depth_image,
                    target.depth_memory,
                    target.depth_view);
                const std::array<VkImageView, 2> attachments = {
                    target.color_view,
                    target.depth_view,
                };
                VkFramebufferCreateInfo create_info{};
                create_info.sType = VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO;
                create_info.renderPass = render_pass_;
                create_info.attachmentCount = static_cast<uint32_t>(
                    attachments.size());
                create_info.pAttachments = attachments.data();
                create_info.width = spec.width;
                create_info.height = spec.height;
                create_info.layers = 1;
                vk_check(
                    vkCreateFramebuffer(
                        device_,
                        &create_info,
                        nullptr,
                        &target.framebuffer),
                    "vkCreateFramebuffer(offscreen)");
            } catch (...) {
                destroy_depth_attachment(
                    target.depth_image,
                    target.depth_memory,
                    target.depth_view);
                throw;
            }
            offscreen_render_targets_.push_back(std::move(target));
        }
        log_.emit(
            "nv2a_offscreen_render_targets_created",
            {
                {"count", std::to_string(offscreen_render_targets_.size())},
                {"dedicated_depth_count", std::to_string(
                    std::count_if(
                        offscreen_render_targets_.begin(),
                        offscreen_render_targets_.end(),
                        [](const OffscreenRenderTarget& target) {
                            return target.depth_view != VK_NULL_HANDLE;
                        }))},
                {"shares_presented_depth", json_bool(false)},
            });
    }

    void destroy_host_texture_bindings() {
        if (texture_descriptor_pool_) {
            vkDestroyDescriptorPool(device_, texture_descriptor_pool_, nullptr);
            texture_descriptor_pool_ = VK_NULL_HANDLE;
        }
        for (const HostTextureBinding& binding : host_texture_bindings_) {
            if (binding.sampler) {
                vkDestroySampler(device_, binding.sampler, nullptr);
            }
        }
        host_texture_bindings_.clear();
    }

    size_t host_texture_index_for_draw(const NativeDraw& draw) const {
        const auto match = std::find_if(
            host_textures_.begin(),
            host_textures_.end(),
            [&](const HostTexture& texture) {
                return host_texture_matches_draw(texture, draw);
            });
        return match == host_textures_.end()
            ? 0u
            : static_cast<size_t>(match - host_textures_.begin());
    }

    std::vector<HostTextureBindingSpec>
    required_host_texture_bindings() const {
        std::vector<HostTextureBindingSpec> required{{0u, 0u, 0u, 0u, 0u}};
        const size_t first_draw = std::min<size_t>(
            interpreted_stream_.presented_draw_begin,
            interpreted_stream_.draws.size());
        const size_t end_draw = std::min<size_t>(
            first_draw + interpreted_stream_.presented_draw_count,
            interpreted_stream_.draws.size());
        const std::vector<RenderTargetFeedbackSpec>& feedback_specs =
            presented_render_target_feedback_specs();
        for (size_t draw_index = first_draw; draw_index < end_draw; ++draw_index) {
            const size_t presented_index = draw_index - first_draw;
            if (presented_index < options_.presented_draw_begin
                || presented_index >= options_.presented_draw_end) {
                continue;
            }
            const NativeDraw& draw = interpreted_stream_.draws[draw_index];
            const bool targets_offscreen_feedback = std::any_of(
                feedback_specs.begin(),
                feedback_specs.end(),
                [&](const RenderTargetFeedbackSpec& target) {
                    return target.offscreen_produced
                        && nv2a_canonical_resource_address(
                            target.producer_address)
                            == nv2a_canonical_resource_address(
                                draw.surface_color_offset);
                });
            if ((!draw_targets_presented_surface(draw)
                    && !targets_offscreen_feedback)
                || !draw_has_supported_host_transform(draw)
                || (draw.primitive != 5u && draw.primitive != 6u)
                || draw.vertex_count == 0u) {
                continue;
            }
            const size_t texture_index = host_texture_index_for_draw(draw);
            const uint32_t address = draw.texture_enabled
                    && draw.texture_stage < draw.texture_addresses.size()
                ? draw.texture_addresses[draw.texture_stage]
                : 0u;
            const uint32_t stage = std::min<uint32_t>(
                draw.texture_stage, 3u);
            const HostTextureBindingSpec spec{
                texture_index,
                address,
                draw.texture_formats[stage],
                draw.texture_controls[stage],
                draw.texture_filters[stage],
            };
            if (std::find(required.begin(), required.end(), spec)
                == required.end()) {
                required.push_back(spec);
            }
        }
        return required;
    }

    void refresh_host_texture_bindings() {
        const auto update_begin = std::chrono::steady_clock::now();
        last_texture_binding_update_us_ = 0;
        last_texture_binding_set_reused_ = false;
        last_texture_binding_image_descriptor_update_count_ = 0u;
        last_texture_binding_descriptor_set_allocation_count_ = 0u;
        if (host_textures_.empty()) {
            destroy_host_texture_bindings();
            last_texture_binding_update_us_ = std::chrono::duration_cast<
                std::chrono::microseconds>(
                    std::chrono::steady_clock::now() - update_begin).count();
            return;
        }
        const std::vector<HostTextureBindingSpec> required =
            required_host_texture_bindings();
        const bool layout_unchanged = texture_descriptor_pool_ != VK_NULL_HANDLE
            && required.size() == host_texture_bindings_.size()
            && std::equal(
                required.begin(), required.end(),
                host_texture_bindings_.begin(),
                [&](const auto& spec, const HostTextureBinding& binding) {
                    return spec.texture_index == binding.texture_index
                        && spec.address == binding.address
                        && spec.format == binding.format
                        && spec.control == binding.control
                        && spec.filter == binding.filter
                        && spec.texture_index < host_textures_.size()
                        && binding.texture_mip_levels
                            == host_textures_[spec.texture_index].mip_levels;
                });
        if (layout_unchanged) {
            uint32_t image_descriptor_update_count = 0u;
            uint32_t repeat_binding_count = 0u;
            uint32_t mirrored_repeat_binding_count = 0u;
            for (HostTextureBinding& binding : host_texture_bindings_) {
                const HostTexture& texture =
                    host_textures_[binding.texture_index];
                if (binding.texture_view != texture.view) {
                    VkDescriptorImageInfo image_info{
                        binding.sampler,
                        texture.view,
                        VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL};
                    VkWriteDescriptorSet write{};
                    write.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
                    write.dstSet = binding.descriptor_set;
                    write.dstBinding = 0u;
                    write.descriptorCount = 1u;
                    write.descriptorType =
                        VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
                    write.pImageInfo = &image_info;
                    vkUpdateDescriptorSets(
                        device_, 1u, &write, 0u, nullptr);
                    binding.texture_view = texture.view;
                    ++image_descriptor_update_count;
                }
                const uint32_t u_mode = binding.address & 7u;
                const uint32_t v_mode = (binding.address >> 8u) & 7u;
                repeat_binding_count +=
                    u_mode == 1u || v_mode == 1u ? 1u : 0u;
                mirrored_repeat_binding_count +=
                    u_mode == 2u || v_mode == 2u ? 1u : 0u;
            }
            ++texture_binding_set_reuse_count_;
            texture_binding_image_descriptor_update_count_ +=
                image_descriptor_update_count;
            last_texture_binding_set_reused_ = true;
            last_texture_binding_image_descriptor_update_count_ =
                image_descriptor_update_count;
            last_texture_binding_update_us_ = std::chrono::duration_cast<
                std::chrono::microseconds>(
                    std::chrono::steady_clock::now() - update_begin).count();
            log_.emit(
                "nv2a_texture_sampler_bindings_refreshed",
                {
                    {"bindings", std::to_string(host_texture_bindings_.size())},
                    {"binding_set_reused", json_bool(true)},
                    {"image_descriptor_updates", std::to_string(
                        image_descriptor_update_count)},
                    {"descriptor_sets_allocated", "0"},
                    {"update_us", std::to_string(
                        last_texture_binding_update_us_)},
                    {"repeat_bindings", std::to_string(repeat_binding_count)},
                    {"mirrored_repeat_bindings", std::to_string(
                        mirrored_repeat_binding_count)},
                });
            return;
        }
        destroy_host_texture_bindings();
        host_texture_bindings_.reserve(required.size());
        for (const HostTextureBindingSpec& spec : required) {
            HostTextureBinding binding{};
            binding.texture_index = spec.texture_index;
            binding.address = spec.address;
            binding.format = spec.format;
            binding.control = spec.control;
            binding.filter = spec.filter;
            binding.texture_mip_levels =
                host_textures_[spec.texture_index].mip_levels;
            binding.texture_view = host_textures_[spec.texture_index].view;
            VkSamplerCreateInfo sampler_info{};
            sampler_info.sType = VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO;
            const uint32_t min_filter = (spec.filter >> 16u) & 0xFFu;
            const uint32_t mag_filter = (spec.filter >> 24u) & 0x0Fu;
            sampler_info.magFilter = mag_filter == 1u
                ? VK_FILTER_NEAREST : VK_FILTER_LINEAR;
            sampler_info.minFilter = min_filter == 1u
                    || min_filter == 3u || min_filter == 5u
                ? VK_FILTER_NEAREST : VK_FILTER_LINEAR;
            sampler_info.mipmapMode = min_filter == 5u || min_filter == 6u
                ? VK_SAMPLER_MIPMAP_MODE_LINEAR
                : VK_SAMPLER_MIPMAP_MODE_NEAREST;
            sampler_info.addressModeU = nv2a_sampler_address_mode(spec.address);
            sampler_info.addressModeV = nv2a_sampler_address_mode(spec.address >> 8u);
            sampler_info.addressModeW = nv2a_sampler_address_mode(spec.address >> 16u);
            sampler_info.borderColor = VK_BORDER_COLOR_FLOAT_TRANSPARENT_BLACK;
            const HostTexture& texture = host_textures_[spec.texture_index];
            const uint32_t requested_mip_levels =
                (spec.format >> 16u) & 0x0Fu;
            const uint32_t available_mip_levels = std::min(
                texture.mip_levels,
                std::max(requested_mip_levels, 1u));
            sampler_info.maxLod = min_filter >= 3u
                ? static_cast<float>(available_mip_levels - 1u)
                : 0.0f;
            int32_t lod_bias = static_cast<int32_t>(spec.filter & 0x1FFFu);
            if ((lod_bias & 0x1000) != 0) {
                lod_bias -= 0x2000;
            }
            sampler_info.mipLodBias = static_cast<float>(lod_bias) / 256.0f;
            vk_check(
                vkCreateSampler(
                    device_, &sampler_info, nullptr, &binding.sampler),
                "vkCreateSampler(texture binding)");
            host_texture_bindings_.push_back(binding);
        }

        const uint32_t descriptor_count = static_cast<uint32_t>(
            host_texture_bindings_.size());
        std::array<VkDescriptorPoolSize, 2> pool_sizes{{
            {
                VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER,
                descriptor_count,
            },
            {
                VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,
                descriptor_count * 4u,
            },
        }};
        VkDescriptorPoolCreateInfo pool_info{};
        pool_info.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO;
        pool_info.maxSets = descriptor_count;
        pool_info.poolSizeCount = static_cast<uint32_t>(pool_sizes.size());
        pool_info.pPoolSizes = pool_sizes.data();
        vk_check(
            vkCreateDescriptorPool(
                device_, &pool_info, nullptr, &texture_descriptor_pool_),
            "vkCreateDescriptorPool");
        std::vector<VkDescriptorSetLayout> layouts(
            host_texture_bindings_.size(), texture_descriptor_layout_);
        std::vector<VkDescriptorSet> descriptor_sets(
            host_texture_bindings_.size());
        VkDescriptorSetAllocateInfo descriptor_allocation{};
        descriptor_allocation.sType =
            VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO;
        descriptor_allocation.descriptorPool = texture_descriptor_pool_;
        descriptor_allocation.descriptorSetCount = descriptor_count;
        descriptor_allocation.pSetLayouts = layouts.data();
        vk_check(
            vkAllocateDescriptorSets(
                device_, &descriptor_allocation, descriptor_sets.data()),
            "vkAllocateDescriptorSets");
        descriptor_allocation_count_ += descriptor_count;
        uint32_t repeat_binding_count = 0u;
        uint32_t mirrored_repeat_binding_count = 0u;
        for (size_t index = 0; index < host_texture_bindings_.size(); ++index) {
            HostTextureBinding& binding = host_texture_bindings_[index];
            binding.descriptor_set = descriptor_sets[index];
            const HostTexture& texture = host_textures_[binding.texture_index];
            VkDescriptorImageInfo image_info{
                binding.sampler,
                texture.view,
                VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL};
            VkDescriptorBufferInfo fragment_buffer_info{
                fragment_state_buffer_,
                0,
                fragment_state_buffer_size_};
            VkDescriptorBufferInfo vertex_program_buffer_info{
                vertex_program_state_buffer_,
                0,
                vertex_program_state_buffer_size_};
            VkDescriptorBufferInfo vertex_buffer_info{
                vertex_buffer_,
                0,
                vertex_buffer_size_};
            VkDescriptorBufferInfo raw_resource_buffer_info{
                raw_vertex_resource_buffer_,
                0,
                raw_vertex_resource_buffer_size_};
            std::array<VkWriteDescriptorSet, 5> writes{};
            writes[0].sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
            writes[0].dstSet = binding.descriptor_set;
            writes[0].dstBinding = 0;
            writes[0].descriptorCount = 1;
            writes[0].descriptorType =
                VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
            writes[0].pImageInfo = &image_info;
            writes[1].sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
            writes[1].dstSet = binding.descriptor_set;
            writes[1].dstBinding = 1;
            writes[1].descriptorCount = 1;
            writes[1].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
            writes[1].pBufferInfo = &fragment_buffer_info;
            writes[2].sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
            writes[2].dstSet = binding.descriptor_set;
            writes[2].dstBinding = 2;
            writes[2].descriptorCount = 1;
            writes[2].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
            writes[2].pBufferInfo = &vertex_program_buffer_info;
            writes[3].sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
            writes[3].dstSet = binding.descriptor_set;
            writes[3].dstBinding = 3;
            writes[3].descriptorCount = 1;
            writes[3].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
            writes[3].pBufferInfo = &vertex_buffer_info;
            writes[4].sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
            writes[4].dstSet = binding.descriptor_set;
            writes[4].dstBinding = 4;
            writes[4].descriptorCount = 1;
            writes[4].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
            writes[4].pBufferInfo = &raw_resource_buffer_info;
            vkUpdateDescriptorSets(
                device_,
                static_cast<uint32_t>(writes.size()),
                writes.data(),
                0,
                nullptr);
            const uint32_t u_mode = binding.address & 7u;
            const uint32_t v_mode = (binding.address >> 8u) & 7u;
            repeat_binding_count += u_mode == 1u || v_mode == 1u ? 1u : 0u;
            mirrored_repeat_binding_count +=
                u_mode == 2u || v_mode == 2u ? 1u : 0u;
        }
        ++texture_binding_set_rebuild_count_;
        texture_binding_image_descriptor_update_count_ += descriptor_count;
        texture_binding_descriptor_set_allocation_count_ += descriptor_count;
        last_texture_binding_image_descriptor_update_count_ = descriptor_count;
        last_texture_binding_descriptor_set_allocation_count_ =
            descriptor_count;
        last_texture_binding_update_us_ = std::chrono::duration_cast<
            std::chrono::microseconds>(
                std::chrono::steady_clock::now() - update_begin).count();
        log_.emit(
            "nv2a_texture_sampler_bindings_refreshed",
            {
                {"bindings", std::to_string(host_texture_bindings_.size())},
                {"binding_set_reused", json_bool(false)},
                {"image_descriptor_updates", std::to_string(
                    descriptor_count)},
                {"descriptor_sets_allocated", std::to_string(
                    descriptor_count)},
                {"update_us", std::to_string(
                    last_texture_binding_update_us_)},
                {"repeat_bindings", std::to_string(repeat_binding_count)},
                {"mirrored_repeat_bindings", std::to_string(mirrored_repeat_binding_count)},
            });
    }

    void refresh_host_textures(bool retain_unlisted_resources = false) {
        const auto refresh_begin = std::chrono::steady_clock::now();
        last_texture_refresh_us_ = 0u;
        last_texture_indexed_lookup_count_ = 0u;
        last_texture_indexed_lookup_candidate_count_ = 0u;
        last_texture_constant_lookup_count_ = 0u;
        last_render_target_feedback_image_cache_hit_count_ = 0u;
        last_render_target_feedback_image_cache_miss_count_ = 0u;
        last_render_target_feedback_image_cache_store_count_ = 0u;
        last_render_target_feedback_image_cache_eviction_count_ = 0u;
        std::vector<HostTexture> previous = std::move(host_textures_);
        host_textures_.clear();
        last_render_target_feedback_pruned_count_ = 0u;
        const std::vector<RenderTargetFeedbackSpec>& feedback_specs =
            presented_render_target_feedback_specs();
        host_textures_.reserve(
            recovered_source_.textures.size()
                + feedback_specs.size()
                + previous.size()
                + 1u);
        uint32_t reused_texture_count = 0;
        uint32_t uploaded_texture_count = 0;
        uint32_t feedback_texture_count = 0;
        uint32_t gpu_converted_texture_count = 0;
        uint32_t cpu_converted_texture_count = 0;
        uint64_t cpu_texture_path_us = 0u;
        std::vector<GpuTextureConversionJob> gpu_conversion_jobs;
        std::unordered_map<uint32_t, std::vector<size_t>>
            previous_indices_by_address;
        previous_indices_by_address.reserve(previous.size() * 2u + 1u);
        for (size_t index = 0u; index < previous.size(); ++index) {
            previous_indices_by_address[previous[index].guest_address]
                .push_back(index);
        }
        std::unordered_map<uint32_t, std::vector<size_t>>
            active_indices_by_address;
        active_indices_by_address.reserve(
            recovered_source_.textures.size() * 2u + 1u);
        std::unordered_set<uint32_t> feedback_canonical_addresses;
        feedback_canonical_addresses.reserve(feedback_specs.size() * 2u + 1u);
        for (const RenderTargetFeedbackSpec& spec : feedback_specs) {
            feedback_canonical_addresses.insert(
                nv2a_canonical_resource_address(spec.address));
        }

        const auto find_previous_index =
            [&](uint32_t address, const auto& matches) -> size_t {
                ++last_texture_indexed_lookup_count_;
                const auto bucket = previous_indices_by_address.find(address);
                if (bucket == previous_indices_by_address.end()) {
                    return previous.size();
                }
                for (const size_t index : bucket->second) {
                    ++last_texture_indexed_lookup_candidate_count_;
                    if (matches(previous[index])) {
                        return index;
                    }
                }
                return previous.size();
            };
        const auto index_active_texture = [&]() {
            const size_t index = host_textures_.size() - 1u;
            const HostTexture& texture = host_textures_[index];
            if (!texture.render_target_feedback) {
                active_indices_by_address[texture.guest_address]
                    .push_back(index);
            }
        };
        const auto active_texture_already_present =
            [&](uint32_t address,
                uint32_t width,
                uint32_t height,
                const std::string& format,
                const std::string& content_hash) {
                ++last_texture_indexed_lookup_count_;
                const auto bucket = active_indices_by_address.find(address);
                if (bucket == active_indices_by_address.end()) {
                    return false;
                }
                for (const size_t index : bucket->second) {
                    ++last_texture_indexed_lookup_candidate_count_;
                    const HostTexture& texture = host_textures_[index];
                    if (texture.width == width
                        && texture.height == height
                        && texture.format == format
                        && texture.content_hash == content_hash) {
                        return true;
                    }
                }
                return false;
            };

        auto retain_matching = [&](uint32_t address,
                                   uint32_t width,
                                   uint32_t height,
                                   const std::string& format,
                                   const std::string& content_hash) -> bool {
            const size_t match = find_previous_index(
                address,
                [&](const HostTexture& texture) {
                    return texture.image != VK_NULL_HANDLE
                        && texture.width == width
                        && texture.height == height
                        && texture.format == format
                        && texture.content_hash == content_hash;
                });
            if (match == previous.size()) {
                return false;
            }
            host_textures_.push_back(std::move(previous[match]));
            clear_moved_texture_handles(previous[match]);
            index_active_texture();
            ++reused_texture_count;
            return true;
        };

        if (!retain_matching(0u, 1u, 1u, "fallback", "white")) {
            host_textures_.push_back(create_host_texture(
                0u, 1u, 1u, "fallback", "white", {255, 255, 255, 255}));
            index_active_texture();
            ++uploaded_texture_count;
        }
        for (const RenderTargetFeedbackSpec& spec : feedback_specs) {
            const size_t retained = find_previous_index(
                spec.address,
                [&](const HostTexture& texture) {
                    return texture.image != VK_NULL_HANDLE
                        && render_target_feedback_texture_matches_spec(
                            texture,
                            spec);
                });
            if (retained != previous.size()) {
                host_textures_.push_back(std::move(previous[retained]));
                clear_moved_texture_handles(previous[retained]);
                ++reused_texture_count;
                ++feedback_texture_count;
                continue;
            }
            const auto cached = std::find_if(
                render_target_feedback_image_cache_.begin(),
                render_target_feedback_image_cache_.end(),
                [&](const HostTexture& texture) {
                    return texture.image != VK_NULL_HANDLE
                        && texture.memory != VK_NULL_HANDLE
                        && texture.view != VK_NULL_HANDLE
                        && render_target_feedback_texture_matches_spec(
                            texture,
                            spec);
                });
            if (cached != render_target_feedback_image_cache_.end()) {
                host_textures_.push_back(std::move(*cached));
                clear_moved_texture_handles(*cached);
                render_target_feedback_image_cache_.erase(cached);
                ++reused_texture_count;
                ++feedback_texture_count;
                ++last_render_target_feedback_image_cache_hit_count_;
                ++render_target_feedback_image_cache_hit_count_;
                continue;
            }
            ++last_render_target_feedback_image_cache_miss_count_;
            ++render_target_feedback_image_cache_miss_count_;
            host_textures_.push_back(
                create_render_target_feedback_texture(spec));
            ++uploaded_texture_count;
            ++feedback_texture_count;
        }
        for (const RecoveredTextureResource& resource : recovered_source_.textures) {
            if (resource.format == "VERTEX_BUFFER") {
                continue;
            }
            ++last_texture_constant_lookup_count_;
            const bool replaced_by_feedback =
                feedback_canonical_addresses.find(
                    nv2a_canonical_resource_address(resource.address))
                != feedback_canonical_addresses.end();
            if (replaced_by_feedback) {
                continue;
            }
            const std::string content_identity = texture_content_identity(resource);
            const size_t rgba_size = static_cast<size_t>(resource.width)
                * resource.height * 4u;
            const size_t r5g6b5_size = static_cast<size_t>(resource.width)
                * resource.height * 2u;
            const bool supported = resource.format == "DXT1"
                || resource.format == "DXT5"
                || (resource.format == "R5G6B5"
                    && resource.payload.size() >= r5g6b5_size)
                || resource.payload.size() >= rgba_size;
            if (!supported) {
                ++unsupported_texture_resource_count_;
                log_.emit(
                    "unsupported_texture_resource",
                    {
                        {"address", std::to_string(resource.address)},
                        {"format", json_string(resource.format)},
                        {"width", std::to_string(resource.width)},
                        {"height", std::to_string(resource.height)},
                        {"payload_bytes", std::to_string(resource.payload.size())},
                    });
                continue;
            }
            const bool already_retained = active_texture_already_present(
                resource.address,
                resource.width,
                resource.height,
                resource.format,
                content_identity);
            if (already_retained
                || retain_matching(
                    resource.address,
                    resource.width,
                    resource.height,
                    resource.format,
                    content_identity)) {
                continue;
            }
            GpuTextureConversionJob gpu_job{};
            if (build_gpu_texture_conversion_job(
                    resource,
                    host_textures_.size(),
                    gpu_job)
                && ensure_texture_conversion_pipeline()) {
                HostTexture placeholder{};
                placeholder.guest_address = resource.address;
                placeholder.width = resource.width;
                placeholder.height = resource.height;
                placeholder.format = resource.format;
                placeholder.content_hash = content_identity;
                host_textures_.push_back(std::move(placeholder));
                index_active_texture();
                gpu_conversion_jobs.push_back(std::move(gpu_job));
                ++uploaded_texture_count;
                continue;
            }
            const auto cpu_begin = std::chrono::steady_clock::now();
            std::optional<HostTexture> texture =
                create_cpu_converted_host_texture(
                    resource, content_identity);
            cpu_texture_path_us += std::chrono::duration_cast<
                std::chrono::microseconds>(
                    std::chrono::steady_clock::now() - cpu_begin).count();
            if (!texture.has_value()) {
                ++unsupported_texture_resource_count_;
                continue;
            }
            host_textures_.push_back(std::move(*texture));
            index_active_texture();
            ++uploaded_texture_count;
            ++cpu_converted_texture_count;
        }
        if (!gpu_conversion_jobs.empty()) {
            if (execute_gpu_texture_conversion_batch(gpu_conversion_jobs)) {
                gpu_converted_texture_count = static_cast<uint32_t>(
                    gpu_conversion_jobs.size());
            } else {
                for (const GpuTextureConversionJob& job :
                     gpu_conversion_jobs) {
                    const auto cpu_begin = std::chrono::steady_clock::now();
                    std::optional<HostTexture> texture =
                        create_cpu_converted_host_texture(
                            *job.resource,
                            host_textures_[job.host_texture_index].content_hash);
                    cpu_texture_path_us += std::chrono::duration_cast<
                        std::chrono::microseconds>(
                            std::chrono::steady_clock::now() - cpu_begin).count();
                    if (!texture.has_value()) {
                        throw std::runtime_error(
                            "GPU texture conversion fallback rejected a validated resource");
                    }
                    host_textures_[job.host_texture_index] =
                        std::move(*texture);
                    ++cpu_converted_texture_count;
                }
            }
        }
        if (retain_unlisted_resources) {
            for (HostTexture& texture : previous) {
                if (texture.image == VK_NULL_HANDLE
                    || texture.render_target_feedback) {
                    continue;
                }
                ++last_texture_constant_lookup_count_;
                const bool replaced_by_feedback =
                    feedback_canonical_addresses.find(
                        nv2a_canonical_resource_address(
                            texture.guest_address))
                    != feedback_canonical_addresses.end();
                if (replaced_by_feedback) {
                    continue;
                }
                host_textures_.push_back(std::move(texture));
                clear_moved_texture_handles(texture);
                ++reused_texture_count;
            }
        }
        for (HostTexture& texture : previous) {
            if (texture.render_target_feedback
                && texture.image != VK_NULL_HANDLE) {
                ++last_render_target_feedback_pruned_count_;
                cache_render_target_feedback_texture(texture);
                continue;
            }
            destroy_host_texture(texture);
        }

        // Retained images keep their descriptor sets and samplers. The binding
        // refresh rewrites only image descriptors whose backing view changed;
        // layout or mip-count changes still rebuild the complete set.
        refresh_host_texture_bindings();
        // Descriptor updates above retire every reference to an inactive
        // feedback view. Capacity eviction is therefore safe only after the
        // active descriptor set no longer points at that image.
        trim_render_target_feedback_image_cache();
        last_texture_refresh_us_ = std::chrono::duration_cast<
            std::chrono::microseconds>(
                std::chrono::steady_clock::now() - refresh_begin).count();
        log_.emit(
            "nv2a_texture_resources_refreshed",
            {
                {"textures", std::to_string(host_textures_.size() - 1u)},
                {"reused", std::to_string(reused_texture_count)},
                {"uploaded", std::to_string(uploaded_texture_count)},
                {"gpu_converted", std::to_string(
                    gpu_converted_texture_count)},
                {"cpu_converted", std::to_string(
                    cpu_converted_texture_count)},
                {"cpu_texture_path_us", std::to_string(
                    cpu_texture_path_us)},
                {"refresh_us", std::to_string(last_texture_refresh_us_)},
                {"indexed_lookup_count", std::to_string(
                    last_texture_indexed_lookup_count_)},
                {"indexed_lookup_candidates", std::to_string(
                    last_texture_indexed_lookup_candidate_count_)},
                {"constant_lookup_count", std::to_string(
                    last_texture_constant_lookup_count_)},
                {"gpu_texture_path_us", std::to_string(
                    gpu_converted_texture_count == 0u
                        ? 0u
                        : last_gpu_texture_conversion_us_)},
                {"gpu_conversion_backend", json_string(
                    options_.cpu_texture_conversion
                        ? "cpu_forced"
                        : queue_family_.supports_compute
                            ? "compute"
                            : "cpu_queue_fallback")},
                {"render_target_feedback_textures", std::to_string(feedback_texture_count)},
                {"render_target_feedback_required", std::to_string(feedback_specs.size())},
                {"render_target_feedback_pruned", std::to_string(
                    last_render_target_feedback_pruned_count_)},
                {"render_target_feedback_image_cache_hits", std::to_string(
                    last_render_target_feedback_image_cache_hit_count_)},
                {"render_target_feedback_image_cache_misses", std::to_string(
                    last_render_target_feedback_image_cache_miss_count_)},
                {"render_target_feedback_image_cache_stores", std::to_string(
                    last_render_target_feedback_image_cache_store_count_)},
                {"render_target_feedback_image_cache_evictions", std::to_string(
                    last_render_target_feedback_image_cache_eviction_count_)},
                {"render_target_feedback_image_cache_resident", std::to_string(
                    render_target_feedback_image_cache_.size())},
                {"render_target_feedback_image_cache_capacity", std::to_string(
                    kRenderTargetFeedbackImageCacheCapacity)},
                {"render_target_feedback_image_cache_hits_total", std::to_string(
                    render_target_feedback_image_cache_hit_count_)},
                {"render_target_feedback_image_cache_misses_total", std::to_string(
                    render_target_feedback_image_cache_miss_count_)},
                {"render_target_feedback_image_cache_stores_total", std::to_string(
                    render_target_feedback_image_cache_store_count_)},
                {"render_target_feedback_image_cache_evictions_total", std::to_string(
                    render_target_feedback_image_cache_eviction_count_)},
            });
    }

    std::vector<NativeVertex> prepare_presented_vertices(
        bool collect_diagnostics = true) {
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
                        && nv2a_canonical_resource_address(
                            spec.producer_address)
                            == nv2a_canonical_resource_address(
                                local_draw.surface_color_offset);
                });
            if (!targets_presented
                && offscreen_spec == feedback_specs.end()) {
                continue;
            }
            if (local_draw.primitive != 5u && local_draw.primitive != 6u) {
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
                : local_draw.vertex_count >= 3u
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
                vertex.x = vertex.x * 2.0f
                    / static_cast<float>(target_width) - 1.0f;
                // A positive-height Vulkan viewport maps NDC -1 to the top
                // edge, matching the guest's top-left screen coordinates.
                vertex.y = vertex.y * 2.0f
                    / static_cast<float>(target_height) - 1.0f;
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

    void emit_presented_vertex_transform_diagnostics(bool emit_draw_details) {
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
            const RecoveredTextureResource* recovered_texture = nullptr;
            uint32_t texture_producer_draw_count = 0u;
            uint32_t texture_raw_producer_draw_count = 0u;
            uint32_t texture_first_producer_address = 0u;
            if (native_draw != nullptr) {
                enabled_texture_stage_count = static_cast<uint32_t>(
                    std::count_if(
                        native_draw->texture_controls.begin(),
                        native_draw->texture_controls.end(),
                        [](uint32_t control) {
                            return (control & (1u << 30u)) != 0u;
                        }));
                if (native_draw->texture_enabled) {
                    const auto [texture_width, texture_height] =
                        nv2a_texture_extent(
                            native_draw->texture_formats[texture_stage],
                            native_draw->texture_image_rects[texture_stage]);
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
                    {"host_texture_stage_coverage_complete", native_draw != nullptr ? json_bool(enabled_texture_stage_count <= 1u) : "null"},
                    {"shader_stage_program", native_draw != nullptr ? std::to_string(native_draw->shader_stage_program) : "null"},
                    {"texture_offsets", native_draw != nullptr ? json_u32_array(native_draw->texture_offsets) : "null"},
                    {"texture_addresses", native_draw != nullptr ? json_u32_array(native_draw->texture_addresses) : "null"},
                    {"texture_formats", native_draw != nullptr ? json_u32_array(native_draw->texture_formats) : "null"},
                    {"texture_controls", native_draw != nullptr ? json_u32_array(native_draw->texture_controls) : "null"},
                    {"texture_filters", native_draw != nullptr ? json_u32_array(native_draw->texture_filters) : "null"},
                    {"texture_resource_matched", native_draw != nullptr ? json_bool(recovered_texture != nullptr) : "null"},
                    {"texture_resource_payload_bytes", recovered_texture != nullptr ? std::to_string(recovered_texture->payload.size()) : "null"},
                    {"texture_resource_payload_all_zero", recovered_texture != nullptr ? json_bool(texture_payload_all_zero) : "null"},
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
                    {"polygon_offset_fill_enable", native_draw != nullptr ? std::to_string(native_draw->polygon_offset_fill_enable) : "null"},
                    {"polygon_offset_scale_factor", native_draw != nullptr ? json_float(float_from_u32(native_draw->polygon_offset_scale_factor)) : "null"},
                    {"polygon_offset_bias", native_draw != nullptr ? json_float(float_from_u32(native_draw->polygon_offset_bias)) : "null"},
                    {"combiner_stage_count", native_draw != nullptr ? std::to_string(native_draw->combiner_control & 0xFFu) : "null"},
                    {"host_register_combiner_applied", native_draw != nullptr ? json_bool(true) : "null"},
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

    void emit_presented_render_state_diagnostics() {
        const size_t first_draw = std::min<size_t>(
            interpreted_stream_.presented_draw_begin,
            interpreted_stream_.draws.size());
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
        uint32_t register_combiner_state_mismatch_draw_count = 0u;
        uint32_t fog_enabled_draw_count = 0u;
        uint32_t fog_factor_applied_draw_count = 0u;
        uint32_t fog_state_mismatch_draw_count = 0u;
        uint32_t multiple_texture_stage_draw_count = 0u;
        uint32_t texture_stage_coverage_mismatch_draw_count = 0u;
        uint32_t zero_payload_textured_draw_count = 0u;
        uint32_t unproduced_zero_payload_texture_draw_count = 0u;
        uint32_t aliased_gpu_produced_texture_draw_count = 0u;
        uint32_t offscreen_render_target_replay_draw_count = 0u;
        uint32_t fixed_function_draw_count = 0u;
        uint32_t fixed_function_transformed_draw_count = 0u;
        uint32_t fixed_function_raw_fallback_draw_count = 0u;
        uint32_t fixed_function_filtered_draw_count = 0u;
        uint64_t fixed_function_invalid_vertex_count = 0u;
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
            texture_stage_coverage_mismatch_draw_count +=
                enabled_texture_stage_count > 1u ? 1u : 0u;
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
                const auto resource = std::find_if(
                    recovered_source_.textures.begin(),
                    recovered_source_.textures.end(),
                    [&](const RecoveredTextureResource& candidate) {
                        return candidate.address == draw.texture_address;
                    });
                const bool all_zero =
                    resource != recovered_source_.textures.end()
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
                    const std::vector<RenderTargetFeedbackSpec>&
                        feedback_specs =
                            presented_render_target_feedback_specs();
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
        const std::vector<RenderTargetFeedbackSpec>& feedback_specs =
            presented_render_target_feedback_specs();
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
                {"register_combiner_state_mismatch_draw_count", std::to_string(register_combiner_state_mismatch_draw_count)},
                {"fog_enabled_draw_count", std::to_string(fog_enabled_draw_count)},
                {"fog_factor_applied_draw_count", std::to_string(fog_factor_applied_draw_count)},
                {"fog_state_mismatch_draw_count", std::to_string(fog_state_mismatch_draw_count)},
                {"multiple_texture_stage_draw_count", std::to_string(multiple_texture_stage_draw_count)},
                {"texture_stage_coverage_mismatch_draw_count", std::to_string(texture_stage_coverage_mismatch_draw_count)},
                {"zero_payload_textured_draw_count", std::to_string(zero_payload_textured_draw_count)},
                {"unproduced_zero_payload_texture_draw_count", std::to_string(unproduced_zero_payload_texture_draw_count)},
                {"aliased_gpu_produced_texture_draw_count", std::to_string(aliased_gpu_produced_texture_draw_count)},
                {"offscreen_render_target_replay_draw_count", std::to_string(offscreen_render_target_replay_draw_count)},
                {"render_target_feedback_required", std::to_string(
                    feedback_specs.size())},
                {"render_target_feedback_required_addresses",
                    feedback_addresses.str()},
                {"fixed_function_draw_count", std::to_string(fixed_function_draw_count)},
                {"fixed_function_transformed_draw_count", std::to_string(fixed_function_transformed_draw_count)},
                {"fixed_function_raw_fallback_draw_count", std::to_string(fixed_function_raw_fallback_draw_count)},
                {"fixed_function_filtered_draw_count", std::to_string(fixed_function_filtered_draw_count)},
                {"fixed_function_invalid_vertex_count", std::to_string(fixed_function_invalid_vertex_count)},
                {"continuation_bootstrap_used", json_bool(continuation_analysis_bootstrap_)},
                {"analysis_state_complete", json_bool(!continuation_analysis_bootstrap_)},
            });
    }

    void create_native_render_resources(
        bool create_textures = true,
        bool reuse_offscreen_render_targets = false) {
        const auto native_resource_begin = std::chrono::steady_clock::now();
        last_vertex_resource_prepare_us_ = 0u;
        last_state_resource_prepare_us_ = 0u;
        last_texture_resource_prepare_us_ = 0u;
        last_offscreen_resource_prepare_us_ = 0u;
        last_resource_bookkeeping_us_ = 0u;
        last_texture_refresh_us_ = 0u;
        last_texture_indexed_lookup_count_ = 0u;
        last_texture_indexed_lookup_candidate_count_ = 0u;
        last_texture_constant_lookup_count_ = 0u;
        last_render_target_feedback_image_cache_hit_count_ = 0u;
        last_render_target_feedback_image_cache_miss_count_ = 0u;
        last_render_target_feedback_image_cache_store_count_ = 0u;
        last_render_target_feedback_image_cache_eviction_count_ = 0u;
        if (create_textures) {
            // This diagnostic belongs to the resident resource generation.
            // Command-only reloads must not erase a failure from the texture
            // payload that is still installed.
            unsupported_texture_resource_count_ = 0;
        }
        last_vertex_map_us_ = 0;
        last_vertex_copy_us_ = 0;
        // Resource generations still run the lightweight validation below.
        // Do not also force the per-draw, per-vertex, and transform-token event
        // flood; sample that diagnostic detail at a fixed cadence instead.
        const bool diagnostic_sample_due = log_.enabled()
            && (!options_.live_render_stream
                || (options_.strict_render_validation
                    && (live_render_reload_count_ == 0u
                        || live_render_reload_count_ % 120u == 0u)));
        last_presented_diagnostics_sampled_ = diagnostic_sample_due;
        last_render_validation_us_ = 0;
        std::vector<NativeVertex> vertices = prepare_presented_vertices(
            diagnostic_sample_due);
        frontend_text_rectangle_count_ = append_frontend_text_vertices(
            vertices, recovered_frontend_text_);
        uploaded_vertex_count_ = static_cast<uint32_t>(vertices.size());
        const VkDeviceSize uploaded_size =
            vertices.size() * sizeof(NativeVertex);
        const VkDeviceSize required_size = std::max<VkDeviceSize>(
            uploaded_size,
            sizeof(NativeVertex));
        if (vertex_buffer_ != VK_NULL_HANDLE
            && required_size > vertex_buffer_size_) {
            destroy_host_texture_bindings();
            if (vertex_mapped_ != nullptr) {
                vkUnmapMemory(device_, vertex_memory_);
                vertex_mapped_ = nullptr;
            }
            vkDestroyBuffer(device_, vertex_buffer_, nullptr);
            vkFreeMemory(device_, vertex_memory_, nullptr);
            vertex_buffer_ = VK_NULL_HANDLE;
            vertex_memory_ = VK_NULL_HANDLE;
            vertex_buffer_size_ = 0u;
        }
        if (vertex_buffer_ == VK_NULL_HANDLE) {
            const VkDeviceSize allocation_size = options_.live_render_stream
                ? std::max<VkDeviceSize>(required_size, 1024u * 1024u)
                : required_size;
            vertex_buffer_size_ = allocation_size;
            create_buffer(
                vertex_buffer_size_,
                VK_BUFFER_USAGE_VERTEX_BUFFER_BIT
                    | VK_BUFFER_USAGE_STORAGE_BUFFER_BIT,
                VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT
                    | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT,
                vertex_buffer_,
                vertex_memory_);
        }
        if (!vertex_mapped_) {
            const auto map_begin = std::chrono::steady_clock::now();
            vk_check(
                vkMapMemory(
                    device_,
                    vertex_memory_,
                    0,
                    vertex_buffer_size_,
                    0,
                    &vertex_mapped_),
                "vkMapMemory(vertices)");
            last_vertex_map_us_ = std::chrono::duration_cast<
                std::chrono::microseconds>(
                    std::chrono::steady_clock::now() - map_begin).count();
        }
        if (uploaded_size != 0u) {
            const auto copy_begin = std::chrono::steady_clock::now();
            std::memcpy(
                vertex_mapped_,
                vertices.data(),
                static_cast<size_t>(uploaded_size));
            upload_bytes_ += static_cast<uint64_t>(uploaded_size);
            last_vertex_copy_us_ = std::chrono::duration_cast<
                std::chrono::microseconds>(
                    std::chrono::steady_clock::now() - copy_begin).count();
        } else {
            std::memset(vertex_mapped_, 0, sizeof(NativeVertex));
        }

        last_vertex_resource_prepare_us_ = std::chrono::duration_cast<
            std::chrono::microseconds>(
                std::chrono::steady_clock::now()
                - native_resource_begin).count();
        const auto state_resource_begin = std::chrono::steady_clock::now();
        refresh_raw_vertex_buffers();
        refresh_vertex_program_states();
        refresh_fragment_states();
        last_state_resource_prepare_us_ = std::chrono::duration_cast<
            std::chrono::microseconds>(
                std::chrono::steady_clock::now()
                - state_resource_begin).count();

        const auto texture_resource_begin = std::chrono::steady_clock::now();
        last_render_target_feedback_pruned_count_ = 0u;
        const bool feedback_refresh_required =
            render_target_feedback_refresh_required();
        if (reuse_offscreen_render_targets
            && !offscreen_render_targets_match_presented_specs()) {
            throw std::runtime_error(
                "cannot reuse offscreen targets with stale attachments");
        }
        if (create_textures || feedback_refresh_required) {
            refresh_host_textures(!create_textures);
        } else {
            refresh_host_texture_bindings();
        }
        if (reuse_offscreen_render_targets
            && !offscreen_render_targets_match_presented_specs()) {
            throw std::runtime_error(
                "offscreen target backing texture changed during refresh");
        }
        last_texture_resource_prepare_us_ = std::chrono::duration_cast<
            std::chrono::microseconds>(
                std::chrono::steady_clock::now()
                - texture_resource_begin).count();
        const auto offscreen_resource_begin =
            std::chrono::steady_clock::now();
        last_offscreen_render_targets_reused_ = reuse_offscreen_render_targets;
        if (!reuse_offscreen_render_targets) {
            create_offscreen_render_targets();
        } else {
            log_.emit(
                "nv2a_offscreen_render_targets_reused",
                {{"count", std::to_string(offscreen_render_targets_.size())}});
        }
        last_offscreen_resource_prepare_us_ = std::chrono::duration_cast<
            std::chrono::microseconds>(
                std::chrono::steady_clock::now()
                - offscreen_resource_begin).count();
        const auto resource_bookkeeping_begin =
            std::chrono::steady_clock::now();
        const std::vector<RenderTargetFeedbackSpec>& feedback_specs =
            presented_render_target_feedback_specs();
        const uint32_t render_target_feedback_texture_count =
            static_cast<uint32_t>(std::count_if(
                host_textures_.begin(),
                host_textures_.end(),
                [](const HostTexture& texture) {
                    return texture.render_target_feedback;
                }));
        const uint32_t missing_render_target_feedback_texture_count =
            static_cast<uint32_t>(std::count_if(
                feedback_specs.begin(),
                feedback_specs.end(),
                [&](const RenderTargetFeedbackSpec& spec) {
                    return std::none_of(
                        host_textures_.begin(),
                        host_textures_.end(),
                        [&](const HostTexture& texture) {
                            return render_target_feedback_texture_matches_spec(
                                texture,
                                spec);
                        });
                }));
        const uint32_t stale_render_target_feedback_texture_count =
            static_cast<uint32_t>(std::count_if(
                host_textures_.begin(),
                host_textures_.end(),
                [&](const HostTexture& texture) {
                    return texture.render_target_feedback
                        && std::none_of(
                            feedback_specs.begin(),
                            feedback_specs.end(),
                            [&](const RenderTargetFeedbackSpec& spec) {
                                return render_target_feedback_texture_matches_spec(
                                    texture,
                                    spec);
                            });
                }));
        std::ostringstream required_feedback_addresses;
        required_feedback_addresses << '[';
        for (size_t index = 0; index < feedback_specs.size(); ++index) {
            if (index != 0u) {
                required_feedback_addresses << ',';
            }
            required_feedback_addresses << feedback_specs[index].address;
        }
        required_feedback_addresses << ']';
        std::ostringstream active_feedback_addresses;
        active_feedback_addresses << '[';
        bool first_feedback_address = true;
        for (const HostTexture& texture : host_textures_) {
            if (!texture.render_target_feedback) {
                continue;
            }
            if (!first_feedback_address) {
                active_feedback_addresses << ',';
            }
            active_feedback_addresses << texture.guest_address;
            first_feedback_address = false;
        }
        active_feedback_addresses << ']';
        log_.emit(
            "nv2a_native_resources_created",
            {
                {"vertices", std::to_string(interpreted_stream_.vertices.size())},
                {"uploaded_vertices", std::to_string(uploaded_vertex_count_)},
                {"uploaded_vertex_base", std::to_string(uploaded_vertex_base_)},
                {"draws", std::to_string(interpreted_stream_.draws.size())},
                {"presented_draws", std::to_string(interpreted_stream_.presented_draw_count)},
                {"guest_flips", std::to_string(interpreted_stream_.flip_count)},
                {"manifest_guest_flip_count", std::to_string(current_manifest_guest_flip_count_)},
                {"manifest_guest_steps", std::to_string(current_manifest_guest_steps_)},
                {"textures", std::to_string(host_textures_.size() - 1u)},
                {"render_target_feedback_textures", std::to_string(render_target_feedback_texture_count)},
                {"render_target_feedback_required", std::to_string(feedback_specs.size())},
                {"render_target_feedback_missing", std::to_string(
                    missing_render_target_feedback_texture_count)},
                {"render_target_feedback_stale", std::to_string(
                    stale_render_target_feedback_texture_count)},
                {"render_target_feedback_pruned", std::to_string(
                    last_render_target_feedback_pruned_count_)},
                {"render_target_feedback_image_cache_hits", std::to_string(
                    last_render_target_feedback_image_cache_hit_count_)},
                {"render_target_feedback_image_cache_misses", std::to_string(
                    last_render_target_feedback_image_cache_miss_count_)},
                {"render_target_feedback_image_cache_stores", std::to_string(
                    last_render_target_feedback_image_cache_store_count_)},
                {"render_target_feedback_image_cache_evictions", std::to_string(
                    last_render_target_feedback_image_cache_eviction_count_)},
                {"render_target_feedback_image_cache_resident", std::to_string(
                    render_target_feedback_image_cache_.size())},
                {"render_target_feedback_image_cache_capacity", std::to_string(
                    kRenderTargetFeedbackImageCacheCapacity)},
                {"render_target_feedback_required_addresses",
                    required_feedback_addresses.str()},
                {"render_target_feedback_active_addresses",
                    active_feedback_addresses.str()},
                {"render_target_feedback_refreshed", json_bool(feedback_refresh_required)},
                {"offscreen_render_targets_reused", json_bool(
                    last_offscreen_render_targets_reused_)},
                {"presented_diagnostics_sampled", json_bool(
                    last_presented_diagnostics_sampled_)},
                {"presented_half_quad_recovered", json_bool(presented_half_quad_recovered_)},
                {"presented_overscan_height_recovered", json_bool(presented_overscan_height_recovered_)},
                {"presented_vertex_program_transformed_count", std::to_string(presented_vertex_program_transformed_count_)},
                {"presented_fixed_function_transformed_count", std::to_string(presented_fixed_function_transformed_count_)},
                {"presented_linear_texture_normalized_vertex_count", std::to_string(presented_linear_texture_normalized_vertex_count_)},
                {"offscreen_render_target_draw_count", std::to_string(offscreen_render_target_draw_count_)},
                {"offscreen_render_target_transformed_vertex_count", std::to_string(offscreen_render_target_transformed_vertex_count_)},
                {"presented_surface_color_offset", std::to_string(presented_surface_color_offset_)},
                {"indexed_array_draws", std::to_string(interpreted_stream_.indexed_array_draw_count)},
                {"indexed_array_elements", std::to_string(interpreted_stream_.indexed_array_element_count)},
                {"materialized_indexed_draws", std::to_string(interpreted_stream_.materialized_indexed_draw_count)},
                {"materialized_indexed_vertices", std::to_string(interpreted_stream_.materialized_indexed_vertex_count)},
                {"missing_indexed_resource_draws", std::to_string(interpreted_stream_.missing_indexed_resource_draw_count)},
                {"vertex_transform_us", std::to_string(last_vertex_transform_us_)},
                {"vertex_state_upload_us", std::to_string(
                    last_vertex_state_upload_us_)},
                {"raw_vertex_upload_us", std::to_string(
                    last_raw_vertex_upload_us_)},
                {"gpu_raw_attribute_draws", std::to_string(
                    gpu_raw_attribute_draw_count_)},
                {"gpu_raw_attribute_vertices", std::to_string(
                    gpu_raw_attribute_vertex_count_)},
                {"expanded_vertex_bytes_avoided", std::to_string(
                    static_cast<uint64_t>(
                        gpu_raw_attribute_vertex_count_)
                        * sizeof(NativeVertex))},
                {"gpu_vertex_program_draws", std::to_string(
                    gpu_vertex_program_draw_count_)},
                {"gpu_vertex_program_vertices", std::to_string(
                    gpu_vertex_program_vertex_count_)},
                {"cpu_vertex_program_fallback_draws", std::to_string(
                    cpu_vertex_program_fallback_draw_count_)},
                {"cpu_vertex_program_fallback_vertices", std::to_string(
                    cpu_vertex_program_fallback_vertex_count_)},
                {"vertex_map_us", std::to_string(last_vertex_map_us_)},
                {"vertex_copy_us", std::to_string(last_vertex_copy_us_)},
            });
        last_resource_bookkeeping_us_ = std::chrono::duration_cast<
            std::chrono::microseconds>(
                std::chrono::steady_clock::now()
                - resource_bookkeeping_begin).count();
        const auto render_validation_begin =
            std::chrono::steady_clock::now();
        if (!log_.enabled() && !options_.strict_render_validation) {
            return;
        }
        const size_t diagnostic_first_draw = std::min<size_t>(
            interpreted_stream_.presented_draw_begin,
            interpreted_stream_.draws.size());
        const size_t diagnostic_end_draw = std::min<size_t>(
            diagnostic_first_draw + interpreted_stream_.presented_draw_count,
            interpreted_stream_.draws.size());
        uint32_t textured_presented_draw_count = 0;
        uint32_t unmatched_presented_texture_draw_count = 0;
        uint32_t unsupported_presented_primitive_count = 0;
        uint32_t missing_presented_indexed_resource_draw_count = 0;
        uint32_t filtered_fixed_function_indexed_draw_count = 0;
        uint32_t filtered_fixed_function_draw_count = 0;
        uint32_t offscreen_render_target_draw_count = 0;
        uint32_t valid_geometry_draw_count = 0;
        uint32_t invalid_geometry_range_count = 0;
        uint32_t gpu_raw_attribute_draw_count = 0;
        uint32_t non_finite_position_draw_count = 0;
        uint32_t collapsed_x_draw_count = 0;
        uint32_t collapsed_y_draw_count = 0;
        uint32_t zero_area_draw_count = 0;
        uint32_t exact_center_origin_draw_count = 0;
        uint32_t fully_offscreen_draw_count = 0;
        uint32_t outside_viewport_draw_count = 0;
        uint64_t presented_vertex_count = 0;
        uint32_t fullscreen_draw_count = 0;
        uint32_t textured_fullscreen_draw_count = 0;
        uint32_t untextured_presented_draw_count = 0;
        uint32_t alpha_only_presented_draw_count = 0;
        uint32_t zero_alpha_presented_draw_count = 0;
        std::vector<uint32_t> presented_texture_addresses;
        int64_t first_zero_area_presented_index = -1;
        int64_t first_center_origin_presented_index = -1;
        bool overall_bounds_valid = false;
        float overall_min_x = 0.0f;
        float overall_max_x = 0.0f;
        float overall_min_y = 0.0f;
        float overall_max_y = 0.0f;
        const bool emit_presented_details = presented_diagnostics_valid_
            && presented_diagnostics_generation_ == render_work_generation_;
        if (emit_presented_details) {
            emit_presented_vertex_transform_diagnostics(true);
            emit_presented_render_state_diagnostics();
        }
        for (size_t draw_index = diagnostic_first_draw;
             draw_index < diagnostic_end_draw;
             ++draw_index) {
            const NativeDraw& draw = interpreted_stream_.draws[draw_index];
            if (!draw_targets_presented_surface(draw)) {
                ++offscreen_render_target_draw_count;
                continue;
            }
            const bool draw_textured = draw.texture_enabled
                && draw.texture_address != 0u;
            if (!draw_textured) {
                ++untextured_presented_draw_count;
            }
            const VkColorComponentFlags write_mask = nv2a_color_write_mask(
                draw.color_mask);
            if ((write_mask & VK_COLOR_COMPONENT_A_BIT) != 0u
                && (write_mask & (VK_COLOR_COMPONENT_R_BIT
                    | VK_COLOR_COMPONENT_G_BIT
                    | VK_COLOR_COMPONENT_B_BIT)) == 0u) {
                ++alpha_only_presented_draw_count;
            }
            if (draw.primitive != 5u && draw.primitive != 6u) {
                ++unsupported_presented_primitive_count;
            }
            if (draw.indexed_array && draw.vertex_count == 0u) {
                ++missing_presented_indexed_resource_draw_count;
            }
            if (!draw_has_supported_host_transform(draw)) {
                ++filtered_fixed_function_draw_count;
                filtered_fixed_function_indexed_draw_count +=
                    draw.indexed_array ? 1u : 0u;
            }
            if (draw.gpu_raw_attribute_fetch) {
                ++valid_geometry_draw_count;
                ++gpu_raw_attribute_draw_count;
                presented_vertex_count += draw.vertex_count;
            } else if (draw.vertex_count == 0u ||
                draw.first_vertex + draw.vertex_count > interpreted_stream_.vertices.size()) {
                ++invalid_geometry_range_count;
            } else {
                ++valid_geometry_draw_count;
                presented_vertex_count += draw.vertex_count;
                bool draw_bounds_valid = false;
                bool non_finite = false;
                bool exact_center_origin = true;
                float max_a = 0.0f;
                float min_x = 0.0f;
                float max_x = 0.0f;
                float min_y = 0.0f;
                float max_y = 0.0f;
                for (uint32_t vertex_index = 0; vertex_index < draw.vertex_count; ++vertex_index) {
                    const NativeVertex& vertex =
                        interpreted_stream_.vertices[draw.first_vertex + vertex_index];
                    if (!std::isfinite(vertex.x) || !std::isfinite(vertex.y)) {
                        non_finite = true;
                        exact_center_origin = false;
                        continue;
                    }
                    exact_center_origin = exact_center_origin
                        && vertex.raw_x_bits == 0x43A00000u
                        && (vertex.raw_y_bits & 0x7FFFFFFFu) == 0u;
                    max_a = std::max(max_a, vertex.a);
                    if (!draw_bounds_valid) {
                        min_x = max_x = vertex.x;
                        min_y = max_y = vertex.y;
                        draw_bounds_valid = true;
                    } else {
                        min_x = std::min(min_x, vertex.x);
                        max_x = std::max(max_x, vertex.x);
                        min_y = std::min(min_y, vertex.y);
                        max_y = std::max(max_y, vertex.y);
                    }
                }
                if (non_finite) {
                    ++non_finite_position_draw_count;
                }
                if (draw_bounds_valid) {
                    if (max_a <= 0.0001f) {
                        ++zero_alpha_presented_draw_count;
                    }
                    if (!overall_bounds_valid) {
                        overall_min_x = min_x;
                        overall_max_x = max_x;
                        overall_min_y = min_y;
                        overall_max_y = max_y;
                        overall_bounds_valid = true;
                    } else {
                        overall_min_x = std::min(overall_min_x, min_x);
                        overall_max_x = std::max(overall_max_x, max_x);
                        overall_min_y = std::min(overall_min_y, min_y);
                        overall_max_y = std::max(overall_max_y, max_y);
                    }
                    const bool collapsed_x = std::abs(max_x - min_x) <= 0.0001f;
                    const bool collapsed_y = std::abs(max_y - min_y) <= 0.0001f;
                    if (collapsed_x) {
                        ++collapsed_x_draw_count;
                    }
                    if (collapsed_y) {
                        ++collapsed_y_draw_count;
                    }
                    if (collapsed_x && collapsed_y) {
                        ++zero_area_draw_count;
                        if (first_zero_area_presented_index < 0) {
                            first_zero_area_presented_index = static_cast<int64_t>(
                                draw_index - diagnostic_first_draw);
                        }
                    }
                    if (exact_center_origin) {
                        ++exact_center_origin_draw_count;
                        if (first_center_origin_presented_index < 0) {
                            first_center_origin_presented_index = static_cast<int64_t>(
                                draw_index - diagnostic_first_draw);
                        }
                    }
                    if (max_x < 0.0f || min_x > static_cast<float>(options_.width)
                        || max_y < 0.0f || min_y > static_cast<float>(options_.height)) {
                        ++fully_offscreen_draw_count;
                    }
                    if (min_x < 0.0f || max_x > static_cast<float>(options_.width)
                        || min_y < 0.0f || max_y > static_cast<float>(options_.height)) {
                        ++outside_viewport_draw_count;
                    }
                    const bool fullscreen = min_x <= 0.5f
                        && max_x >= static_cast<float>(options_.width) - 0.5f
                        && min_y <= 0.5f
                        && max_y >= static_cast<float>(options_.height) - 0.5f;
                    if (fullscreen) {
                        ++fullscreen_draw_count;
                        if (draw_textured) {
                            ++textured_fullscreen_draw_count;
                        }
                    }
                }
            }
            if (!draw_textured) {
                continue;
            }
            ++textured_presented_draw_count;
            if (std::find(
                    presented_texture_addresses.begin(),
                    presented_texture_addresses.end(),
                    draw.texture_address) == presented_texture_addresses.end()) {
                presented_texture_addresses.push_back(draw.texture_address);
            }
            const bool texture_matched = std::any_of(
                host_textures_.begin() + 1,
                host_textures_.end(),
                [&](const HostTexture& texture) {
                    return host_texture_matches_draw(texture, draw);
                });
            if (!texture_matched) {
                ++unmatched_presented_texture_draw_count;
            }
        }
        std::ostringstream presented_texture_addresses_json;
        presented_texture_addresses_json << '[';
        for (size_t index = 0; index < presented_texture_addresses.size(); ++index) {
            if (index != 0u) {
                presented_texture_addresses_json << ',';
            }
            presented_texture_addresses_json << presented_texture_addresses[index];
        }
        presented_texture_addresses_json << ']';
        log_.emit(
            "nv2a_presented_geometry_anomalies",
            {
                {"reload", std::to_string(options_.live_render_stream ? live_render_reload_count_ + 1u : 0u)},
                {"source_commands", std::to_string(
                    options_.live_render_stream
                        ? interpreted_source_command_count_
                        : recovered_source_.commands.size())},
                {"guest_flips", std::to_string(interpreted_stream_.flip_count)},
                {"manifest_guest_flip_count", std::to_string(current_manifest_guest_flip_count_)},
                {"manifest_guest_steps", std::to_string(current_manifest_guest_steps_)},
                {"presented_draw_count", std::to_string(diagnostic_end_draw - diagnostic_first_draw)},
                {"offscreen_render_target_draw_count", std::to_string(offscreen_render_target_draw_count)},
                {"presented_vertex_count", std::to_string(presented_vertex_count)},
                {"fullscreen_draw_count", std::to_string(fullscreen_draw_count)},
                {"textured_fullscreen_draw_count", std::to_string(textured_fullscreen_draw_count)},
                {"untextured_presented_draw_count", std::to_string(untextured_presented_draw_count)},
                {"alpha_only_presented_draw_count", std::to_string(alpha_only_presented_draw_count)},
                {"zero_alpha_presented_draw_count", std::to_string(zero_alpha_presented_draw_count)},
                {"presented_texture_addresses", presented_texture_addresses_json.str()},
                {"valid_geometry_draw_count", std::to_string(valid_geometry_draw_count)},
                {"gpu_raw_attribute_draw_count", std::to_string(
                    gpu_raw_attribute_draw_count)},
                {"invalid_geometry_range_count", std::to_string(invalid_geometry_range_count)},
                {"filtered_fixed_function_draw_count", std::to_string(filtered_fixed_function_draw_count)},
                {"filtered_fixed_function_indexed_draw_count", std::to_string(filtered_fixed_function_indexed_draw_count)},
                {"non_finite_position_draw_count", std::to_string(non_finite_position_draw_count)},
                {"collapsed_x_draw_count", std::to_string(collapsed_x_draw_count)},
                {"collapsed_y_draw_count", std::to_string(collapsed_y_draw_count)},
                {"zero_area_draw_count", std::to_string(zero_area_draw_count)},
                {"exact_center_origin_draw_count", std::to_string(exact_center_origin_draw_count)},
                {"fully_offscreen_draw_count", std::to_string(fully_offscreen_draw_count)},
                {"outside_viewport_draw_count", std::to_string(outside_viewport_draw_count)},
                {"first_zero_area_presented_index", std::to_string(first_zero_area_presented_index)},
                {"first_center_origin_presented_index", std::to_string(first_center_origin_presented_index)},
                {"overall_min_x", overall_bounds_valid ? json_float(overall_min_x) : "null"},
                {"overall_max_x", overall_bounds_valid ? json_float(overall_max_x) : "null"},
                {"overall_min_y", overall_bounds_valid ? json_float(overall_min_y) : "null"},
                {"overall_max_y", overall_bounds_valid ? json_float(overall_max_y) : "null"},
                {"anomalous", json_bool(
                    invalid_geometry_range_count != 0u
                    || non_finite_position_draw_count != 0u
                    || zero_area_draw_count != 0u
                    || exact_center_origin_draw_count != 0u
                    || filtered_fixed_function_draw_count != 0u)},
            });
        const size_t vertex_buffer_resource_count = std::count_if(
            recovered_source_.textures.begin(),
            recovered_source_.textures.end(),
            [](const RecoveredTextureResource& resource) {
                return resource.format == "VERTEX_BUFFER";
            });
        log_.emit(
            "render_validation",
            {
                {"strict", json_bool(options_.strict_render_validation)},
                {"manifest_guest_flip_count", std::to_string(current_manifest_guest_flip_count_)},
                {"manifest_guest_steps", std::to_string(current_manifest_guest_steps_)},
                {"texture_resource_count", std::to_string(
                    recovered_source_.textures.size()
                        - vertex_buffer_resource_count)},
                {"vertex_buffer_resource_count", std::to_string(
                    vertex_buffer_resource_count)},
                {"offscreen_render_target_draw_count", std::to_string(offscreen_render_target_draw_count)},
                {"unsupported_texture_resource_count", std::to_string(unsupported_texture_resource_count_)},
                {"textured_presented_draw_count", std::to_string(textured_presented_draw_count)},
                {"unmatched_presented_texture_draw_count", std::to_string(unmatched_presented_texture_draw_count)},
                {"unsupported_presented_primitive_count", std::to_string(unsupported_presented_primitive_count)},
                {"missing_presented_indexed_resource_draw_count", std::to_string(missing_presented_indexed_resource_draw_count)},
                {"unsupported_draw_arrays_count", std::to_string(interpreted_stream_.unsupported_draw_arrays_count)},
                {"passed", json_bool(
                    unsupported_texture_resource_count_ == 0u
                    && unmatched_presented_texture_draw_count == 0u
                    && unsupported_presented_primitive_count == 0u
                    && missing_presented_indexed_resource_draw_count == 0u
                    && interpreted_stream_.unsupported_draw_arrays_count == 0u)},
            });
        if (options_.strict_render_validation
            && (unsupported_texture_resource_count_ != 0u
                || unmatched_presented_texture_draw_count != 0u
                || unsupported_presented_primitive_count != 0u
                || missing_presented_indexed_resource_draw_count != 0u
                || interpreted_stream_.unsupported_draw_arrays_count != 0u)) {
            std::ostringstream message;
            message << "strict render validation failed: "
                    << unsupported_texture_resource_count_ << " unsupported texture resources, "
                    << unmatched_presented_texture_draw_count << " unmatched textured presented draws, "
                    << unsupported_presented_primitive_count << " unsupported presented primitives, "
                    << missing_presented_indexed_resource_draw_count << " missing presented indexed resources, "
                    << interpreted_stream_.unsupported_draw_arrays_count << " unsupported DRAW_ARRAYS methods";
            throw std::runtime_error(message.str());
        }
        if (emit_presented_details) {
        for (size_t draw_index = diagnostic_first_draw;
             draw_index < diagnostic_end_draw;
             ++draw_index) {
            const NativeDraw& draw = interpreted_stream_.draws[draw_index];
            if (draw.vertex_count == 0u ||
                draw.first_vertex + draw.vertex_count > interpreted_stream_.vertices.size()) {
                continue;
            }
            const bool targets_presented_surface =
                draw_targets_presented_surface(draw);
            const auto [surface_width, surface_height] =
                draw_surface_extent(draw);
            const NativeVertex& first = interpreted_stream_.vertices[draw.first_vertex];
            float min_x = first.x;
            float max_x = first.x;
            float min_y = first.y;
            float max_y = first.y;
            float min_u = first.u;
            float max_u = first.u;
            float min_v = first.v;
            float max_v = first.v;
            float min_r = first.r;
            float max_r = first.r;
            float min_g = first.g;
            float max_g = first.g;
            float min_b = first.b;
            float max_b = first.b;
            float min_a = first.a;
            float max_a = first.a;
            for (uint32_t vertex_index = 1; vertex_index < draw.vertex_count; ++vertex_index) {
                const NativeVertex& vertex =
                    interpreted_stream_.vertices[draw.first_vertex + vertex_index];
                min_x = std::min(min_x, vertex.x);
                max_x = std::max(max_x, vertex.x);
                min_y = std::min(min_y, vertex.y);
                max_y = std::max(max_y, vertex.y);
                min_u = std::min(min_u, vertex.u);
                max_u = std::max(max_u, vertex.u);
                min_v = std::min(min_v, vertex.v);
                max_v = std::max(max_v, vertex.v);
                min_r = std::min(min_r, vertex.r);
                max_r = std::max(max_r, vertex.r);
                min_g = std::min(min_g, vertex.g);
                max_g = std::max(max_g, vertex.g);
                min_b = std::min(min_b, vertex.b);
                max_b = std::max(max_b, vertex.b);
                min_a = std::min(min_a, vertex.a);
                max_a = std::max(max_a, vertex.a);
            }
            const bool texture_matched = std::any_of(
                host_textures_.begin() + 1,
                host_textures_.end(),
                [&](const HostTexture& texture) {
                    return host_texture_matches_draw(texture, draw);
                });
            log_.emit(
                "nv2a_presented_draw_details",
                {
                    {"presented_index", std::to_string(draw_index - diagnostic_first_draw)},
                    {"draw_index", std::to_string(draw_index)},
                    {"targets_presented_surface", json_bool(targets_presented_surface)},
                    {"surface_width", std::to_string(surface_width)},
                    {"surface_height", std::to_string(surface_height)},
                    {"primitive", std::to_string(draw.primitive)},
                    {"first_vertex", std::to_string(draw.first_vertex)},
                    {"vertex_count", std::to_string(draw.vertex_count)},
                    {"indexed_array", json_bool(draw.indexed_array)},
                    {"texture_address", std::to_string(draw.texture_address)},
                    {"texture_enabled", json_bool(draw.texture_enabled)},
                    {"texture_stage", std::to_string(draw.texture_stage)},
                    {"texture_matched", json_bool(texture_matched)},
                    {"texture_offset_0", std::to_string(draw.texture_offsets[0])},
                    {"texture_offset_1", std::to_string(draw.texture_offsets[1])},
                    {"texture_offset_2", std::to_string(draw.texture_offsets[2])},
                    {"texture_offset_3", std::to_string(draw.texture_offsets[3])},
                    {"texture_control_0", std::to_string(draw.texture_controls[0])},
                    {"texture_control_1", std::to_string(draw.texture_controls[1])},
                    {"texture_control_2", std::to_string(draw.texture_controls[2])},
                    {"texture_control_3", std::to_string(draw.texture_controls[3])},
                    {"texture_format_0", std::to_string(draw.texture_formats[0])},
                    {"texture_format_1", std::to_string(draw.texture_formats[1])},
                    {"texture_format_2", std::to_string(draw.texture_formats[2])},
                    {"texture_format_3", std::to_string(draw.texture_formats[3])},
                    {"texture_image_rect_0", std::to_string(draw.texture_image_rects[0])},
                    {"texture_image_rect_1", std::to_string(draw.texture_image_rects[1])},
                    {"texture_image_rect_2", std::to_string(draw.texture_image_rects[2])},
                    {"texture_image_rect_3", std::to_string(draw.texture_image_rects[3])},
                    {"blend_enable", std::to_string(draw.blend_enable)},
                    {"blend_source_factor", std::to_string(draw.blend_source_factor)},
                    {"blend_destination_factor", std::to_string(draw.blend_destination_factor)},
                    {"blend_equation", std::to_string(draw.blend_equation)},
                    {"surface_format", std::to_string(draw.surface_format)},
                    {"surface_pitch", std::to_string(draw.surface_pitch)},
                    {"surface_color_offset", std::to_string(draw.surface_color_offset)},
                    {"surface_clip_horizontal", std::to_string(draw.surface_clip_horizontal)},
                    {"surface_clip_vertical", std::to_string(draw.surface_clip_vertical)},
                    {"color_mask", std::to_string(draw.color_mask)},
                    {"depth_test_enable", std::to_string(draw.depth_test_enable)},
                    {"depth_function", std::to_string(draw.depth_function)},
                    {"depth_write_enable", std::to_string(draw.depth_write_enable)},
                    {"cull_face_enable", std::to_string(draw.cull_face_enable)},
                    {"cull_face", std::to_string(draw.cull_face)},
                    {"front_face", std::to_string(draw.front_face)},
                    {"shader_stage_program", std::to_string(draw.shader_stage_program)},
                    {"combiner_control", std::to_string(draw.combiner_control)},
                    {"combiner_color_input0", std::to_string(draw.combiner_color_inputs[0])},
                    {"combiner_color_output0", std::to_string(draw.combiner_color_outputs[0])},
                    {"combiner_alpha_input0", std::to_string(draw.combiner_alpha_inputs[0])},
                    {"combiner_alpha_output0", std::to_string(draw.combiner_alpha_outputs[0])},
                    {"combiner_factor0_0", std::to_string(draw.combiner_factors0[0])},
                    {"combiner_factor1_0", std::to_string(draw.combiner_factors1[0])},
                    {"final_combiner_inputs0", std::to_string(draw.final_combiner_inputs0)},
                    {"final_combiner_inputs1", std::to_string(draw.final_combiner_inputs1)},
                    {"final_combiner_factor0", std::to_string(draw.final_combiner_factors[0])},
                    {"final_combiner_factor1", std::to_string(draw.final_combiner_factors[1])},
                    {"fog_enable", std::to_string(draw.fog_enable)},
                    {"fog_mode", std::to_string(draw.fog_mode)},
                    {"fog_generation_mode", std::to_string(draw.fog_generation_mode)},
                    {"fog_color", std::to_string(draw.fog_color)},
                    {"vertex_format_0", std::to_string(draw.vertex_formats[0])},
                    {"vertex_format_3", std::to_string(draw.vertex_formats[3])},
                    {"vertex_format_9", std::to_string(draw.vertex_formats[9])},
                    {"vertex_offset_0", std::to_string(draw.vertex_offsets[0])},
                    {"vertex_offset_3", std::to_string(draw.vertex_offsets[3])},
                    {"vertex_offset_9", std::to_string(draw.vertex_offsets[9])},
                    {"transform_execution_mode", std::to_string(draw.transform_execution_mode)},
                    {"transform_program_start", std::to_string(draw.transform_program_start)},
                    {"transform_constant_0_x", std::to_string(draw.transform_constants[0][0])},
                    {"viewport_scale_x", json_float(float_from_u32(draw.transform_constants[58][0]))},
                    {"viewport_scale_y", json_float(float_from_u32(draw.transform_constants[58][1]))},
                    {"viewport_offset_x", json_float(float_from_u32(draw.transform_constants[59][0]))},
                    {"viewport_offset_y", json_float(float_from_u32(draw.transform_constants[59][1]))},
                    {"transform_constant_96_x", std::to_string(draw.transform_constants[96][0])},
                    {"min_x", json_float(min_x)},
                    {"max_x", json_float(max_x)},
                    {"min_y", json_float(min_y)},
                    {"max_y", json_float(max_y)},
                    {"min_u", json_float(min_u)},
                    {"max_u", json_float(max_u)},
                    {"min_v", json_float(min_v)},
                    {"max_v", json_float(max_v)},
                    {"min_r", json_float(min_r)},
                    {"max_r", json_float(max_r)},
                    {"min_g", json_float(min_g)},
                    {"max_g", json_float(max_g)},
                    {"min_b", json_float(min_b)},
                    {"max_b", json_float(max_b)},
                    {"min_a", json_float(min_a)},
                    {"max_a", json_float(max_a)},
                });
            if ((draw.transform_execution_mode & 3u) == 2u) {
                for (uint32_t instruction = draw.transform_program_start;
                     instruction < draw.transform_program.size()
                         && instruction < draw.transform_program_start + 32u;
                     ++instruction) {
                    const auto& token = draw.transform_program[instruction];
                    log_.emit(
                        "nv2a_presented_transform_instruction",
                        {
                            {"presented_index", std::to_string(draw_index - diagnostic_first_draw)},
                            {"instruction", std::to_string(instruction)},
                            {"token_0", std::to_string(token[0])},
                            {"token_1", std::to_string(token[1])},
                            {"token_2", std::to_string(token[2])},
                            {"token_3", std::to_string(token[3])},
                            {"final", json_bool((token[3] & 1u) != 0u)},
                        });
                    if ((token[3] & 1u) != 0u) {
                        break;
                    }
                }
            }
            const uint32_t diagnostic_vertex_count = std::min<uint32_t>(
                draw.vertex_count,
                12u);
            for (uint32_t vertex_index = 0;
                 vertex_index < diagnostic_vertex_count;
                 ++vertex_index) {
                const NativeVertex& vertex =
                    interpreted_stream_.vertices[draw.first_vertex + vertex_index];
                log_.emit(
                    "nv2a_presented_vertex_details",
                    {
                        {"presented_index", std::to_string(draw_index - diagnostic_first_draw)},
                        {"vertex_index", std::to_string(vertex_index)},
                        {"raw_x_bits", std::to_string(vertex.raw_x_bits)},
                        {"raw_y_bits", std::to_string(vertex.raw_y_bits)},
                        {"x", json_float(vertex.x)},
                        {"y", json_float(vertex.y)},
                        {"r", json_float(vertex.r)},
                        {"g", json_float(vertex.g)},
                        {"b", json_float(vertex.b)},
                        {"a", json_float(vertex.a)},
                        {"u", json_float(vertex.u)},
                        {"v", json_float(vertex.v)},
                    });
            }
        }
        }
        if (interpreted_stream_.presented_draw_count != 0u) {
            const auto selected_begin = interpreted_stream_.draws.begin()
                + std::min<size_t>(
                    interpreted_stream_.presented_draw_begin,
                    interpreted_stream_.draws.size());
            const auto selected_end = interpreted_stream_.draws.begin()
                + std::min<size_t>(
                    interpreted_stream_.presented_draw_begin
                        + interpreted_stream_.presented_draw_count,
                    interpreted_stream_.draws.size());
            const auto selected = std::find_if(
                selected_begin,
                selected_end,
                [&](const NativeDraw& candidate) {
                    return draw_targets_presented_surface(candidate)
                        && candidate.vertex_count != 0u
                        && candidate.first_vertex + candidate.vertex_count
                            <= interpreted_stream_.vertices.size();
                });
            if (selected != selected_end) {
                const NativeDraw& draw = *selected;
                const NativeVertex& vertex =
                    interpreted_stream_.vertices[draw.first_vertex];
                const bool texture_matched = std::any_of(
                    host_textures_.begin() + 1,
                    host_textures_.end(),
                    [&](const HostTexture& texture) {
                        return host_texture_matches_draw(texture, draw);
                    });
                log_.emit(
                    "nv2a_presented_draw_selected",
                    {
                        {"texture_address", std::to_string(draw.texture_address)},
                        {"texture_matched", json_bool(texture_matched)},
                        {"first_vertex", std::to_string(draw.first_vertex)},
                        {"color_r", std::to_string(vertex.r)},
                        {"color_a", std::to_string(vertex.a)},
                        {"u", std::to_string(vertex.u)},
                        {"v", std::to_string(vertex.v)},
                    });
            }
        }
        last_render_validation_us_ =
            std::chrono::duration_cast<std::chrono::microseconds>(
                std::chrono::steady_clock::now()
                    - render_validation_begin).count();
    }

    VkDescriptorSet descriptor_for_draw(const NativeDraw& draw) const {
        const size_t texture_index = host_texture_index_for_draw(draw);
        const uint32_t stage = std::min<uint32_t>(draw.texture_stage, 3u);
        const uint32_t address = draw.texture_enabled
                && draw.texture_stage < draw.texture_addresses.size()
            ? draw.texture_addresses[draw.texture_stage]
            : 0u;
        const auto match = std::find_if(
            host_texture_bindings_.begin(),
            host_texture_bindings_.end(),
            [&](const HostTextureBinding& binding) {
                return binding.texture_index == texture_index
                    && binding.address == address
                    && binding.format == draw.texture_formats[stage]
                    && binding.control == draw.texture_controls[stage]
                    && binding.filter == draw.texture_filters[stage];
            });
        if (match != host_texture_bindings_.end()) {
            return match->descriptor_set;
        }
        return host_texture_bindings_.front().descriptor_set;
    }

    const HostTexture* presented_render_target_feedback_texture() const {
        const uint32_t selected_address = presented_surface_color_offset_;
        const size_t first_draw = std::min<size_t>(
            interpreted_stream_.presented_draw_begin,
            interpreted_stream_.draws.size());
        const size_t end_draw = std::min<size_t>(
            first_draw + interpreted_stream_.presented_draw_count,
            interpreted_stream_.draws.size());
        for (size_t draw_index = first_draw; draw_index < end_draw; ++draw_index) {
            const uint32_t address =
                interpreted_stream_.draws[draw_index].surface_color_offset;
            if (selected_address != 0u && address != selected_address) {
                continue;
            }
            const auto match = std::find_if(
                host_textures_.begin(),
                host_textures_.end(),
                [&](const HostTexture& texture) {
                    return texture.render_target_feedback
                        && texture.guest_address == address
                        && texture.width == swapchain_extent_.width
                        && texture.height == swapchain_extent_.height
                        && texture.image_format == swapchain_format_;
                });
            if (match != host_textures_.end()) {
                return &*match;
            }
        }
        return nullptr;
    }

    bool record_render_target_feedback(
        VkCommandBuffer command_buffer,
        VkImage swapchain_image) const {
        const HostTexture* target =
            presented_render_target_feedback_texture();
        if (target == nullptr) {
            return false;
        }
        VkImageMemoryBarrier to_transfer{};
        to_transfer.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
        to_transfer.srcAccessMask = VK_ACCESS_SHADER_READ_BIT;
        to_transfer.dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
        to_transfer.oldLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
        to_transfer.newLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
        to_transfer.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
        to_transfer.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
        to_transfer.image = target->image;
        to_transfer.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
        to_transfer.subresourceRange.levelCount = 1;
        to_transfer.subresourceRange.layerCount = 1;
        ++recording_barrier_count_;
        vkCmdPipelineBarrier(
            command_buffer,
            VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT,
            VK_PIPELINE_STAGE_TRANSFER_BIT,
            0,
            0,
            nullptr,
            0,
            nullptr,
            1,
            &to_transfer);

        VkImageCopy copy{};
        copy.srcSubresource.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
        copy.srcSubresource.layerCount = 1;
        copy.dstSubresource.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
        copy.dstSubresource.layerCount = 1;
        copy.extent = {
            swapchain_extent_.width,
            swapchain_extent_.height,
            1u,
        };
        vkCmdCopyImage(
            command_buffer,
            swapchain_image,
            VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
            target->image,
            VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
            1,
            &copy);

        VkImageMemoryBarrier to_shader = to_transfer;
        to_shader.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
        to_shader.dstAccessMask = VK_ACCESS_SHADER_READ_BIT;
        to_shader.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
        to_shader.newLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
        ++recording_barrier_count_;
        vkCmdPipelineBarrier(
            command_buffer,
            VK_PIPELINE_STAGE_TRANSFER_BIT,
            VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT,
            0,
            0,
            nullptr,
            0,
            nullptr,
            1,
            &to_shader);
        return true;
    }

    void record_native_draws_for_target(
        VkCommandBuffer command_buffer,
        VkExtent2D target_extent,
        std::optional<uint32_t> producer_address) const {
        if (interpreted_stream_.draws.empty() || vertex_buffer_ == VK_NULL_HANDLE) {
            return;
        }
        const VkDeviceSize offset = 0;
        vkCmdBindVertexBuffers(command_buffer, 0, 1, &vertex_buffer_, &offset);
        const VkViewport viewport{
            0.0f,
            0.0f,
            static_cast<float>(target_extent.width),
            static_cast<float>(target_extent.height),
            0.0f,
            1.0f,
        };
        vkCmdSetViewport(command_buffer, 0, 1, &viewport);
        const size_t first_draw = std::min<size_t>(
            interpreted_stream_.presented_draw_begin,
            interpreted_stream_.draws.size());
        const size_t end_draw = std::min<size_t>(
            first_draw + interpreted_stream_.presented_draw_count,
            interpreted_stream_.draws.size());
        for (size_t draw_index = first_draw; draw_index < end_draw; ++draw_index) {
            const size_t presented_index = draw_index - first_draw;
            if (presented_index < options_.presented_draw_begin
                || presented_index >= options_.presented_draw_end) {
                continue;
            }
            const NativeDraw& draw = interpreted_stream_.draws[draw_index];
            const bool targets_requested_surface = producer_address.has_value()
                ? !draw_targets_presented_surface(draw)
                    && nv2a_canonical_resource_address(
                        draw.surface_color_offset)
                        == nv2a_canonical_resource_address(
                            *producer_address)
                : draw_targets_presented_surface(draw);
            if (!targets_requested_surface
                || !draw_has_supported_host_transform(draw)
                || (draw.primitive != 5u && draw.primitive != 6u)
                || draw.vertex_count == 0u) {
                continue;
            }
            const NativePipelineState state = pipeline_state_for_draw(draw);
            const auto pipeline = std::find_if(
                graphics_pipelines_.begin(), graphics_pipelines_.end(),
                [&](const HostPipeline& candidate) { return candidate.state == state; });
            if (pipeline == graphics_pipelines_.end()) continue;
            vkCmdBindPipeline(command_buffer, VK_PIPELINE_BIND_POINT_GRAPHICS, pipeline->pipeline);
            const VkRect2D scissor = draw_scissor(draw, target_extent);
            if (scissor.extent.width == 0u || scissor.extent.height == 0u) {
                continue;
            }
            vkCmdSetScissor(command_buffer, 0, 1, &scissor);
            const VkDescriptorSet descriptor = descriptor_for_draw(draw);
            vkCmdBindDescriptorSets(command_buffer, VK_PIPELINE_BIND_POINT_GRAPHICS, pipeline_layout_, 0, 1, &descriptor, 0, nullptr);
            const NativeFragmentPushConstants fragment_state{
                static_cast<uint32_t>(presented_index),
            };
            vkCmdPushConstants(
                command_buffer,
                pipeline_layout_,
                VK_SHADER_STAGE_VERTEX_BIT | VK_SHADER_STAGE_FRAGMENT_BIT,
                0u,
                sizeof(fragment_state),
                &fragment_state);
            const uint64_t triangle_count = draw.primitive == 5u
                ? draw.vertex_count / 3u
                : (draw.vertex_count >= 3u ? draw.vertex_count - 2u : 0u);
            if (draw.gpu_raw_attribute_fetch) {
                ++recording_draw_count_;
                recording_triangle_count_ += triangle_count;
                vkCmdDraw(
                    command_buffer,
                    draw.vertex_count,
                    1,
                    0,
                    0);
                continue;
            }
            const uint64_t draw_first = draw.first_vertex;
            const uint64_t draw_end = draw_first + draw.vertex_count;
            const uint64_t upload_first = uploaded_vertex_base_;
            const uint64_t upload_end = upload_first + uploaded_vertex_count_;
            if (draw_first < upload_first || draw_end > upload_end) {
                continue;
            }
            ++recording_draw_count_;
            recording_triangle_count_ += triangle_count;
            vkCmdDraw(
                command_buffer,
                draw.vertex_count,
                1,
                draw.first_vertex - uploaded_vertex_base_,
                0);
        }
    }

    void record_native_draws(VkCommandBuffer command_buffer) const {
        record_native_draws_for_target(
            command_buffer,
            swapchain_extent_,
            std::nullopt);
    }

    uint32_t record_offscreen_render_targets(
        VkCommandBuffer command_buffer) const {
        uint32_t recorded = 0u;
        for (const OffscreenRenderTarget& target : offscreen_render_targets_) {
            const auto texture = std::find_if(
                host_textures_.begin(),
                host_textures_.end(),
                [&](const HostTexture& candidate) {
                    return candidate.render_target_feedback
                        && nv2a_canonical_resource_address(
                            candidate.guest_address)
                            == nv2a_canonical_resource_address(
                                target.spec.address)
                        && candidate.width == target.spec.width
                        && candidate.height == target.spec.height;
                });
            if (texture == host_textures_.end()
                || target.framebuffer == VK_NULL_HANDLE) {
                continue;
            }
            const VkExtent2D extent{
                target.spec.width,
                target.spec.height,
            };
            std::array<VkClearValue, 2> clear_values{};
            clear_values[1].depthStencil = {1.0f, 0u};
            VkRenderPassBeginInfo render_pass_info{};
            render_pass_info.sType =
                VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO;
            render_pass_info.renderPass = render_pass_;
            render_pass_info.framebuffer = target.framebuffer;
            render_pass_info.renderArea = {{0, 0}, extent};
            render_pass_info.clearValueCount = static_cast<uint32_t>(
                clear_values.size());
            render_pass_info.pClearValues = clear_values.data();
            vkCmdBeginRenderPass(
                command_buffer,
                &render_pass_info,
                VK_SUBPASS_CONTENTS_INLINE);
            record_native_draws_for_target(
                command_buffer,
                extent,
                target.spec.producer_address);
            vkCmdEndRenderPass(command_buffer);

            VkImageMemoryBarrier to_shader{};
            to_shader.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
            to_shader.srcAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;
            to_shader.dstAccessMask = VK_ACCESS_SHADER_READ_BIT;
            to_shader.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
            to_shader.newLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
            to_shader.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
            to_shader.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
            to_shader.image = texture->image;
            to_shader.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
            to_shader.subresourceRange.levelCount = 1;
            to_shader.subresourceRange.layerCount = 1;
            ++recording_barrier_count_;
            vkCmdPipelineBarrier(
                command_buffer,
                VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT,
                VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT,
                0,
                0,
                nullptr,
                0,
                nullptr,
                1,
                &to_shader);
            ++recorded;
        }
        return recorded;
    }

    struct FrontendTextRaster {
        uint32_t width = 0u;
        uint32_t height = 0u;
        std::vector<uint8_t> alpha;
    };

    FrontendTextRaster rasterize_frontend_text(
        const std::string& text,
        float layout_scale) const {
        FrontendTextRaster raster{};
        if (text.empty()) {
            return raster;
        }
        const std::wstring wide_text = widen(text);
        const int32_t pixel_height = std::max<int32_t>(
            1, static_cast<int32_t>(std::lround(28.0f * layout_scale)));
        HDC dc = CreateCompatibleDC(nullptr);
        if (dc == nullptr) {
            throw std::runtime_error("CreateCompatibleDC(frontend text) failed");
        }
        HFONT font = CreateFontW(
            -pixel_height,
            0,
            0,
            0,
            FW_NORMAL,
            FALSE,
            FALSE,
            FALSE,
            DEFAULT_CHARSET,
            OUT_TT_PRECIS,
            CLIP_DEFAULT_PRECIS,
            ANTIALIASED_QUALITY,
            DEFAULT_PITCH | FF_DONTCARE,
            L"Impact");
        if (font == nullptr) {
            DeleteDC(dc);
            throw std::runtime_error("CreateFontW(frontend text) failed");
        }
        const HGDIOBJ previous_font = SelectObject(dc, font);
        SIZE extent{};
        TEXTMETRICW metrics{};
        if (!GetTextExtentPoint32W(
                dc,
                wide_text.data(),
                static_cast<int>(wide_text.size()),
                &extent)
            || !GetTextMetricsW(dc, &metrics)) {
            SelectObject(dc, previous_font);
            DeleteObject(font);
            DeleteDC(dc);
            throw std::runtime_error("GDI frontend text measurement failed");
        }
        const LONG extra_space = static_cast<LONG>(std::lround(
            2.0f * layout_scale));
        const LONG space_count = static_cast<LONG>(std::count(
            wide_text.begin(), wide_text.end(), L' '));
        raster.width = static_cast<uint32_t>(std::max<LONG>(
            extent.cx + extra_space * space_count + 4,
            1));
        raster.height = static_cast<uint32_t>(
            std::max<LONG>(std::max(extent.cy, metrics.tmHeight) + 4, 1));
        BITMAPINFO bitmap_info{};
        bitmap_info.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
        bitmap_info.bmiHeader.biWidth = static_cast<LONG>(raster.width);
        bitmap_info.bmiHeader.biHeight = -static_cast<LONG>(raster.height);
        bitmap_info.bmiHeader.biPlanes = 1;
        bitmap_info.bmiHeader.biBitCount = 32;
        bitmap_info.bmiHeader.biCompression = BI_RGB;
        void* dib_pixels = nullptr;
        HBITMAP bitmap = CreateDIBSection(
            dc,
            &bitmap_info,
            DIB_RGB_COLORS,
            &dib_pixels,
            nullptr,
            0);
        if (bitmap == nullptr || dib_pixels == nullptr) {
            SelectObject(dc, previous_font);
            DeleteObject(font);
            DeleteDC(dc);
            throw std::runtime_error("CreateDIBSection(frontend text) failed");
        }
        const HGDIOBJ previous_bitmap = SelectObject(dc, bitmap);
        PatBlt(dc, 0, 0, raster.width, raster.height, BLACKNESS);
        SetBkMode(dc, TRANSPARENT);
        SetTextColor(dc, RGB(255, 255, 255));
        SIZE space_extent{};
        GetTextExtentPoint32W(dc, L" ", 1, &space_extent);
        LONG cursor = 2;
        size_t text_cursor = 0u;
        while (text_cursor < wide_text.size()) {
            if (wide_text[text_cursor] == L' ') {
                cursor += space_extent.cx + extra_space;
                ++text_cursor;
                continue;
            }
            const size_t word_end = wide_text.find(L' ', text_cursor);
            const size_t word_length = (word_end == std::wstring::npos
                ? wide_text.size()
                : word_end) - text_cursor;
            SIZE word_extent{};
            if (!GetTextExtentPoint32W(
                    dc,
                    wide_text.data() + text_cursor,
                    static_cast<int>(word_length),
                    &word_extent)
                || !TextOutW(
                    dc,
                    cursor,
                    2,
                    wide_text.data() + text_cursor,
                    static_cast<int>(word_length))) {
                SelectObject(dc, previous_bitmap);
                SelectObject(dc, previous_font);
                DeleteObject(bitmap);
                DeleteObject(font);
                DeleteDC(dc);
                throw std::runtime_error("TextOutW(frontend text) failed");
            }
            cursor += word_extent.cx;
            text_cursor += word_length;
        }
        GdiFlush();
        raster.alpha.resize(
            static_cast<size_t>(raster.width) * raster.height);
        const uint8_t* bgra = static_cast<const uint8_t*>(dib_pixels);
        for (size_t pixel = 0; pixel < raster.alpha.size(); ++pixel) {
            raster.alpha[pixel] = std::max({
                bgra[pixel * 4u],
                bgra[pixel * 4u + 1u],
                bgra[pixel * 4u + 2u],
            });
        }
        SelectObject(dc, previous_bitmap);
        SelectObject(dc, previous_font);
        DeleteObject(bitmap);
        DeleteObject(font);
        DeleteDC(dc);
        return raster;
    }

    uint32_t append_frontend_text_vertices(
        std::vector<NativeVertex>& vertices,
        const std::string& text) {
        frontend_text_first_vertex_ = static_cast<uint32_t>(vertices.size());
        frontend_text_vertex_count_ = 0u;
        if (text.empty()) {
            return 0;
        }
        float text_size = float_from_u32(
            recovered_source_.frontend_text_size_bits);
        if (!std::isfinite(text_size) || text_size <= 0.0f) {
            text_size = 25.0f;
        }
        const float layout_scale = std::clamp(
            text_size / 25.0f, 0.25f, 4.0f);
        const FrontendTextRaster raster = rasterize_frontend_text(
            text, layout_scale);
        if (raster.alpha.empty()) {
            return 0;
        }
        float center_x = float_from_u32(recovered_source_.frontend_text_x_bits);
        float top_y = float_from_u32(recovered_source_.frontend_text_y_bits);
        if (!std::isfinite(center_x)) {
            center_x = static_cast<float>(swapchain_extent_.width) * 0.5f;
        }
        if (!std::isfinite(top_y)) {
            top_y = static_cast<float>(swapchain_extent_.height) * 0.5f;
        }
        const int32_t origin_x = static_cast<int32_t>(std::lround(
            center_x - 4.0f * layout_scale
                - static_cast<float>(raster.width) * 0.5f));
        const int32_t origin_y = static_cast<int32_t>(std::lround(
            top_y + 7.0f * layout_scale));
        const uint32_t color_argb =
            recovered_source_.frontend_text_color_argb;
        const float color_r = static_cast<float>(
            (color_argb >> 16u) & 0xFFu) / 255.0f;
        const float color_g = static_cast<float>(
            (color_argb >> 8u) & 0xFFu) / 255.0f;
        const float color_b = static_cast<float>(
            color_argb & 0xFFu) / 255.0f;
        const float color_a = static_cast<float>(
            (color_argb >> 24u) & 0xFFu) / 255.0f;
        uint32_t rectangle_count = 0u;
        auto append_rectangle = [&](int32_t left,
                                    int32_t top,
                                    int32_t right,
                                    int32_t bottom,
                                    float alpha) {
            left = std::clamp<int32_t>(
                left, 0, static_cast<int32_t>(swapchain_extent_.width));
            right = std::clamp<int32_t>(
                right, 0, static_cast<int32_t>(swapchain_extent_.width));
            top = std::clamp<int32_t>(
                top, 0, static_cast<int32_t>(swapchain_extent_.height));
            bottom = std::clamp<int32_t>(
                bottom, 0, static_cast<int32_t>(swapchain_extent_.height));
            if (left >= right || top >= bottom) {
                return;
            }
            const float x0 = static_cast<float>(left) * 2.0f
                / static_cast<float>(swapchain_extent_.width) - 1.0f;
            const float x1 = static_cast<float>(right) * 2.0f
                / static_cast<float>(swapchain_extent_.width) - 1.0f;
            const float y0 = static_cast<float>(top) * 2.0f
                / static_cast<float>(swapchain_extent_.height) - 1.0f;
            const float y1 = static_cast<float>(bottom) * 2.0f
                / static_cast<float>(swapchain_extent_.height) - 1.0f;
            const std::array<std::array<float, 2>, 6> positions{{
                {x0, y0}, {x1, y0}, {x0, y1},
                {x0, y1}, {x1, y0}, {x1, y1},
            }};
            for (const auto& position : positions) {
                NativeVertex vertex{};
                vertex.x = position[0];
                vertex.y = position[1];
                vertex.r = color_r;
                vertex.g = color_g;
                vertex.b = color_b;
                vertex.a = alpha * color_a;
                vertices.push_back(vertex);
            }
            ++rectangle_count;
        };
        for (uint32_t row = 0u; row < raster.height; ++row) {
            uint32_t column = 0u;
            while (column < raster.width) {
                const uint32_t coverage = raster.alpha[
                    static_cast<size_t>(row) * raster.width + column];
                const uint32_t quantized = coverage < 16u
                    ? 0u
                    : std::min(255u, ((coverage + 15u) / 32u) * 32u);
                if (quantized == 0u) {
                    ++column;
                    continue;
                }
                const uint32_t run_begin = column++;
                while (column < raster.width) {
                    const uint32_t next_coverage = raster.alpha[
                        static_cast<size_t>(row) * raster.width + column];
                    const uint32_t next_quantized = next_coverage < 16u
                        ? 0u
                        : std::min(
                            255u,
                            ((next_coverage + 15u) / 32u) * 32u);
                    if (next_quantized != quantized) {
                        break;
                    }
                    ++column;
                }
                append_rectangle(
                    origin_x + static_cast<int32_t>(run_begin),
                    origin_y + static_cast<int32_t>(row),
                    origin_x + static_cast<int32_t>(column),
                    origin_y + static_cast<int32_t>(row + 1u),
                    static_cast<float>(quantized) / 255.0f);
            }
        }
        frontend_text_vertex_count_ = rectangle_count * 6u;
        return rectangle_count;
    }

    void record_frontend_text(VkCommandBuffer command_buffer) const {
        if (frontend_text_vertex_count_ == 0u
            || frontend_text_fragment_state_index_
                == std::numeric_limits<uint32_t>::max()
            || vertex_buffer_ == VK_NULL_HANDLE
            || host_texture_bindings_.empty()) {
            return;
        }
        const NativePipelineState state = frontend_text_pipeline_state();
        const auto pipeline = std::find_if(
            graphics_pipelines_.begin(),
            graphics_pipelines_.end(),
            [&](const HostPipeline& candidate) {
                return candidate.state == state;
            });
        if (pipeline == graphics_pipelines_.end()) {
            return;
        }
        const VkDeviceSize offset = 0;
        vkCmdBindVertexBuffers(command_buffer, 0, 1, &vertex_buffer_, &offset);
        const VkViewport viewport{
            0.0f,
            0.0f,
            static_cast<float>(swapchain_extent_.width),
            static_cast<float>(swapchain_extent_.height),
            0.0f,
            1.0f,
        };
        vkCmdSetViewport(command_buffer, 0, 1, &viewport);
        const VkRect2D scissor{{0, 0}, swapchain_extent_};
        vkCmdSetScissor(command_buffer, 0, 1, &scissor);
        vkCmdBindPipeline(
            command_buffer,
            VK_PIPELINE_BIND_POINT_GRAPHICS,
            pipeline->pipeline);
        const VkDescriptorSet descriptor =
            host_texture_bindings_.front().descriptor_set;
        vkCmdBindDescriptorSets(
            command_buffer,
            VK_PIPELINE_BIND_POINT_GRAPHICS,
            pipeline_layout_,
            0,
            1,
            &descriptor,
            0,
            nullptr);
        const NativeFragmentPushConstants fragment_state{
            frontend_text_fragment_state_index_,
        };
        vkCmdPushConstants(
            command_buffer,
            pipeline_layout_,
            VK_SHADER_STAGE_VERTEX_BIT | VK_SHADER_STAGE_FRAGMENT_BIT,
            0u,
            sizeof(fragment_state),
            &fragment_state);
        ++recording_draw_count_;
        recording_triangle_count_ += frontend_text_vertex_count_ / 3u;
        vkCmdDraw(
            command_buffer,
            frontend_text_vertex_count_,
            1,
            frontend_text_first_vertex_,
            0);
    }

    void create_command_buffers() {
        command_buffers_.resize(framebuffers_.size());
        const std::vector<RecoveredD3DCommand>& recovered_d3d_stream =
            recovered_source_.commands;
        const InterpretedD3DStream& interpreted_stream = interpreted_stream_;
        if (options_.live_render_stream) {
            if (counted_command_render_work_generation_
                != render_work_generation_) {
                if (last_command_cursor_reset_) {
                    recovered_d3d_mmio_count_ = 0u;
                }
                recovered_d3d_mmio_count_ += last_live_command_mmio_count_;
                counted_command_render_work_generation_ =
                    render_work_generation_;
            }
            counted_recovered_command_count_ = 0u;
            recovered_d3d_command_count_ = static_cast<uint32_t>(
                std::min<size_t>(
                    interpreted_source_command_count_,
                    std::numeric_limits<uint32_t>::max()));
        } else {
            if (counted_recovered_command_count_ > recovered_d3d_stream.size()) {
                counted_recovered_command_count_ = 0u;
                recovered_d3d_mmio_count_ = 0u;
            }
            for (size_t index = counted_recovered_command_count_;
                 index < recovered_d3d_stream.size();
                 ++index) {
                recovered_d3d_mmio_count_ += static_cast<uint32_t>(
                    recovered_d3d_stream[index].kind
                        == RecoveredD3DCommandKind::MmioWrite);
            }
            counted_recovered_command_count_ = recovered_d3d_stream.size();
            recovered_d3d_command_count_ = static_cast<uint32_t>(
                recovered_d3d_stream.size());
        }
        recovered_d3d_push_buffer_count_ =
            recovered_d3d_command_count_ - recovered_d3d_mmio_count_;
        interpreted_push_buffer_word_count_ = interpreted_stream.push_buffer_word_count;
        interpreted_method_packet_count_ = interpreted_stream.method_packet_count;
        interpreted_method_count_ = interpreted_stream.interpreted_method_count;
        interpreted_zero_count_method_word_count_ = interpreted_stream.zero_count_method_word_count;
        translated_command_count_ =
            interpreted_stream.method_packet_count +
            interpreted_stream.zero_count_method_word_count +
            interpreted_stream.control_flow_packet_count +
            interpreted_stream.mmio_setup_write_count +
            interpreted_stream.submission_kick_count +
            interpreted_stream.unknown_packet_count;
        VkCommandBufferAllocateInfo allocate_info{};
        allocate_info.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;
        allocate_info.commandPool = command_pool_;
        allocate_info.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
        allocate_info.commandBufferCount = static_cast<uint32_t>(command_buffers_.size());
        vk_check(vkAllocateCommandBuffers(device_, &allocate_info, command_buffers_.data()), "vkAllocateCommandBuffers");
        command_buffer_allocation_count_ += command_buffers_.size();
        command_buffer_draw_counts_.assign(command_buffers_.size(), 0u);
        command_buffer_triangle_counts_.assign(command_buffers_.size(), 0u);
        command_buffer_barrier_counts_.assign(command_buffers_.size(), 0u);

        const bool record_frame_readback = readback_enabled()
            && (options_.flip_audit_ack.empty()
                || !flip_audit_health_check_reason().empty());
        current_work_has_readback_ = record_frame_readback;
        uint32_t recorded_render_target_feedback_copy_count = 0;
        uint32_t recorded_offscreen_render_target_pass_count = 0;
        for (size_t index = 0; index < command_buffers_.size(); ++index) {
            recording_draw_count_ = 0u;
            recording_triangle_count_ = 0u;
            recording_barrier_count_ = 0u;
            VkCommandBufferBeginInfo begin_info{};
            begin_info.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;
            vk_check(vkBeginCommandBuffer(command_buffers_[index], &begin_info), "vkBeginCommandBuffer");
            if (gpu_timing_query_pool_ != VK_NULL_HANDLE) {
                const uint32_t first_query = static_cast<uint32_t>(index) * 2u;
                vkCmdResetQueryPool(
                    command_buffers_[index], gpu_timing_query_pool_, first_query, 2u);
                vkCmdWriteTimestamp(
                    command_buffers_[index],
                    VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,
                    gpu_timing_query_pool_,
                    first_query);
            }

            recorded_offscreen_render_target_pass_count +=
                record_offscreen_render_targets(command_buffers_[index]);

            VkRenderPassBeginInfo render_pass_info{};
            render_pass_info.sType = VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO;
            render_pass_info.renderPass = render_pass_;
            render_pass_info.framebuffer = framebuffers_[index];
            render_pass_info.renderArea.offset = {0, 0};
            render_pass_info.renderArea.extent = swapchain_extent_;
            std::array<VkClearValue, 2> clear_values{};
            clear_values[0] = interpreted_stream.diagnostic_clear_color;
            clear_values[1].depthStencil = {1.0f, 0u};
            render_pass_info.clearValueCount =
                static_cast<uint32_t>(clear_values.size());
            render_pass_info.pClearValues = clear_values.data();

            vkCmdBeginRenderPass(command_buffers_[index], &render_pass_info, VK_SUBPASS_CONTENTS_INLINE);
            record_native_draws(command_buffers_[index]);
            record_frontend_text(command_buffers_[index]);
            vkCmdEndRenderPass(command_buffers_[index]);
            recorded_render_target_feedback_copy_count +=
                record_render_target_feedback(
                    command_buffers_[index],
                    swapchain_images_[index])
                ? 1u
                : 0u;
            if (record_frame_readback) {
                VkImageMemoryBarrier readback_barrier{};
                readback_barrier.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
                readback_barrier.srcAccessMask =
                    VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT
                    | VK_ACCESS_TRANSFER_READ_BIT;
                readback_barrier.dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
                readback_barrier.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
                readback_barrier.newLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
                readback_barrier.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
                readback_barrier.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
                readback_barrier.image = swapchain_images_[index];
                readback_barrier.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
                readback_barrier.subresourceRange.levelCount = 1;
                readback_barrier.subresourceRange.layerCount = 1;
                ++recording_barrier_count_;
                vkCmdPipelineBarrier(
                    command_buffers_[index],
                    VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT
                        | VK_PIPELINE_STAGE_TRANSFER_BIT,
                    VK_PIPELINE_STAGE_TRANSFER_BIT,
                    0,
                    0,
                    nullptr,
                    0,
                    nullptr,
                    1,
                    &readback_barrier);

                VkBufferImageCopy copy{};
                copy.imageSubresource.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
                copy.imageSubresource.mipLevel = 0;
                copy.imageSubresource.baseArrayLayer = 0;
                copy.imageSubresource.layerCount = 1;
                copy.imageExtent = {swapchain_extent_.width, swapchain_extent_.height, 1};
                vkCmdCopyImageToBuffer(
                    command_buffers_[index],
                    swapchain_images_[index],
                    VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                    readback_buffer_,
                    1,
                    &copy);

                VkImageMemoryBarrier present_barrier{};
                present_barrier.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
                present_barrier.srcAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
                present_barrier.dstAccessMask = VK_ACCESS_MEMORY_READ_BIT;
                present_barrier.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
                present_barrier.newLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;
                present_barrier.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
                present_barrier.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
                present_barrier.image = swapchain_images_[index];
                present_barrier.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
                present_barrier.subresourceRange.levelCount = 1;
                present_barrier.subresourceRange.layerCount = 1;
                ++recording_barrier_count_;
                vkCmdPipelineBarrier(
                    command_buffers_[index],
                    VK_PIPELINE_STAGE_TRANSFER_BIT,
                    VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,
                    0,
                    0,
                    nullptr,
                    0,
                    nullptr,
                    1,
                    &present_barrier);
            } else {
                VkImageMemoryBarrier present_barrier{};
                present_barrier.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
                present_barrier.srcAccessMask =
                    VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT
                    | VK_ACCESS_TRANSFER_READ_BIT;
                present_barrier.dstAccessMask = VK_ACCESS_MEMORY_READ_BIT;
                present_barrier.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
                present_barrier.newLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;
                present_barrier.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
                present_barrier.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
                present_barrier.image = swapchain_images_[index];
                present_barrier.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
                present_barrier.subresourceRange.levelCount = 1;
                present_barrier.subresourceRange.layerCount = 1;
                ++recording_barrier_count_;
                vkCmdPipelineBarrier(
                    command_buffers_[index],
                    VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT
                        | VK_PIPELINE_STAGE_TRANSFER_BIT,
                    VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,
                    0,
                    0,
                    nullptr,
                    0,
                    nullptr,
                    1,
                    &present_barrier);
            }
            if (gpu_timing_query_pool_ != VK_NULL_HANDLE) {
                vkCmdWriteTimestamp(
                    command_buffers_[index],
                    VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,
                    gpu_timing_query_pool_,
                    static_cast<uint32_t>(index) * 2u + 1u);
            }
            vk_check(vkEndCommandBuffer(command_buffers_[index]), "vkEndCommandBuffer");
            command_buffer_draw_counts_[index] = recording_draw_count_;
            command_buffer_triangle_counts_[index] = recording_triangle_count_;
            command_buffer_barrier_counts_[index] = recording_barrier_count_;
        }
        recording_draw_count_ = 0u;
        recording_triangle_count_ = 0u;
        recording_barrier_count_ = 0u;
        log_.emit(
            "recovered_d3d_command_stream_loaded",
            {
                {"commands", std::to_string(recovered_d3d_stream.size())},
                {"mmio_writes", std::to_string(recovered_d3d_mmio_count_)},
                {"push_buffer_writes", std::to_string(recovered_d3d_push_buffer_count_)},
                {"source", json_string(recovered_source_.source)},
                {"interpreter_bootstrap", recovered_source_.interpreter_bootstrap_source.empty()
                    ? "null"
                    : json_string(recovered_source_.interpreter_bootstrap_source.string())},
                {"interpreter_bootstrap_applied", json_bool(
                    !recovered_source_.interpreter_bootstrap_source.empty())},
            });
        log_.emit(
            "d3d8_stream_interpreted",
            {
                {"push_buffer_words", std::to_string(interpreted_stream.push_buffer_word_count)},
                {"method_packets", std::to_string(interpreted_stream.method_packet_count)},
                {"interpreted_methods", std::to_string(interpreted_stream.interpreted_method_count)},
                {"bulk_indexed_methods", std::to_string(
                    interpreted_stream.bulk_indexed_method_count)},
                {"bulk_inline_methods", std::to_string(
                    interpreted_stream.bulk_inline_method_count)},
                {"last_interpreted_methods", std::to_string(
                    interpreted_stream.last_interpreted_method_count)},
                {"last_bulk_indexed_methods", std::to_string(
                    interpreted_stream.last_bulk_indexed_method_count)},
                {"last_bulk_inline_methods", std::to_string(
                    interpreted_stream.last_bulk_inline_method_count)},
                {"push_buffer_collect_us", std::to_string(
                    interpreted_stream.last_push_buffer_collect_us)},
                {"method_apply_us", std::to_string(
                    interpreted_stream.last_method_apply_us)},
                {"method_finalize_us", std::to_string(
                    interpreted_stream.last_method_finalize_us)},
                {"state_seed_updates_required", json_bool(
                    interpreted_stream.state_seed_updates_required)},
                {"zero_count_method_words", std::to_string(interpreted_stream.zero_count_method_word_count)},
                {"zero_count_indexed_array_noop_packets", std::to_string(interpreted_stream.zero_count_indexed_array_noop_packet_count)},
                {"control_flow_packets", std::to_string(interpreted_stream.control_flow_packet_count)},
                {"mmio_setup_writes", std::to_string(interpreted_stream.mmio_setup_write_count)},
                {"submission_kicks", std::to_string(interpreted_stream.submission_kick_count)},
                {"unknown_packets", std::to_string(interpreted_stream.unknown_packet_count)},
                {"truncated_packets", std::to_string(interpreted_stream.truncated_packet_count)},
                {"pending_method_packet", json_bool(interpreted_stream.pending_method_packet)},
                {"converted_quad_count", std::to_string(interpreted_stream.converted_quad_count)},
                {"discarded_quad_vertex_count", std::to_string(interpreted_stream.discarded_quad_vertex_count)},
                {"indexed_array_draws", std::to_string(interpreted_stream.indexed_array_draw_count)},
                {"indexed_array_elements", std::to_string(interpreted_stream.indexed_array_element_count)},
                {"materialized_indexed_draws", std::to_string(interpreted_stream.materialized_indexed_draw_count)},
                {"materialized_indexed_vertices", std::to_string(interpreted_stream.materialized_indexed_vertex_count)},
                {"missing_indexed_resource_draws", std::to_string(interpreted_stream.missing_indexed_resource_draw_count)},
                {"launch_transform_program_count", std::to_string(interpreted_stream.launch_transform_program_count)},
                {"failed_launch_transform_program_count", std::to_string(interpreted_stream.failed_launch_transform_program_count)},
                {"surface_payload_samples", std::to_string(interpreted_stream.surface_payload_sample_count)},
                {"surface_payload_dominant_count", std::to_string(interpreted_stream.surface_payload_dominant_count)},
                {"surface_payload_color_valid", interpreted_stream.surface_payload_color_valid ? "true" : "false"},
                {"surface_payload_argb", std::to_string(interpreted_stream.surface_payload_argb)},
                {"surface_payload_scans_skipped", std::to_string(
                    interpreted_stream.surface_payload_scan_skipped_count)},
                {"ordered_push_buffer_appends", std::to_string(
                    interpreted_stream.ordered_push_buffer_append_count)},
                {"indexed_word_push_buffer_appends", std::to_string(
                    interpreted_stream.indexed_word_push_buffer_append_count)},
                {"reconstructed_push_buffer_appends", std::to_string(
                    interpreted_stream.reconstructed_push_buffer_append_count)},
                {
                    "diagnostic_clear_source",
                    json_string(
                        interpreted_stream.clear_color_valid ? "d3d8_clear_color_method" :
                        interpreted_stream.surface_payload_color_valid ? "recovered_surface_payload" :
                        "state_seed")
                },
                {"state_seed", std::to_string(interpreted_stream.state_seed)},
                {"clear_color_valid", interpreted_stream.clear_color_valid ? "true" : "false"},
                {"clear_color_argb", std::to_string(interpreted_stream.clear_color_argb)},
                {"native_vertices", std::to_string(interpreted_stream.vertices.size())},
                {"native_draws", std::to_string(interpreted_stream.draws.size())},
                {"translation_semantics", json_string("d3d8-nv2a-method-push-buffer-interpretation")},
            });
        if (!options_.live_render_stream) {
            for (size_t index = 0; index < recovered_d3d_stream.size(); ++index) {
                const RecoveredD3DCommand& command = recovered_d3d_stream[index];
                log_.emit(
                    "recovered_d3d_command",
                    {
                        {"index", std::to_string(index)},
                        {"kind", json_string(recovered_d3d_command_kind_name(command.kind))},
                        {"address", std::to_string(command.address)},
                        {"value", std::to_string(command.value)},
                    });
            }
        }
        log_.emit(
            "translated_render_work_recorded",
            {
                {"commands", std::to_string(translated_command_count_)},
                {"source_commands", std::to_string(recovered_d3d_command_count_)},
                {"method_packets", std::to_string(interpreted_stream.method_packet_count)},
                {"interpreted_methods", std::to_string(interpreted_stream.interpreted_method_count)},
                {"zero_count_method_words", std::to_string(interpreted_stream.zero_count_method_word_count)},
                {"zero_count_indexed_array_noop_packets", std::to_string(interpreted_stream.zero_count_indexed_array_noop_packet_count)},
                {"native_vertices", std::to_string(interpreted_stream.vertices.size())},
                {"native_draws", std::to_string(interpreted_stream.draws.size())},
                {"native_textures", std::to_string(host_textures_.size() - 1u)},
                {"translation_semantics", json_string("d3d8-nv2a-method-push-buffer-interpretation")},
            });
        if (!recovered_frontend_text_.empty() && frontend_text_rectangle_count_ != 0u) {
            log_.emit(
                "frontend_text_draw_recorded",
                {
                    {"characters", std::to_string(recovered_frontend_text_.size())},
                    {"rectangles", std::to_string(frontend_text_rectangle_count_)},
                    {"text", json_string(recovered_frontend_text_)},
                    {"x", json_float(float_from_u32(
                        recovered_source_.frontend_text_x_bits))},
                    {"y", json_float(float_from_u32(
                        recovered_source_.frontend_text_y_bits))},
                    {"size", json_float(float_from_u32(
                        recovered_source_.frontend_text_size_bits))},
                    {"color_argb", std::to_string(
                        recovered_source_.frontend_text_color_argb)},
                    {"opacity", json_float(static_cast<float>(
                        (recovered_source_.frontend_text_color_argb >> 24u)
                            & 0xFFu) / 255.0f)},
                    {"font", json_string("Impact")},
                    {"translation_semantics", json_string("recovered-title-text-hle")},
                });
        }
        log_.emit(
            "command_buffers_recorded",
            {
                {"count", std::to_string(command_buffers_.size())},
                {"translated_commands", std::to_string(translated_command_count_)},
                {"source_d3d_commands", std::to_string(recovered_d3d_stream.size())},
                {"render_target_feedback_copies", std::to_string(recorded_render_target_feedback_copy_count)},
                {"offscreen_render_target_passes", std::to_string(recorded_offscreen_render_target_pass_count)},
            });
    }

    void create_sync_objects() {
        VkSemaphoreCreateInfo semaphore_info{};
        semaphore_info.sType = VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO;
        VkFenceCreateInfo fence_info{};
        fence_info.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO;
        fence_info.flags = VK_FENCE_CREATE_SIGNALED_BIT;
        vk_check(vkCreateSemaphore(device_, &semaphore_info, nullptr, &image_available_), "vkCreateSemaphore");
        vk_check(vkCreateSemaphore(device_, &semaphore_info, nullptr, &render_finished_), "vkCreateSemaphore");
        vk_check(vkCreateFence(device_, &fence_info, nullptr, &in_flight_), "vkCreateFence");
        log_.emit("sync_objects_created");
    }

    bool readback_enabled() const {
        return !options_.screenshot.empty()
            || !options_.hotkey_screenshot_directory.empty()
            || !options_.flip_audit_frame_directory.empty();
    }

    std::filesystem::path current_flip_audit_frame_path() const {
        std::wostringstream name;
        name << L"flip-" << std::setfill(L'0') << std::setw(6)
             << current_audit_flip_ << L".bmp";
        return options_.flip_audit_frame_directory / name.str();
    }

    bool acknowledge_current_presentation() {
        if (presentation_ack_path_.empty()
            || current_manifest_guest_flip_count_ == 0u
            || current_manifest_guest_flip_count_ <= acknowledged_presentation_flip_) {
            return false;
        }
        if (presentation_ack_path_.has_parent_path()) {
            std::filesystem::create_directories(
                presentation_ack_path_.parent_path());
        }
        if (presentation_ack_file_ == INVALID_HANDLE_VALUE) {
            presentation_ack_file_ = CreateFileW(
                presentation_ack_path_.c_str(),
                GENERIC_WRITE,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                nullptr,
                OPEN_ALWAYS,
                FILE_ATTRIBUTE_NORMAL,
                nullptr);
        }
        if (presentation_ack_file_ == INVALID_HANDLE_VALUE) {
            throw std::runtime_error(
                "cannot open live presentation acknowledgement");
        }
        std::array<uint8_t, 24> ack{};
        std::memcpy(ack.data(), "B2PRS001", 8);
        const uint64_t generation = std::stoull(live_command_generation_);
        std::memcpy(ack.data() + 8, &generation, sizeof(generation));
        std::memcpy(
            ack.data() + 16,
            &current_manifest_guest_flip_count_,
            sizeof(current_manifest_guest_flip_count_));
        LARGE_INTEGER start{};
        DWORD written = 0;
        const BOOL write_ok = SetFilePointerEx(
                presentation_ack_file_, start, nullptr, FILE_BEGIN)
            && WriteFile(
                presentation_ack_file_,
                ack.data(),
                static_cast<DWORD>(ack.size()),
                &written,
                nullptr)
            && SetEndOfFile(presentation_ack_file_);
        if (!write_ok || written != ack.size()) {
            throw std::runtime_error(
                "cannot publish live presentation acknowledgement");
        }
        acknowledged_presentation_flip_ = current_manifest_guest_flip_count_;
        if (!presentation_ack_event_ && !presentation_event_name_.empty()) {
            presentation_ack_event_ = OpenEventW(
                EVENT_MODIFY_STATE,
                FALSE,
                widen(presentation_event_name_).c_str());
        }
        if (presentation_ack_event_) {
            SetEvent(presentation_ack_event_);
        }
        return true;
    }

    void acknowledge_current_flip_audit(
        const FrameReadback* readback,
        const std::filesystem::path& frame_path,
        const std::string& health_check_reason) {
        if (options_.flip_audit_ack.empty()
            || current_audit_flip_ == 0u
            || current_audit_flip_ <= acknowledged_audit_flip_) {
            return;
        }
        if (options_.flip_audit_ack.has_parent_path()) {
            std::filesystem::create_directories(
                options_.flip_audit_ack.parent_path());
        }
        const HANDLE ack_file = CreateFileW(
            options_.flip_audit_ack.c_str(),
            GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            nullptr,
            CREATE_ALWAYS,
            FILE_ATTRIBUTE_NORMAL,
            nullptr);
        if (ack_file == INVALID_HANDLE_VALUE) {
            throw std::runtime_error(
                "cannot open lossless flip audit acknowledgement");
        }
        std::array<uint8_t, 20> ack{};
        std::memcpy(ack.data(), "B2ACK001", 8);
        const uint64_t generation = std::stoull(current_audit_generation_);
        std::memcpy(ack.data() + 8, &generation, sizeof(generation));
        std::memcpy(
            ack.data() + 16, &current_audit_flip_, sizeof(current_audit_flip_));
        DWORD written = 0;
        const BOOL write_ok = WriteFile(
            ack_file,
            ack.data(),
            static_cast<DWORD>(ack.size()),
            &written,
            nullptr);
        CloseHandle(ack_file);
        if (!write_ok || written != ack.size()) {
            throw std::runtime_error(
                "cannot publish lossless flip audit acknowledgement");
        }
        acknowledged_audit_flip_ = current_audit_flip_;
        const uint32_t command_delta = current_audit_command_count_
            - std::min(current_audit_command_count_, last_audit_command_count_);
        last_audit_command_count_ = current_audit_command_count_;
        last_audit_command_delta_ = command_delta;
        last_audit_flip_value_ = current_audit_flip_value_;
        last_audit_resource_generation_ = live_resource_generation_;
        std::ostringstream fingerprint;
        if (readback != nullptr) {
            fingerprint << std::hex << std::setfill('0') << std::setw(16)
                        << readback->pixel_fingerprint;
        }
        const bool health_checked = readback != nullptr;
        log_.emit(
            "lossless_flip_audited",
            {
                {"flip_index", std::to_string(current_audit_flip_)},
                {"guest_steps", std::to_string(current_audit_guest_steps_)},
                {"command_record_count", std::to_string(current_audit_command_count_)},
                {"host_frame", std::to_string(frame_count_ + 1u)},
                {"health_checked", json_bool(health_checked)},
                {"health_check_reason", json_string(
                    health_checked ? health_check_reason : "not_selected")},
                {"readback_captured", json_bool(health_checked)},
                {"pixel_fingerprint", health_checked
                    ? json_string(fingerprint.str()) : "null"},
                {"pixel_count", std::to_string(health_checked ? readback->pixel_count : 0u)},
                {"unique_colors", std::to_string(health_checked ? readback->unique_colors : 0u)},
                {"dominant_rgba", std::to_string(health_checked ? readback->dominant_rgba : 0u)},
                {"dominant_count", std::to_string(health_checked ? readback->dominant_count : 0u)},
                {"bright_count", std::to_string(health_checked ? readback->bright_count : 0u)},
                {"dark_count", std::to_string(health_checked ? readback->dark_count : 0u)},
                {"near_solid_frame", json_bool(health_checked && readback->near_solid)},
                {"whiteout_frame", json_bool(health_checked && readback->whiteout)},
                {"low_information_frame", json_bool(health_checked && readback->low_information)},
                {"visual_issue", json_bool(health_checked && readback->visual_issue())},
                {"frame_saved", json_bool(!frame_path.empty())},
                {"frame_path", json_string(frame_path.string())},
                {"ack_transport", json_string("binary")},
            });
        if (options_.flip_audit_max_flips != 0u
            && acknowledged_audit_flip_ >= options_.flip_audit_max_flips) {
            running_ = false;
        }
    }

    void main_loop() {
        log_.emit("main_loop_enter");
        poll_host_controller(false);
        write_controller_state();
        auto last_frame = std::chrono::steady_clock::now();
        auto next_frame_time = last_frame + kTargetFrameInterval;
        while (running_ && (options_.max_frames == 0u || frame_count_ < options_.max_frames)) {
            const auto frame_start = std::chrono::steady_clock::now();
            active_frame_fence_wait_us_ = 0u;
            last_window_message_pump_us_ = 0u;
            last_controller_poll_us_ = 0u;
            last_keyboard_latch_us_ = 0u;
            last_reload_probe_us_ = 0u;
            last_reload_dispatch_us_ = 0u;
            last_pre_render_unattributed_us_ = 0u;
            last_draw_fence_wait_us_ = 0u;
            last_reload_fence_wait_us_ = 0u;
            last_gpu_query_us_ = 0u;
            last_fence_reset_us_ = 0u;
            last_acquire_us_ = 0u;
            last_submit_us_ = 0u;
            last_readback_wait_us_ = 0u;
            last_readback_process_us_ = 0u;
            last_present_us_ = 0u;
            last_audit_ack_us_ = 0u;
            last_draw_unattributed_us_ = 0u;

            auto stage_begin = std::chrono::steady_clock::now();
            pump_window_messages();
            last_window_message_pump_us_ = static_cast<uint64_t>(
                std::chrono::duration_cast<std::chrono::microseconds>(
                    std::chrono::steady_clock::now() - stage_begin).count());
            stage_begin = std::chrono::steady_clock::now();
            poll_host_controller();
            last_controller_poll_us_ = static_cast<uint64_t>(
                std::chrono::duration_cast<std::chrono::microseconds>(
                    std::chrono::steady_clock::now() - stage_begin).count());
            stage_begin = std::chrono::steady_clock::now();
            update_keyboard_button_latches();
            last_keyboard_latch_us_ = static_cast<uint64_t>(
                std::chrono::duration_cast<std::chrono::microseconds>(
                    std::chrono::steady_clock::now() - stage_begin).count());
            if (!running_) {
                break;
            }
            const auto reload_dispatch_begin = std::chrono::steady_clock::now();
            reload_live_render_work();
            last_reload_dispatch_us_ = static_cast<uint64_t>(
                std::chrono::duration_cast<std::chrono::microseconds>(
                    std::chrono::steady_clock::now()
                    - reload_dispatch_begin).count());
            if (options_.inject_input && !input_injected_) {
                PostMessageW(hwnd_, WM_KEYDOWN, VK_SPACE, 0);
                PostMessageW(hwnd_, WM_KEYUP, VK_SPACE, 0);
                input_injected_ = true;
                log_.emit("input_injected", {{"virtual_key", std::to_string(VK_SPACE)}});
                const auto injected_pump_begin = std::chrono::steady_clock::now();
                const uint32_t pumped_messages = pump_window_messages();
                last_window_message_pump_us_ += static_cast<uint64_t>(
                    std::chrono::duration_cast<std::chrono::microseconds>(
                        std::chrono::steady_clock::now()
                        - injected_pump_begin).count());
                log_.emit(
                    "input_injection_processed",
                    {
                        {"messages", std::to_string(pumped_messages)},
                        {"input_events", std::to_string(input_events_)},
                        {"before_frame", std::to_string(frame_count_ + 1u)},
                    });
                if (!running_) {
                    break;
                }
            }
            const auto cpu_render_begin = std::chrono::steady_clock::now();
            const uint64_t pre_render_us = static_cast<uint64_t>(
                std::chrono::duration_cast<std::chrono::microseconds>(
                    cpu_render_begin - frame_start).count());
            const uint64_t attributed_pre_render_us =
                last_window_message_pump_us_
                + last_controller_poll_us_
                + last_keyboard_latch_us_
                + last_reload_dispatch_us_;
            last_pre_render_unattributed_us_ = pre_render_us
                > attributed_pre_render_us
                ? pre_render_us - attributed_pre_render_us
                : 0u;
            draw_frame();
            last_cpu_render_us_ = std::chrono::duration_cast<
                std::chrono::microseconds>(
                    std::chrono::steady_clock::now() - cpu_render_begin).count();
            const uint64_t attributed_draw_us =
                last_draw_fence_wait_us_
                + last_gpu_query_us_
                + last_fence_reset_us_
                + last_acquire_us_
                + last_submit_us_
                + last_readback_wait_us_
                + last_readback_process_us_
                + last_present_us_
                + last_audit_ack_us_;
            last_draw_unattributed_us_ = last_cpu_render_us_
                > attributed_draw_us
                ? last_cpu_render_us_ - attributed_draw_us
                : 0u;
            const uint64_t draw_frame_fence_wait_us =
                last_draw_fence_wait_us_ + last_readback_wait_us_;
            last_reload_fence_wait_us_ = active_frame_fence_wait_us_
                > draw_frame_fence_wait_us
                ? active_frame_fence_wait_us_ - draw_frame_fence_wait_us
                : 0u;
            ++frame_count_;
            const auto after_draw = std::chrono::steady_clock::now();
            const auto draw_us =
                std::chrono::duration_cast<std::chrono::microseconds>(after_draw - frame_start).count();
            const auto sleep_us = std::max<int64_t>(
                0,
                std::chrono::duration_cast<std::chrono::microseconds>(next_frame_time - after_draw).count());
            if (sleep_us > 0) {
                wait_for_frame_deadline(next_frame_time);
            }
            const auto now = std::chrono::steady_clock::now();
            const auto elapsed_us =
                std::chrono::duration_cast<std::chrono::microseconds>(now - last_frame).count();
            last_frame_elapsed_us_ = elapsed_us;
            last_fence_wait_us_ = active_frame_fence_wait_us_;
            last_frame = now;
            next_frame_time += kTargetFrameInterval;
            if (next_frame_time <= now) {
                next_frame_time = now + kTargetFrameInterval;
            }
            update_fps_counter(now);
            log_.emit(
                "frame_presented",
                {
                    {"frame", std::to_string(frame_count_)},
                    {"elapsed_us", std::to_string(elapsed_us)},
                    {"draw_us", std::to_string(draw_us)},
                    {"cpu_render_us", std::to_string(last_cpu_render_us_)},
                    {"window_message_pump_us", std::to_string(
                        last_window_message_pump_us_)},
                    {"controller_poll_us", std::to_string(
                        last_controller_poll_us_)},
                    {"keyboard_latch_us", std::to_string(
                        last_keyboard_latch_us_)},
                    {"reload_probe_us", std::to_string(
                        last_reload_probe_us_)},
                    {"reload_dispatch_us", std::to_string(
                        last_reload_dispatch_us_)},
                    {"pre_render_unattributed_us", std::to_string(
                        last_pre_render_unattributed_us_)},
                    {"reload_fence_wait_us", std::to_string(
                        last_reload_fence_wait_us_)},
                    {"draw_fence_wait_us", std::to_string(
                        last_draw_fence_wait_us_)},
                    {"gpu_query_us", std::to_string(last_gpu_query_us_)},
                    {"fence_reset_us", std::to_string(last_fence_reset_us_)},
                    {"acquire_us", std::to_string(last_acquire_us_)},
                    {"submit_us", std::to_string(last_submit_us_)},
                    {"readback_wait_us", std::to_string(
                        last_readback_wait_us_)},
                    {"readback_process_us", std::to_string(
                        last_readback_process_us_)},
                    {"present_us", std::to_string(last_present_us_)},
                    {"audit_ack_us", std::to_string(last_audit_ack_us_)},
                    {"draw_unattributed_us", std::to_string(
                        last_draw_unattributed_us_)},
                    {"target_frame_us", std::to_string(kTargetFrameUs)},
                    {"pacing_sleep_us", std::to_string(sleep_us)},
                    {"elapsed_ms", std::to_string(elapsed_us / 1000)},
                    {"draw_ms", std::to_string(draw_us / 1000)},
                    {"target_frame_ms", "16.666667"},
                    {"pacing_sleep_ms", std::to_string(sleep_us / 1000)},
                    {"translated_commands", std::to_string(translated_command_count_)},
                    {"source_d3d_commands", std::to_string(recovered_d3d_command_count_)},
                    {"push_buffer_words", std::to_string(interpreted_push_buffer_word_count_)},
                    {"method_packets", std::to_string(interpreted_method_packet_count_)},
                    {"interpreted_methods", std::to_string(interpreted_method_count_)},
                    {"zero_count_method_words", std::to_string(interpreted_zero_count_method_word_count_)},
        });
    }

        log_.emit(
            "main_loop_exit",
            {
                {"frames", std::to_string(frame_count_)},
                {"input_events", std::to_string(input_events_)},
                {"closed_by_user", json_bool(closed_by_user_)},
            });
    }

    void set_fps_counter_title(std::optional<double> fps) const {
        if (hwnd_ == nullptr) {
            return;
        }
        std::wostringstream title;
        title << options_.title << L" | Game FPS: ";
        if (fps.has_value()) {
            title << std::fixed << std::setprecision(1) << *fps;
        } else {
            title << L"--";
        }
        SetWindowTextW(hwnd_, title.str().c_str());
    }

    void toggle_fps_counter() {
        fps_counter_enabled_ = !fps_counter_enabled_;
        fps_counter_sample_start_ = std::chrono::steady_clock::now();
        fps_counter_last_guest_flip_count_ =
            current_manifest_guest_flip_count_;
        if (fps_counter_enabled_) {
            set_fps_counter_title(std::nullopt);
        } else if (hwnd_ != nullptr) {
            SetWindowTextW(hwnd_, options_.title.c_str());
        }
        log_.emit(
            "fps_counter_toggled",
            {
                {"enabled", json_bool(fps_counter_enabled_)},
                {"source", json_string("completed_guest_flips")},
                {"manifest_guest_flip_count", std::to_string(
                    current_manifest_guest_flip_count_)},
            });
    }

    void update_fps_counter(
        std::chrono::steady_clock::time_point now) {
        if (!fps_counter_enabled_) {
            return;
        }
        const auto elapsed = now - fps_counter_sample_start_;
        if (elapsed < kFpsCounterSampleInterval) {
            return;
        }
        const uint64_t completed_flips =
            current_manifest_guest_flip_count_
                >= fps_counter_last_guest_flip_count_
            ? current_manifest_guest_flip_count_
                - fps_counter_last_guest_flip_count_
            : 0u;
        const double elapsed_seconds =
            std::chrono::duration<double>(elapsed).count();
        set_fps_counter_title(
            elapsed_seconds > 0.0
                ? std::optional<double>(
                    static_cast<double>(completed_flips) / elapsed_seconds)
                : std::optional<double>(0.0));
        fps_counter_sample_start_ = now;
        fps_counter_last_guest_flip_count_ =
            current_manifest_guest_flip_count_;
    }

    void sleep_until_frame_deadline(
        std::chrono::steady_clock::time_point deadline) {
        const auto now = std::chrono::steady_clock::now();
        if (deadline <= now) {
            return;
        }
        if (!frame_pacing_timer_) {
            frame_pacing_timer_ = CreateWaitableTimerExW(
                nullptr,
                nullptr,
                kHighResolutionWaitableTimerFlag,
                TIMER_ALL_ACCESS);
        }
        if (frame_pacing_timer_) {
            const int64_t remaining_ns = std::chrono::duration_cast<
                std::chrono::nanoseconds>(deadline - now).count();
            LARGE_INTEGER due_time{};
            due_time.QuadPart = -std::max<int64_t>(
                1,
                (remaining_ns + 99) / 100);
            if (SetWaitableTimerEx(
                    frame_pacing_timer_,
                    &due_time,
                    0,
                    nullptr,
                    nullptr,
                    nullptr,
                    0)) {
                WaitForSingleObject(frame_pacing_timer_, INFINITE);
                return;
            }
        }
        std::this_thread::sleep_until(deadline);
    }

    void wait_for_frame_deadline(
        std::chrono::steady_clock::time_point deadline) {
        if (!options_.live_render_stream || presentation_ack_path_.empty()) {
            sleep_until_frame_deadline(deadline);
            return;
        }
        if (publication_event_) {
            const auto now = std::chrono::steady_clock::now();
            if (deadline <= now) {
                return;
            }
            if (!frame_pacing_timer_) {
                frame_pacing_timer_ = CreateWaitableTimerExW(
                    nullptr,
                    nullptr,
                    kHighResolutionWaitableTimerFlag,
                    TIMER_ALL_ACCESS);
            }
            if (frame_pacing_timer_) {
                const int64_t remaining_ns = std::chrono::duration_cast<
                    std::chrono::nanoseconds>(deadline - now).count();
                LARGE_INTEGER due_time{};
                due_time.QuadPart = -std::max<int64_t>(
                    1,
                    (remaining_ns + 99) / 100);
                if (SetWaitableTimerEx(
                        frame_pacing_timer_,
                        &due_time,
                        0,
                        nullptr,
                        nullptr,
                        nullptr,
                        0)) {
                    const HANDLE waits[] = {
                        frame_pacing_timer_,
                        publication_event_,
                    };
                    const DWORD result = WaitForMultipleObjects(
                        2, waits, FALSE, INFINITE);
                    if (result == WAIT_OBJECT_0 + 1u) {
                        reload_live_render_work(true);
                        sleep_until_frame_deadline(deadline);
                    }
                    if (result == WAIT_OBJECT_0 || result == WAIT_OBJECT_0 + 1u) {
                        return;
                    }
                }
            }
        }
        // A normal live guest waits for acknowledgement of every exact flip.
        // Poll the tiny manifest while this frame is otherwise idle so command
        // ingestion overlaps presentation rather than serializing an entire
        // 16.7 ms host interval ahead of the guest's next frame computation.
        constexpr auto poll_interval = std::chrono::milliseconds(1);
        while (running_) {
            reload_live_render_work();
            const auto now = std::chrono::steady_clock::now();
            if (now >= deadline) {
                return;
            }
            sleep_until_frame_deadline(std::min(deadline, now + poll_interval));
        }
    }

    uint32_t pump_window_messages() {
        uint32_t pumped = 0;
        MSG message{};
        while (PeekMessageW(&message, nullptr, 0, 0, PM_REMOVE)) {
            ++pumped;
            if (message.message == WM_QUIT) {
                running_ = false;
            }
            TranslateMessage(&message);
            DispatchMessageW(&message);
        }
        return pumped;
    }

    uint64_t wait_for_in_flight_fence(const char* operation) {
        const auto wait_begin = std::chrono::steady_clock::now();
        const VkResult result = vkWaitForFences(
            device_, 1, &in_flight_, VK_TRUE, UINT64_MAX);
        const uint64_t wait_us = static_cast<uint64_t>(
            std::chrono::duration_cast<std::chrono::microseconds>(
                std::chrono::steady_clock::now() - wait_begin).count());
        active_frame_fence_wait_us_ += wait_us;
        vk_check(result, operation);
        return wait_us;
    }

    void update_gpu_frame_time() {
        if (gpu_timing_query_pool_ == VK_NULL_HANDLE
            || last_submitted_image_index_
                == std::numeric_limits<uint32_t>::max()) {
            return;
        }
        std::array<uint64_t, 2> timestamps{};
        const VkResult result = vkGetQueryPoolResults(
            device_,
            gpu_timing_query_pool_,
            last_submitted_image_index_ * 2u,
            2u,
            sizeof(timestamps),
            timestamps.data(),
            sizeof(uint64_t),
            VK_QUERY_RESULT_64_BIT);
        if (result != VK_SUCCESS) {
            return;
        }
        uint64_t tick_delta = timestamps[1] - timestamps[0];
        if (gpu_timestamp_valid_bits_ < 64u) {
            const uint64_t mask = (uint64_t{1} << gpu_timestamp_valid_bits_) - 1u;
            tick_delta = (timestamps[1] - timestamps[0]) & mask;
        }
        last_gpu_frame_ms_ = static_cast<double>(tick_delta)
            * static_cast<double>(gpu_timestamp_period_ns_) / 1'000'000.0;
        gpu_frame_time_valid_ = true;
    }

    void draw_frame() {
        last_draw_fence_wait_us_ =
            wait_for_in_flight_fence("vkWaitForFences");
        const auto gpu_query_begin = std::chrono::steady_clock::now();
        update_gpu_frame_time();
        last_gpu_query_us_ = static_cast<uint64_t>(
            std::chrono::duration_cast<std::chrono::microseconds>(
                std::chrono::steady_clock::now() - gpu_query_begin).count());
        const auto fence_reset_begin = std::chrono::steady_clock::now();
        vk_check(vkResetFences(device_, 1, &in_flight_), "vkResetFences");
        last_fence_reset_us_ = static_cast<uint64_t>(
            std::chrono::duration_cast<std::chrono::microseconds>(
                std::chrono::steady_clock::now() - fence_reset_begin).count());

        uint32_t image_index = 0;
        const auto acquire_begin = std::chrono::steady_clock::now();
        VkResult acquire = vkAcquireNextImageKHR(
            device_, swapchain_, UINT64_MAX, image_available_, VK_NULL_HANDLE, &image_index);
        last_acquire_us_ = static_cast<uint64_t>(
            std::chrono::duration_cast<std::chrono::microseconds>(
                std::chrono::steady_clock::now() - acquire_begin).count());
        if (acquire == VK_ERROR_OUT_OF_DATE_KHR) {
            running_ = false;
            log_.emit("swapchain_out_of_date");
            return;
        }
        vk_check(acquire, "vkAcquireNextImageKHR");

        VkSemaphore wait_semaphores[] = {image_available_};
        VkPipelineStageFlags wait_stages[] = {VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT};
        VkSemaphore signal_semaphores[] = {render_finished_};

        VkSubmitInfo submit_info{};
        submit_info.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO;
        submit_info.waitSemaphoreCount = 1;
        submit_info.pWaitSemaphores = wait_semaphores;
        submit_info.pWaitDstStageMask = wait_stages;
        submit_info.commandBufferCount = 1;
        submit_info.pCommandBuffers = &command_buffers_[image_index];
        submit_info.signalSemaphoreCount = 1;
        submit_info.pSignalSemaphores = signal_semaphores;
        const auto submit_begin = std::chrono::steady_clock::now();
        vk_check(vkQueueSubmit(graphics_queue_, 1, &submit_info, in_flight_), "vkQueueSubmit");
        last_submit_us_ = static_cast<uint64_t>(
            std::chrono::duration_cast<std::chrono::microseconds>(
                std::chrono::steady_clock::now() - submit_begin).count());
        ++queue_submission_count_;
        if (image_index < command_buffer_draw_counts_.size()) {
            draw_count_ += command_buffer_draw_counts_[image_index];
            triangle_count_ += command_buffer_triangle_counts_[image_index];
            barrier_count_ += command_buffer_barrier_counts_[image_index];
        }
        if (current_work_has_readback_) {
            readback_bytes_ += static_cast<uint64_t>(readback_size_);
        }
        last_submitted_image_index_ = image_index;

        const bool capture_automatic =
            !options_.screenshot.empty() && !screenshot_captured_;
        const bool capture_flip_audit =
            !options_.flip_audit_frame_directory.empty()
            && current_audit_flip_ > acknowledged_audit_flip_;
        std::filesystem::path flip_audit_frame_path;
        std::optional<FrameReadback> flip_audit_readback;
        const std::string audit_health_reason = capture_flip_audit
            ? flip_audit_health_check_reason() : std::string{};
        const bool analyze_flip_health = capture_flip_audit
            && !audit_health_reason.empty() && current_work_has_readback_;
        const bool capture_hotkey = hotkey_screenshot_pending_
            && current_work_has_readback_;
        if (capture_automatic || capture_hotkey || analyze_flip_health) {
            last_readback_wait_us_ =
                wait_for_in_flight_fence("vkWaitForFences(readback)");
            const auto readback_process_begin =
                std::chrono::steady_clock::now();
            if (capture_automatic) {
                capture_screenshot(options_.screenshot, "automatic");
                screenshot_captured_ = true;
            }
            if (capture_hotkey) {
                capture_screenshot(pending_hotkey_screenshot_path_, "f12");
                hotkey_screenshot_pending_ = false;
                pending_hotkey_screenshot_path_.clear();
                pending_hotkey_render_capture_manifest_.clear();
            }
            if (analyze_flip_health) {
                flip_audit_readback = read_frame();
                if (flip_audit_readback->visual_issue()) {
                    flip_audit_frame_path = current_flip_audit_frame_path();
                    write_screenshot(
                        flip_audit_frame_path,
                        *flip_audit_readback,
                        "lossless_flip_audit_issue");
                }
            }
            last_readback_process_us_ = static_cast<uint64_t>(
                std::chrono::duration_cast<std::chrono::microseconds>(
                    std::chrono::steady_clock::now()
                    - readback_process_begin).count());
        }

        VkPresentInfoKHR present_info{};
        present_info.sType = VK_STRUCTURE_TYPE_PRESENT_INFO_KHR;
        present_info.waitSemaphoreCount = 1;
        present_info.pWaitSemaphores = signal_semaphores;
        present_info.swapchainCount = 1;
        present_info.pSwapchains = &swapchain_;
        present_info.pImageIndices = &image_index;
        const auto present_begin = std::chrono::steady_clock::now();
        const VkResult present = vkQueuePresentKHR(graphics_queue_, &present_info);
        last_present_us_ = static_cast<uint64_t>(
            std::chrono::duration_cast<std::chrono::microseconds>(
                std::chrono::steady_clock::now() - present_begin).count());
        if (present == VK_ERROR_OUT_OF_DATE_KHR || present == VK_SUBOPTIMAL_KHR) {
            running_ = false;
            log_.emit("swapchain_present_suboptimal", {{"result", std::to_string(present)}});
            return;
        }
        vk_check(present, "vkQueuePresentKHR");
        if (capture_flip_audit) {
            const auto audit_ack_begin = std::chrono::steady_clock::now();
            acknowledge_current_flip_audit(
                flip_audit_readback ? &*flip_audit_readback : nullptr,
                flip_audit_frame_path,
                audit_health_reason);
            last_audit_ack_us_ = static_cast<uint64_t>(
                std::chrono::duration_cast<std::chrono::microseconds>(
                    std::chrono::steady_clock::now()
                    - audit_ack_begin).count());
        }
    }

    static void write_u16(std::ofstream& output, uint16_t value) {
        const char bytes[2] = {
            static_cast<char>(value & 0xffu),
            static_cast<char>((value >> 8u) & 0xffu),
        };
        output.write(bytes, sizeof(bytes));
    }

    static void write_u32(std::ofstream& output, uint32_t value) {
        const char bytes[4] = {
            static_cast<char>(value & 0xffu),
            static_cast<char>((value >> 8u) & 0xffu),
            static_cast<char>((value >> 16u) & 0xffu),
            static_cast<char>((value >> 24u) & 0xffu),
        };
        output.write(bytes, sizeof(bytes));
    }

    std::filesystem::path retain_hotkey_render_capture(
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
        bool command_copied = !directory_error
            && std::filesystem::copy_file(
                current_live_command_snapshot_path_,
                command_copy,
                std::filesystem::copy_options::overwrite_existing,
                command_error);
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
        const bool resource_copied = !directory_error
            && std::filesystem::copy_file(
                current_live_resource_snapshot_path_,
                resource_copy,
                std::filesystem::copy_options::overwrite_existing,
                resource_error);
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

    std::filesystem::path next_metrics_report_path() {
        SYSTEMTIME now{};
        GetLocalTime(&now);
        ++metrics_report_count_;
        std::wostringstream name;
        name << L"b2-recomp-metrics-"
             << std::setfill(L'0')
             << std::setw(4) << now.wYear
             << std::setw(2) << now.wMonth
             << std::setw(2) << now.wDay << L'-'
             << std::setw(2) << now.wHour
             << std::setw(2) << now.wMinute
             << std::setw(2) << now.wSecond << L'-'
             << std::setw(3) << now.wMilliseconds
             << L"-frame-" << frame_count_
             << L"-" << metrics_report_count_ << L".txt";
        return options_.metrics_report_directory / name.str();
    }

    void write_metrics_report() {
        const std::filesystem::path output_path = next_metrics_report_path();
        std::filesystem::create_directories(output_path.parent_path());
        std::ofstream output(output_path, std::ios::binary | std::ios::trunc);
        if (!output) {
            throw std::runtime_error(
                "unable to open metrics report: " + output_path.string());
        }
        const double bytes_per_mb = 1024.0 * 1024.0;
        output << "B2 Recomp metrics snapshot\n"
               << "frame: " << frame_count_ << '\n'
               << "scope: counters are cumulative since presenter startup; "
                  "FPS and timing values are from the latest completed frame\n"
               << std::fixed << std::setprecision(3);
        output << "FPS: ";
        if (last_frame_elapsed_us_ > 0) {
            output << 1'000'000.0 / static_cast<double>(last_frame_elapsed_us_);
        } else {
            output << "n/a";
        }
        output << '\n'
               << "guest instructions: " << current_manifest_guest_steps_ << '\n'
               << "compiled blocks / invalidations: "
               << current_manifest_guest_compiled_blocks_ << " / "
               << current_manifest_guest_invalidations_ << '\n'
               << "push-buffer commands: " << interpreted_method_count_ << '\n'
               << "draws: " << draw_count_ << '\n'
               << "triangles: " << triangle_count_ << '\n'
               << "pipeline creations: " << pipeline_creation_count_ << '\n'
               << "pipeline cache misses: " << pipeline_cache_miss_count_ << '\n'
               << "descriptor allocations: " << descriptor_allocation_count_ << '\n'
               << "command buffers: " << command_buffer_allocation_count_ << '\n'
               << "queue submissions: " << queue_submission_count_ << '\n'
               << "barriers: " << barrier_count_ << '\n'
               << "uploads MB: "
               << static_cast<double>(upload_bytes_) / bytes_per_mb << '\n'
               << "readbacks MB: "
               << static_cast<double>(readback_bytes_) / bytes_per_mb << '\n'
               << "CPU render ms: "
               << static_cast<double>(last_cpu_render_us_) / 1000.0 << '\n'
               << "window message pump ms: "
               << static_cast<double>(last_window_message_pump_us_) / 1000.0
               << '\n'
               << "controller poll ms: "
               << static_cast<double>(last_controller_poll_us_) / 1000.0
               << '\n'
               << "keyboard latch ms: "
               << static_cast<double>(last_keyboard_latch_us_) / 1000.0
               << '\n'
               << "reload probe ms: "
               << static_cast<double>(last_reload_probe_us_) / 1000.0 << '\n'
               << "pre-render unattributed ms: "
               << static_cast<double>(last_pre_render_unattributed_us_)
                    / 1000.0
               << '\n'
               << "GPU frame ms: ";
        if (gpu_frame_time_valid_) {
            output << last_gpu_frame_ms_;
        } else {
            output << "n/a";
        }
        output << '\n'
               << "fence-wait ms: "
               << static_cast<double>(last_fence_wait_us_) / 1000.0 << '\n'
               << "draw fence-wait ms: "
               << static_cast<double>(last_draw_fence_wait_us_) / 1000.0 << '\n'
               << "image acquire ms: "
               << static_cast<double>(last_acquire_us_) / 1000.0 << '\n'
               << "queue submit ms: "
               << static_cast<double>(last_submit_us_) / 1000.0 << '\n'
               << "readback wait ms: "
               << static_cast<double>(last_readback_wait_us_) / 1000.0 << '\n'
               << "queue present ms: "
               << static_cast<double>(last_present_us_) / 1000.0 << '\n'
               << "draw unattributed ms: "
               << static_cast<double>(last_draw_unattributed_us_) / 1000.0
               << '\n';
        if (!output) {
            throw std::runtime_error(
                "failed while writing metrics report: " + output_path.string());
        }
        log_.emit(
            "metrics_report_written",
            {
                {"output", json_string(output_path.string())},
                {"frame", std::to_string(frame_count_)},
                {"guest_steps", std::to_string(current_manifest_guest_steps_)},
            });
    }

    std::filesystem::path next_hotkey_screenshot_path() {
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

    FrameReadback read_frame() {
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

    void write_screenshot(
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

    void capture_screenshot(
        const std::filesystem::path& screenshot_path,
        const char* trigger) {
        write_screenshot(screenshot_path, read_frame(), trigger);
    }

    void on_key(UINT message, WPARAM key) {
        ++input_events_;
        log_.emit(
            "input_event",
            {
                {"message", json_string(message == WM_KEYDOWN ? "keydown" : "keyup")},
                {"virtual_key", std::to_string(static_cast<uint32_t>(key))},
                {"frame", std::to_string(frame_count_ + 1u)},
            });
        if (message == WM_KEYDOWN && key == VK_ESCAPE) {
            closed_by_user_ = true;
            running_ = false;
            PostQuitMessage(0);
        }
        if (key == VK_F9) {
            if (message == WM_KEYDOWN && !fps_counter_key_down_) {
                toggle_fps_counter();
            }
            fps_counter_key_down_ = message == WM_KEYDOWN;
            return;
        }
        if (key == VK_F11) {
            if (message == WM_KEYDOWN
                && !metrics_report_key_down_
                && !options_.metrics_report_directory.empty()) {
                write_metrics_report();
            }
            metrics_report_key_down_ = message == WM_KEYDOWN;
            return;
        }
        if (key == VK_F12) {
            if (message == WM_KEYDOWN
                && !hotkey_screenshot_key_down_
                && !options_.hotkey_screenshot_directory.empty()) {
                hotkey_screenshot_pending_ = true;
                pending_hotkey_screenshot_path_ =
                    next_hotkey_screenshot_path();
                pending_hotkey_render_capture_manifest_ =
                    retain_hotkey_render_capture(
                        pending_hotkey_screenshot_path_);
                log_.emit(
                    "hotkey_screenshot_queued",
                    {
                        {"frame", std::to_string(frame_count_ + 1u)},
                        {"directory", json_string(options_.hotkey_screenshot_directory.string())},
                        {"output", json_string(
                            pending_hotkey_screenshot_path_.string())},
                        {"render_capture_manifest",
                            pending_hotkey_render_capture_manifest_.empty()
                            ? "null"
                            : json_string(
                                pending_hotkey_render_capture_manifest_.string())},
                    });
            }
            hotkey_screenshot_key_down_ = message == WM_KEYDOWN;
            return;
        }
        const uint16_t mask = controller_button_for_key(key);
        if (mask != 0u) {
            const size_t key_index = static_cast<size_t>(key);
            if (key_index >= controller_key_down_.size()) {
                return;
            }
            const uint16_t previous_buttons = controller_buttons_;
            controller_key_down_[key_index] = message == WM_KEYDOWN;
            controller_buttons_ = controller_buttons_from_keys();
            latch_keyboard_button_presses(
                previous_buttons, controller_buttons_);
            write_controller_state();
        }
    }

    static uint16_t controller_button_for_key(WPARAM key) {
        switch (key) {
            case VK_UP: return 0x0001u;
            case VK_DOWN: return 0x0002u;
            case VK_LEFT: return 0x0004u;
            case VK_RIGHT: return 0x0008u;
            case 'S': return 0x0010u;
            case VK_BACK: return 0x0020u;
            case VK_SPACE: return 0x1000u;
            case VK_RETURN: return 0x1000u;
            case 'B': return 0x2000u;
            case 'X': return 0x4000u;
            case 'Y': return 0x8000u;
            default: return 0u;
        }
    }

    uint16_t controller_buttons_from_keys() const {
        uint16_t buttons = 0u;
        for (size_t key_index = 0; key_index < controller_key_down_.size();
             ++key_index) {
            if (controller_key_down_[key_index]) {
                buttons |= controller_button_for_key(
                    static_cast<WPARAM>(key_index));
            }
        }
        return buttons;
    }

    void latch_keyboard_button_presses(
        uint16_t previous_buttons,
        uint16_t current_buttons) {
        const uint16_t pressed_buttons = static_cast<uint16_t>(
            current_buttons & ~previous_buttons);
        if (pressed_buttons == 0u) {
            return;
        }
        const auto now = std::chrono::steady_clock::now();
        for (size_t bit_index = 0; bit_index < 16u; ++bit_index) {
            const uint16_t bit = static_cast<uint16_t>(1u << bit_index);
            if ((pressed_buttons & bit) == 0u) {
                continue;
            }
            keyboard_latched_buttons_ |= bit;
            keyboard_latch_deadlines_[bit_index] = now
                + std::chrono::milliseconds(kControllerMinimumPulseMs);
            keyboard_latch_max_deadlines_[bit_index] = now
                + std::chrono::milliseconds(kControllerMaximumPulseMs);
            keyboard_latch_guest_flip_counts_[bit_index] =
                current_manifest_guest_flip_count_;
        }
    }

    void initialize_controller_backend() {
        if (options_.controller_state_json.empty()) {
            return;
        }
        SDL_SetMainReady();
        if (!SDL_InitSubSystem(SDL_INIT_GAMEPAD)) {
            log_.emit(
                "controller_backend_init_failed",
                {
                    {"backend", json_string("sdl3")},
                    {"error", json_string(SDL_GetError())},
                });
            return;
        }
        controller_backend_initialized_ = true;
        SDL_SetGamepadEventsEnabled(false);

        int custom_mapping_count = 0;
        std::filesystem::path custom_mapping_path;
        std::array<wchar_t, 32768> executable_path{};
        const DWORD executable_length = GetModuleFileNameW(
            nullptr,
            executable_path.data(),
            static_cast<DWORD>(executable_path.size()));
        if (executable_length > 0u
            && executable_length < executable_path.size()) {
            custom_mapping_path = std::filesystem::path(
                executable_path.data()).parent_path() / L"gamecontrollerdb.txt";
            if (std::filesystem::is_regular_file(custom_mapping_path)) {
                custom_mapping_count = SDL_AddGamepadMappingsFromFile(
                    narrow(custom_mapping_path.wstring()).c_str());
                if (custom_mapping_count < 0) {
                    log_.emit(
                        "controller_mapping_load_failed",
                        {
                            {"backend", json_string("sdl3")},
                            {"path", json_string(custom_mapping_path.string())},
                            {"error", json_string(SDL_GetError())},
                        });
                    custom_mapping_count = 0;
                }
            }
        }

        const int version = SDL_GetVersion();
        std::ostringstream version_text;
        version_text << SDL_VERSIONNUM_MAJOR(version) << '.'
                     << SDL_VERSIONNUM_MINOR(version) << '.'
                     << SDL_VERSIONNUM_MICRO(version);
        const char* revision = SDL_GetRevision();
        log_.emit(
            "controller_backend_initialized",
            {
                {"backend", json_string("sdl3")},
                {"version", json_string(version_text.str())},
                {"revision", json_string(revision ? revision : "")},
                {"custom_mapping_count", std::to_string(custom_mapping_count)},
                {"custom_mapping_path", custom_mapping_path.empty()
                    ? "null"
                    : json_string(custom_mapping_path.string())},
            });
    }

    bool discover_host_controller() {
        int gamepad_count = 0;
        SDL_ClearError();
        SDL_JoystickID* gamepad_ids = SDL_GetGamepads(&gamepad_count);
        if (gamepad_ids == nullptr && SDL_GetError()[0] != '\0') {
            log_.emit(
                "controller_discovery_failed",
                {
                    {"backend", json_string("sdl3")},
                    {"error", json_string(SDL_GetError())},
                });
        }
        for (int index = 0; index < gamepad_count; ++index) {
            SDL_Gamepad* candidate = SDL_OpenGamepad(gamepad_ids[index]);
            if (candidate == nullptr) {
                continue;
            }
            active_gamepad_ = candidate;
            active_gamepad_id_ = gamepad_ids[index];
            const char* name = SDL_GetGamepadName(candidate);
            active_gamepad_name_ = name ? name : "Unknown gamepad";
            const SDL_GamepadType gamepad_type = SDL_GetGamepadType(candidate);
            const char* type_name = SDL_GetGamepadStringForType(gamepad_type);
            active_gamepad_type_ = type_name ? type_name : "unknown";
            active_gamepad_vendor_ = SDL_GetGamepadVendor(candidate);
            active_gamepad_product_ = SDL_GetGamepadProduct(candidate);
            active_gamepad_product_version_ =
                SDL_GetGamepadProductVersion(candidate);
            std::array<char, 33> guid{};
            SDL_GUIDToString(
                SDL_GetGamepadGUIDForID(active_gamepad_id_),
                guid.data(),
                static_cast<int>(guid.size()));
            active_gamepad_guid_ = guid.data();
            char* mapping = SDL_GetGamepadMapping(candidate);
            active_gamepad_mapping_ = mapping ? mapping : "";
            SDL_free(mapping);
            log_.emit(
                "host_controller_connected",
                {
                    {"backend", json_string("sdl3")},
                    {"gamepad_id", std::to_string(active_gamepad_id_)},
                    {"name", json_string(active_gamepad_name_)},
                    {"type", json_string(active_gamepad_type_)},
                    {"guid", json_string(active_gamepad_guid_)},
                    {"vendor_id", std::to_string(active_gamepad_vendor_)},
                    {"product_id", std::to_string(active_gamepad_product_)},
                    {"product_version", std::to_string(
                        active_gamepad_product_version_)},
                    {"mapping", json_string(active_gamepad_mapping_)},
                });
            SDL_free(gamepad_ids);
            return true;
        }
        SDL_free(gamepad_ids);
        next_controller_discovery_ = std::chrono::steady_clock::now()
            + kControllerDiscoveryInterval;
        return false;
    }

    void disconnect_host_controller() {
        log_.emit(
            "host_controller_disconnected",
            {
                {"backend", json_string("sdl3")},
                {"gamepad_id", std::to_string(active_gamepad_id_)},
                {"name", json_string(active_gamepad_name_)},
                {"guid", json_string(active_gamepad_guid_)},
            });
        if (active_gamepad_ != nullptr) {
            SDL_CloseGamepad(active_gamepad_);
            active_gamepad_ = nullptr;
        }
        active_gamepad_id_ = 0u;
        active_gamepad_name_.clear();
        active_gamepad_type_.clear();
        active_gamepad_guid_.clear();
        active_gamepad_mapping_.clear();
        active_gamepad_vendor_ = 0u;
        active_gamepad_product_ = 0u;
        active_gamepad_product_version_ = 0u;
        next_controller_discovery_ = std::chrono::steady_clock::now();
    }

    void poll_host_controller(bool publish = true) {
        if (!controller_backend_initialized_) {
            return;
        }
        SDL_UpdateGamepads();
        if (active_gamepad_ != nullptr
            && !SDL_GamepadConnected(active_gamepad_)) {
            disconnect_host_controller();
        }
        const auto now = std::chrono::steady_clock::now();
        if (active_gamepad_ == nullptr && now >= next_controller_discovery_) {
            discover_host_controller();
        }
        const HostControllerState next_state =
            map_sdl_gamepad_state(active_gamepad_);
        if (controller_states_equivalent(host_controller_state_, next_state)) {
            return;
        }
        host_controller_state_ = next_state;
        if (publish) {
            write_controller_state();
        }
    }

    uint16_t effective_controller_buttons() const {
        return controller_buttons_ | host_controller_state_.buttons
            | keyboard_latched_buttons_;
    }

    void update_keyboard_button_latches() {
        if (keyboard_latched_buttons_ == 0u) {
            return;
        }
        const uint16_t before = effective_controller_buttons();
        const auto now = std::chrono::steady_clock::now();
        for (size_t bit_index = 0; bit_index < 16u; ++bit_index) {
            const uint16_t bit = static_cast<uint16_t>(1u << bit_index);
            if ((keyboard_latched_buttons_ & bit) == 0u
                || now < keyboard_latch_deadlines_[bit_index]) {
                continue;
            }
            const uint64_t latch_guest_flip_count =
                keyboard_latch_guest_flip_counts_[bit_index];
            const uint64_t guest_flip_pulse =
                current_manifest_guest_flip_count_ >= latch_guest_flip_count
                ? current_manifest_guest_flip_count_ - latch_guest_flip_count
                : 0u;
            if (guest_flip_pulse >= kControllerMinimumPulseGuestFlips
                || now >= keyboard_latch_max_deadlines_[bit_index]) {
                keyboard_latched_buttons_ &= static_cast<uint16_t>(~bit);
            }
        }
        if (effective_controller_buttons() != before) {
            write_controller_state();
        }
    }

    void write_controller_state() {
        if (options_.controller_state_json.empty()) {
            return;
        }
        const auto publish_begin = std::chrono::steady_clock::now();
        if (options_.controller_state_json.has_parent_path()) {
            std::filesystem::create_directories(options_.controller_state_json.parent_path());
        }
        std::filesystem::path temporary = options_.controller_state_json;
        temporary += L".tmp";
        {
            std::ofstream output(temporary, std::ios::trunc);
            if (!output) {
                throw std::runtime_error("cannot write controller state JSON: " + temporary.string());
            }
            output << "{\"ports\":{\"0\":{\"connected\":true,\"buttons\":"
                   << effective_controller_buttons()
                   << ",\"left_trigger\":"
                   << static_cast<uint32_t>(host_controller_state_.left_trigger)
                   << ",\"right_trigger\":"
                   << static_cast<uint32_t>(host_controller_state_.right_trigger)
                   << ",\"thumb_lx\":" << host_controller_state_.thumb_lx
                   << ",\"thumb_ly\":" << host_controller_state_.thumb_ly
                   << ",\"thumb_rx\":" << host_controller_state_.thumb_rx
                   << ",\"thumb_ry\":" << host_controller_state_.thumb_ry
                   << "}},\"host_controller\":{\"connected\":"
                   << json_bool(host_controller_state_.connected)
                   << ",\"backend\":\"sdl3\",\"name\":"
                   << json_string(active_gamepad_name_)
                   << ",\"type\":" << json_string(active_gamepad_type_)
                   << ",\"guid\":" << json_string(active_gamepad_guid_)
                   << ",\"vendor_id\":" << active_gamepad_vendor_
                   << ",\"product_id\":" << active_gamepad_product_
                   << "}}\n";
        }
        const auto deadline = std::chrono::steady_clock::now()
            + std::chrono::seconds(10);
        uint32_t replace_retry_count = 0u;
        while (!MoveFileExW(
            temporary.c_str(), options_.controller_state_json.c_str(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
            if (std::chrono::steady_clock::now() >= deadline) {
                throw std::runtime_error("cannot publish controller state JSON");
            }
            ++replace_retry_count;
            std::this_thread::sleep_for(std::chrono::milliseconds(2));
        }
        const int64_t publish_us = std::chrono::duration_cast<
            std::chrono::microseconds>(
                std::chrono::steady_clock::now() - publish_begin).count();
        ++controller_publish_count_;
        log_.emit(
            "controller_state_published",
            {
                {"publish", std::to_string(controller_publish_count_)},
                {"buttons", std::to_string(effective_controller_buttons())},
                {"physical_buttons", std::to_string(
                    controller_buttons_ | host_controller_state_.buttons)},
                {"keyboard_buttons", std::to_string(controller_buttons_)},
                {"gamepad_buttons", std::to_string(host_controller_state_.buttons)},
                {"latched_buttons", std::to_string(keyboard_latched_buttons_)},
                {"controller_backend", json_string("sdl3")},
                {"host_controller_connected", json_bool(
                    host_controller_state_.connected)},
                {"left_trigger", std::to_string(
                    host_controller_state_.left_trigger)},
                {"right_trigger", std::to_string(
                    host_controller_state_.right_trigger)},
                {"thumb_lx", std::to_string(host_controller_state_.thumb_lx)},
                {"thumb_ly", std::to_string(host_controller_state_.thumb_ly)},
                {"thumb_rx", std::to_string(host_controller_state_.thumb_rx)},
                {"thumb_ry", std::to_string(host_controller_state_.thumb_ry)},
                {"publish_us", std::to_string(publish_us)},
                {"replace_retry_count", std::to_string(replace_retry_count)},
                {
                    "manifest_guest_flip_count",
                    std::to_string(current_manifest_guest_flip_count_),
                },
            });
    }

    static LRESULT CALLBACK window_proc(HWND hwnd, UINT message, WPARAM wparam, LPARAM lparam) {
        auto* app = reinterpret_cast<VulkanFirstFrameApp*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
        if (message == WM_NCCREATE) {
            auto* create = reinterpret_cast<CREATESTRUCTW*>(lparam);
            app = reinterpret_cast<VulkanFirstFrameApp*>(create->lpCreateParams);
            SetWindowLongPtrW(hwnd, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(app));
        }
        if (app) {
            switch (message) {
                case WM_CLOSE:
                    app->closed_by_user_ = true;
                    app->running_ = false;
                    DestroyWindow(hwnd);
                    return 0;
                case WM_DESTROY:
                    if (app->hwnd_ == hwnd) {
                        app->hwnd_ = nullptr;
                    }
                    PostQuitMessage(0);
                    return 0;
                case WM_KEYDOWN:
                case WM_KEYUP:
                    app->on_key(message, wparam);
                    return 0;
                default:
                    break;
            }
        }
        return DefWindowProcW(hwnd, message, wparam, lparam);
    }

    void cleanup() {
        if (controller_backend_initialized_) {
            if (active_gamepad_ != nullptr) {
                disconnect_host_controller();
            }
            SDL_QuitSubSystem(SDL_INIT_GAMEPAD);
            controller_backend_initialized_ = false;
        }
        if (publication_event_) {
            CloseHandle(publication_event_);
            publication_event_ = nullptr;
        }
        if (presentation_ack_event_) {
            CloseHandle(presentation_ack_event_);
            presentation_ack_event_ = nullptr;
        }
        if (presentation_ack_file_ != INVALID_HANDLE_VALUE) {
            CloseHandle(presentation_ack_file_);
            presentation_ack_file_ = INVALID_HANDLE_VALUE;
        }
        if (live_manifest_file_ != INVALID_HANDLE_VALUE) {
            CloseHandle(live_manifest_file_);
            live_manifest_file_ = INVALID_HANDLE_VALUE;
        }
        if (frame_pacing_timer_) {
            CloseHandle(frame_pacing_timer_);
            frame_pacing_timer_ = nullptr;
        }
        if (device_) {
            persist_pipeline_cache();
            if (in_flight_) {
                vkDestroyFence(device_, in_flight_, nullptr);
            }
            if (render_finished_) {
                vkDestroySemaphore(device_, render_finished_, nullptr);
            }
            if (image_available_) {
                vkDestroySemaphore(device_, image_available_, nullptr);
            }
            if (command_pool_) {
                command_buffers_.clear();
                vkDestroyCommandPool(device_, command_pool_, nullptr);
            }
            if (gpu_timing_query_pool_) {
                vkDestroyQueryPool(device_, gpu_timing_query_pool_, nullptr);
                gpu_timing_query_pool_ = VK_NULL_HANDLE;
            }
            destroy_host_texture_bindings();
            if (vertex_mapped_) {
                vkUnmapMemory(device_, vertex_memory_);
                vertex_mapped_ = nullptr;
            }
            if (vertex_buffer_) {
                vkDestroyBuffer(device_, vertex_buffer_, nullptr);
            }
            if (vertex_memory_) {
                vkFreeMemory(device_, vertex_memory_, nullptr);
            }
            destroy_offscreen_render_targets();
            if (fragment_state_mapped_) {
                vkUnmapMemory(device_, fragment_state_memory_);
                fragment_state_mapped_ = nullptr;
            }
            if (fragment_state_buffer_) {
                vkDestroyBuffer(device_, fragment_state_buffer_, nullptr);
            }
            if (fragment_state_memory_) {
                vkFreeMemory(device_, fragment_state_memory_, nullptr);
            }
            if (vertex_program_state_mapped_) {
                vkUnmapMemory(device_, vertex_program_state_memory_);
                vertex_program_state_mapped_ = nullptr;
            }
            if (vertex_program_state_buffer_) {
                vkDestroyBuffer(
                    device_, vertex_program_state_buffer_, nullptr);
            }
            if (vertex_program_state_memory_) {
                vkFreeMemory(
                    device_, vertex_program_state_memory_, nullptr);
            }
            if (raw_vertex_resource_mapped_) {
                vkUnmapMemory(device_, raw_vertex_resource_memory_);
                raw_vertex_resource_mapped_ = nullptr;
            }
            if (raw_vertex_resource_buffer_) {
                vkDestroyBuffer(
                    device_, raw_vertex_resource_buffer_, nullptr);
            }
            if (raw_vertex_resource_memory_) {
                vkFreeMemory(
                    device_, raw_vertex_resource_memory_, nullptr);
            }
            for (const HostTexture& texture : host_textures_) {
                if (texture.view) vkDestroyImageView(device_, texture.view, nullptr);
                if (texture.image) vkDestroyImage(device_, texture.image, nullptr);
                if (texture.memory) vkFreeMemory(device_, texture.memory, nullptr);
            }
            for (HostTexture& texture :
                 render_target_feedback_image_cache_) {
                destroy_host_texture(texture);
            }
            render_target_feedback_image_cache_.clear();
            for (const HostPipeline& host_pipeline : graphics_pipelines_) {
                if (host_pipeline.pipeline) vkDestroyPipeline(device_, host_pipeline.pipeline, nullptr);
            }
            graphics_pipelines_.clear();
            if (texture_convert_pipeline_) {
                vkDestroyPipeline(
                    device_, texture_convert_pipeline_, nullptr);
            }
            if (texture_convert_pipeline_layout_) {
                vkDestroyPipelineLayout(
                    device_, texture_convert_pipeline_layout_, nullptr);
            }
            if (texture_convert_descriptor_layout_) {
                vkDestroyDescriptorSetLayout(
                    device_, texture_convert_descriptor_layout_, nullptr);
            }
            if (pipeline_layout_) {
                vkDestroyPipelineLayout(device_, pipeline_layout_, nullptr);
            }
            if (texture_descriptor_layout_) {
                vkDestroyDescriptorSetLayout(device_, texture_descriptor_layout_, nullptr);
            }
            if (readback_buffer_) {
                vkDestroyBuffer(device_, readback_buffer_, nullptr);
            }
            if (readback_memory_) {
                vkFreeMemory(device_, readback_memory_, nullptr);
            }
            for (VkFramebuffer framebuffer : framebuffers_) {
                vkDestroyFramebuffer(device_, framebuffer, nullptr);
            }
            if (depth_image_view_) {
                vkDestroyImageView(device_, depth_image_view_, nullptr);
            }
            if (depth_image_) {
                vkDestroyImage(device_, depth_image_, nullptr);
            }
            if (depth_memory_) {
                vkFreeMemory(device_, depth_memory_, nullptr);
            }
            if (render_pass_) {
                vkDestroyRenderPass(device_, render_pass_, nullptr);
            }
            for (VkImageView image_view : swapchain_image_views_) {
                vkDestroyImageView(device_, image_view, nullptr);
            }
            if (swapchain_) {
                vkDestroySwapchainKHR(device_, swapchain_, nullptr);
            }
            if (pipeline_cache_) {
                vkDestroyPipelineCache(device_, pipeline_cache_, nullptr);
                pipeline_cache_ = VK_NULL_HANDLE;
            }
            vkDestroyDevice(device_, nullptr);
            device_ = VK_NULL_HANDLE;
        }
        if (surface_) {
            vkDestroySurfaceKHR(instance_, surface_, nullptr);
            surface_ = VK_NULL_HANDLE;
        }
        if (hwnd_) {
            DestroyWindow(hwnd_);
            hwnd_ = nullptr;
        }
        if (instance_) {
            vkDestroyInstance(instance_, nullptr);
            instance_ = VK_NULL_HANDLE;
        }
    }

    Options options_;
    DebugLog log_;
    HINSTANCE hinstance_ = nullptr;
    HWND hwnd_ = nullptr;
    bool running_ = true;
    bool closed_by_user_ = false;
    bool input_injected_ = false;
    uint32_t frame_count_ = 0;
    uint32_t input_events_ = 0;
    std::array<bool, 256> controller_key_down_{};
    uint16_t controller_buttons_ = 0;
    HostControllerState host_controller_state_{};
    bool controller_backend_initialized_ = false;
    SDL_Gamepad* active_gamepad_ = nullptr;
    SDL_JoystickID active_gamepad_id_ = 0u;
    std::string active_gamepad_name_;
    std::string active_gamepad_type_;
    std::string active_gamepad_guid_;
    std::string active_gamepad_mapping_;
    uint16_t active_gamepad_vendor_ = 0u;
    uint16_t active_gamepad_product_ = 0u;
    uint16_t active_gamepad_product_version_ = 0u;
    std::chrono::steady_clock::time_point next_controller_discovery_{};
    uint16_t keyboard_latched_buttons_ = 0;
    std::array<uint64_t, 16> keyboard_latch_guest_flip_counts_{};
    std::array<std::chrono::steady_clock::time_point, 16>
        keyboard_latch_deadlines_{};
    std::array<std::chrono::steady_clock::time_point, 16>
        keyboard_latch_max_deadlines_{};
    HANDLE frame_pacing_timer_ = nullptr;
    uint32_t controller_publish_count_ = 0;
    uint32_t live_render_reload_count_ = 0;
    uint32_t live_render_skipped_reload_count_ = 0;
    int64_t last_command_load_us_ = 0;
    int64_t last_manifest_read_us_ = 0;
    int64_t last_manifest_parse_us_ = 0;
    int64_t last_source_load_us_ = 0;
    int64_t last_command_file_open_us_ = 0;
    int64_t last_command_file_read_us_ = 0;
    int64_t last_command_record_validation_us_ = 0;
    bool last_command_file_reused_ = false;
    int64_t last_interpret_us_ = 0;
    int64_t last_method_interpret_us_ = 0;
    int64_t last_indexed_materialize_us_ = 0;
    int64_t last_render_validation_us_ = 0;
    uint64_t last_resource_snapshot_reused_count_ = 0u;
    uint64_t last_resource_snapshot_reused_bytes_ = 0u;
    bool last_presented_diagnostics_sampled_ = false;
    bool last_offscreen_render_targets_reused_ = false;
    size_t last_interpreted_command_delta_ = 0;
    size_t last_native_command_read_count_ = 0;
    size_t last_native_command_span_count_ = 0;
    size_t last_command_read_bytes_ = 0;
    uint64_t native_command_read_count_ = 0;
    uint64_t native_command_span_read_count_ = 0;
    uint32_t last_live_command_mmio_count_ = 0;
    bool last_resource_generation_changed_ = false;
    bool last_command_cursor_reset_ = false;
    uint64_t render_work_generation_ = 0;
    mutable uint64_t feedback_spec_cache_generation_ =
        std::numeric_limits<uint64_t>::max();
    mutable std::vector<RenderTargetFeedbackSpec> feedback_spec_cache_;
    mutable uint64_t feedback_spec_cache_hit_count_ = 0;
    mutable uint64_t feedback_spec_cache_build_count_ = 0;
    mutable int64_t last_feedback_spec_build_us_ = 0;
    uint64_t counted_command_render_work_generation_ =
        std::numeric_limits<uint64_t>::max();
    uint64_t presented_diagnostics_generation_ = 0;
    bool presented_diagnostics_valid_ = false;
    uint64_t current_manifest_guest_flip_count_ = 0;
    uint64_t current_manifest_guest_steps_ = 0;
    uint64_t current_manifest_guest_compiled_blocks_ = 0;
    uint64_t current_manifest_guest_invalidations_ = 0;
    uint64_t current_manifest_presentable_command_count_ = 0;
    uint64_t current_manifest_command_base_count_ = 0;
    bool current_manifest_bulk_span_commands_ = false;
    uint64_t current_manifest_presentable_command_byte_count_ = 0;
    uint64_t current_manifest_command_base_byte_count_ = 0;
    std::filesystem::file_time_type live_render_write_time_{};
    std::filesystem::file_time_type live_resource_write_time_{};
    std::string live_resource_generation_;
    std::filesystem::path live_resource_source_;
    std::string live_command_generation_;
    std::string current_live_manifest_text_;
    std::ifstream live_command_file_;
    std::filesystem::path live_command_file_path_;
    std::vector<uint8_t> live_command_delta_bytes_;
    std::filesystem::path current_live_command_snapshot_path_;
    std::filesystem::path current_live_resource_snapshot_path_;
    HANDLE live_manifest_file_ = INVALID_HANDLE_VALUE;
    std::filesystem::path presentation_ack_path_;
    HANDLE presentation_ack_file_ = INVALID_HANDLE_VALUE;
    std::string presentation_event_name_;
    HANDLE presentation_ack_event_ = nullptr;
    std::string publication_event_name_;
    HANDLE publication_event_ = nullptr;
    bool publication_retry_pending_ = false;
    uint64_t acknowledged_presentation_flip_ = 0;
    uint32_t recovered_d3d_command_count_ = 0;
    size_t counted_recovered_command_count_ = 0;
    size_t interpreted_source_command_count_ = 0;
    uint64_t interpreted_source_command_byte_count_ = 0;
    bool continuation_analysis_bootstrap_ = false;
    uint32_t recovered_d3d_mmio_count_ = 0;
    uint32_t recovered_d3d_push_buffer_count_ = 0;
    uint32_t interpreted_push_buffer_word_count_ = 0;
    uint32_t interpreted_method_packet_count_ = 0;
    uint32_t interpreted_method_count_ = 0;
    uint32_t interpreted_zero_count_method_word_count_ = 0;
    uint32_t translated_command_count_ = 0;
    uint32_t frontend_text_rectangle_count_ = 0;
    uint32_t frontend_text_first_vertex_ = 0;
    uint32_t frontend_text_vertex_count_ = 0;
    uint32_t frontend_text_fragment_state_index_ =
        std::numeric_limits<uint32_t>::max();
    std::string recovered_frontend_text_;
    RecoveredD3DStreamSource recovered_source_;
    InterpretedD3DStream interpreted_stream_;
    GpuRawVertexResourceCache gpu_raw_vertex_resource_cache_;
    bool presented_half_quad_recovered_ = false;
    bool presented_overscan_height_recovered_ = false;
    uint32_t presented_vertex_program_transformed_count_ = 0;
    uint32_t presented_linear_texture_normalized_vertex_count_ = 0;
    PresentedVertexTransformDiagnostics presented_vertex_transform_diagnostics_{};
    std::vector<PresentedDrawTransformDiagnostics>
        presented_draw_transform_diagnostics_;
    bool screenshot_captured_ = false;
    bool hotkey_screenshot_pending_ = false;
    bool hotkey_screenshot_key_down_ = false;
    uint32_t hotkey_screenshot_count_ = 0;
    bool metrics_report_key_down_ = false;
    uint32_t metrics_report_count_ = 0;
    bool fps_counter_enabled_ = false;
    bool fps_counter_key_down_ = false;
    uint64_t fps_counter_last_guest_flip_count_ = 0u;
    std::chrono::steady_clock::time_point fps_counter_sample_start_{};
    std::filesystem::path pending_hotkey_screenshot_path_;
    std::filesystem::path pending_hotkey_render_capture_manifest_;
    uint32_t unsupported_texture_resource_count_ = 0;
    uint32_t current_audit_flip_ = 0;
    uint32_t current_audit_guest_steps_ = 0;
    uint32_t current_audit_flip_value_ = 0;
    uint32_t current_audit_command_count_ = 0;
    uint32_t acknowledged_audit_flip_ = 0;
    uint32_t last_audit_command_count_ = 0;
    uint32_t last_audit_command_delta_ = 0;
    uint32_t last_audit_flip_value_ = 0;
    bool current_work_has_readback_ = false;
    std::string current_audit_generation_;
    std::string current_audit_health_reason_;
    std::string last_audit_resource_generation_;

    VkInstance instance_ = VK_NULL_HANDLE;
    VkSurfaceKHR surface_ = VK_NULL_HANDLE;
    VkPhysicalDevice physical_device_ = VK_NULL_HANDLE;
    QueueFamilySelection queue_family_{};
    VkDevice device_ = VK_NULL_HANDLE;
    VkQueue graphics_queue_ = VK_NULL_HANDLE;
    VkPipelineCache pipeline_cache_ = VK_NULL_HANDLE;
    size_t pipeline_cache_loaded_bytes_ = 0u;
    size_t pipeline_cache_saved_bytes_ = 0u;
    bool pipeline_cache_rejected_ = false;
    VkSwapchainKHR swapchain_ = VK_NULL_HANDLE;
    VkFormat swapchain_format_ = VK_FORMAT_UNDEFINED;
    VkExtent2D swapchain_extent_{};
    std::vector<VkImage> swapchain_images_;
    std::vector<VkImageView> swapchain_image_views_;
    VkFormat depth_format_ = VK_FORMAT_UNDEFINED;
    VkImage depth_image_ = VK_NULL_HANDLE;
    VkDeviceMemory depth_memory_ = VK_NULL_HANDLE;
    VkImageView depth_image_view_ = VK_NULL_HANDLE;
    VkRenderPass render_pass_ = VK_NULL_HANDLE;
    VkDescriptorSetLayout texture_descriptor_layout_ = VK_NULL_HANDLE;
    VkDescriptorPool texture_descriptor_pool_ = VK_NULL_HANDLE;
    VkPipelineLayout pipeline_layout_ = VK_NULL_HANDLE;
    std::vector<HostPipeline> graphics_pipelines_;
    VkDescriptorSetLayout texture_convert_descriptor_layout_ = VK_NULL_HANDLE;
    VkPipelineLayout texture_convert_pipeline_layout_ = VK_NULL_HANDLE;
    VkPipeline texture_convert_pipeline_ = VK_NULL_HANDLE;
    VkBuffer vertex_buffer_ = VK_NULL_HANDLE;
    VkDeviceMemory vertex_memory_ = VK_NULL_HANDLE;
    VkDeviceSize vertex_buffer_size_ = 0;
    void* vertex_mapped_ = nullptr;
    VkBuffer fragment_state_buffer_ = VK_NULL_HANDLE;
    VkDeviceMemory fragment_state_memory_ = VK_NULL_HANDLE;
    VkDeviceSize fragment_state_buffer_size_ = 0;
    void* fragment_state_mapped_ = nullptr;
    uint32_t fragment_state_count_ = 0;
    VkBuffer vertex_program_state_buffer_ = VK_NULL_HANDLE;
    VkDeviceMemory vertex_program_state_memory_ = VK_NULL_HANDLE;
    VkDeviceSize vertex_program_state_buffer_size_ = 0;
    void* vertex_program_state_mapped_ = nullptr;
    uint32_t vertex_program_state_count_ = 0;
    VkBuffer raw_vertex_resource_buffer_ = VK_NULL_HANDLE;
    VkDeviceMemory raw_vertex_resource_memory_ = VK_NULL_HANDLE;
    VkDeviceSize raw_vertex_resource_buffer_size_ = 0;
    void* raw_vertex_resource_mapped_ = nullptr;
    uint32_t gpu_vertex_program_draw_count_ = 0;
    uint32_t gpu_vertex_program_vertex_count_ = 0;
    uint32_t gpu_raw_attribute_draw_count_ = 0;
    uint32_t gpu_raw_attribute_vertex_count_ = 0;
    uint32_t cpu_vertex_program_fallback_draw_count_ = 0;
    uint32_t cpu_vertex_program_fallback_vertex_count_ = 0;
    uint64_t last_vertex_state_upload_us_ = 0;
    uint64_t last_raw_vertex_upload_us_ = 0;
    uint64_t raw_vertex_resource_upload_bytes_ = 0;
    uint64_t raw_vertex_index_upload_bytes_ = 0;
    uint64_t last_gpu_texture_conversion_us_ = 0;
    uint64_t last_texture_refresh_us_ = 0u;
    uint64_t last_texture_indexed_lookup_count_ = 0u;
    uint64_t last_texture_indexed_lookup_candidate_count_ = 0u;
    uint64_t last_texture_constant_lookup_count_ = 0u;
    uint64_t gpu_texture_conversion_batch_count_ = 0;
    uint64_t gpu_texture_conversion_texture_count_ = 0;
    uint64_t gpu_texture_conversion_mip_count_ = 0;
    uint64_t gpu_texture_conversion_input_bytes_ = 0;
    uint64_t gpu_texture_conversion_output_bytes_ = 0;
    uint64_t gpu_texture_conversion_rejected_batch_count_ = 0;
    GpuTextureValidationCoverage gpu_texture_validation_coverage_{};
    uint64_t last_vertex_transform_us_ = 0;
    uint64_t last_vertex_map_us_ = 0;
    uint64_t last_vertex_copy_us_ = 0;
    uint64_t last_vertex_resource_prepare_us_ = 0u;
    uint64_t last_state_resource_prepare_us_ = 0u;
    uint64_t last_texture_resource_prepare_us_ = 0u;
    uint64_t last_offscreen_resource_prepare_us_ = 0u;
    uint64_t last_resource_bookkeeping_us_ = 0u;
    uint32_t uploaded_vertex_base_ = 0;
    uint32_t uploaded_vertex_count_ = 0;
    uint32_t presented_surface_color_offset_ = 0;
    uint32_t presented_fixed_function_transformed_count_ = 0;
    uint32_t offscreen_render_target_draw_count_ = 0;
    uint32_t offscreen_render_target_transformed_vertex_count_ = 0;
    uint32_t last_render_target_feedback_pruned_count_ = 0;
    uint32_t last_render_target_feedback_image_cache_hit_count_ = 0;
    uint32_t last_render_target_feedback_image_cache_miss_count_ = 0;
    uint32_t last_render_target_feedback_image_cache_store_count_ = 0;
    uint32_t last_render_target_feedback_image_cache_eviction_count_ = 0;
    uint64_t render_target_feedback_image_cache_hit_count_ = 0;
    uint64_t render_target_feedback_image_cache_miss_count_ = 0;
    uint64_t render_target_feedback_image_cache_store_count_ = 0;
    uint64_t render_target_feedback_image_cache_eviction_count_ = 0;
    std::vector<HostTexture> host_textures_;
    std::vector<HostTexture> render_target_feedback_image_cache_;
    std::vector<HostTextureBinding> host_texture_bindings_;
    uint64_t texture_binding_set_reuse_count_ = 0u;
    uint64_t texture_binding_set_rebuild_count_ = 0u;
    uint64_t texture_binding_image_descriptor_update_count_ = 0u;
    uint64_t texture_binding_descriptor_set_allocation_count_ = 0u;
    uint64_t last_texture_binding_update_us_ = 0u;
    uint32_t last_texture_binding_image_descriptor_update_count_ = 0u;
    uint32_t last_texture_binding_descriptor_set_allocation_count_ = 0u;
    bool last_texture_binding_set_reused_ = false;
    std::vector<OffscreenRenderTarget> offscreen_render_targets_;
    std::vector<VkFramebuffer> framebuffers_;
    VkCommandPool command_pool_ = VK_NULL_HANDLE;
    VkQueryPool gpu_timing_query_pool_ = VK_NULL_HANDLE;
    float gpu_timestamp_period_ns_ = 0.0f;
    uint32_t gpu_timestamp_valid_bits_ = 0u;
    uint32_t last_submitted_image_index_ =
        std::numeric_limits<uint32_t>::max();
    bool gpu_frame_time_valid_ = false;
    double last_gpu_frame_ms_ = 0.0;
    VkBuffer readback_buffer_ = VK_NULL_HANDLE;
    VkDeviceMemory readback_memory_ = VK_NULL_HANDLE;
    VkDeviceSize readback_size_ = 0;
    std::vector<VkCommandBuffer> command_buffers_;
    std::vector<uint64_t> command_buffer_draw_counts_;
    std::vector<uint64_t> command_buffer_triangle_counts_;
    std::vector<uint64_t> command_buffer_barrier_counts_;
    mutable uint64_t recording_draw_count_ = 0u;
    mutable uint64_t recording_triangle_count_ = 0u;
    mutable uint64_t recording_barrier_count_ = 0u;
    uint64_t draw_count_ = 0u;
    uint64_t triangle_count_ = 0u;
    uint64_t pipeline_creation_count_ = 0u;
    uint64_t pipeline_cache_miss_count_ = 0u;
    uint64_t last_pipeline_state_discovery_us_ = 0u;
    uint32_t last_pipeline_candidate_draw_count_ = 0u;
    uint32_t last_pipeline_unique_state_count_ = 0u;
    uint32_t last_pipeline_missing_state_count_ = 0u;
    uint64_t descriptor_allocation_count_ = 0u;
    uint64_t command_buffer_allocation_count_ = 0u;
    uint64_t queue_submission_count_ = 0u;
    uint64_t barrier_count_ = 0u;
    uint64_t upload_bytes_ = 0u;
    uint64_t readback_bytes_ = 0u;
    uint64_t active_frame_fence_wait_us_ = 0u;
    uint64_t last_fence_wait_us_ = 0u;
    uint64_t last_window_message_pump_us_ = 0u;
    uint64_t last_controller_poll_us_ = 0u;
    uint64_t last_keyboard_latch_us_ = 0u;
    uint64_t last_reload_probe_us_ = 0u;
    uint64_t last_reload_dispatch_us_ = 0u;
    uint64_t last_pre_render_unattributed_us_ = 0u;
    uint64_t last_reload_fence_wait_us_ = 0u;
    uint64_t last_draw_fence_wait_us_ = 0u;
    uint64_t last_gpu_query_us_ = 0u;
    uint64_t last_fence_reset_us_ = 0u;
    uint64_t last_acquire_us_ = 0u;
    uint64_t last_submit_us_ = 0u;
    uint64_t last_readback_wait_us_ = 0u;
    uint64_t last_readback_process_us_ = 0u;
    uint64_t last_present_us_ = 0u;
    uint64_t last_audit_ack_us_ = 0u;
    uint64_t last_draw_unattributed_us_ = 0u;
    int64_t last_frame_elapsed_us_ = 0;
    uint64_t last_cpu_render_us_ = 0u;
    VkSemaphore image_available_ = VK_NULL_HANDLE;
    VkSemaphore render_finished_ = VK_NULL_HANDLE;
    VkFence in_flight_ = VK_NULL_HANDLE;
};

std::vector<std::wstring> command_line_args() {
    int argc = 0;
    LPWSTR* argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (!argv) {
        throw std::runtime_error("CommandLineToArgvW failed");
    }
    std::vector<std::wstring> result;
    for (int index = 0; index < argc; ++index) {
        result.emplace_back(argv[index]);
    }
    LocalFree(argv);
    return result;
}

uint32_t parse_u32(const std::wstring& value, const wchar_t* label) {
    wchar_t* end = nullptr;
    const unsigned long parsed = wcstoul(value.c_str(), &end, 10);
    if (!end || *end != L'\0' || parsed > UINT32_MAX) {
        throw std::runtime_error("invalid integer for " + narrow(label));
    }
    return static_cast<uint32_t>(parsed);
}

Options parse_options() {
    Options options;
    const std::vector<std::wstring> args = command_line_args();
    for (size_t index = 1; index < args.size(); ++index) {
        const std::wstring& arg = args[index];
        auto require_value = [&](const wchar_t* name) -> const std::wstring& {
            if (index + 1 >= args.size()) {
                throw std::runtime_error("missing value for " + narrow(name));
            }
            return args[++index];
        };

        if (arg == L"--width") {
            options.width = parse_u32(require_value(L"--width"), L"--width");
        } else if (arg == L"--height") {
            options.height = parse_u32(require_value(L"--height"), L"--height");
        } else if (arg == L"--max-frames") {
            options.max_frames = parse_u32(require_value(L"--max-frames"), L"--max-frames");
        } else if (arg == L"--presented-draw-begin") {
            options.presented_draw_begin = parse_u32(
                require_value(L"--presented-draw-begin"),
                L"--presented-draw-begin");
        } else if (arg == L"--presented-draw-end") {
            options.presented_draw_end = parse_u32(
                require_value(L"--presented-draw-end"),
                L"--presented-draw-end");
        } else if (arg == L"--presentation-pipeline-depth") {
            options.presentation_pipeline_depth = parse_u32(
                require_value(L"--presentation-pipeline-depth"),
                L"--presentation-pipeline-depth");
        } else if (arg == L"--debug-json") {
            options.debug_json = std::filesystem::path(require_value(L"--debug-json"));
        } else if (arg == L"--render-stream-json") {
            options.render_stream_json = std::filesystem::path(require_value(L"--render-stream-json"));
        } else if (arg == L"--live-render-stream-json") {
            options.render_stream_json = std::filesystem::path(require_value(L"--live-render-stream-json"));
            options.live_render_stream = true;
        } else if (arg == L"--controller-state-json") {
            options.controller_state_json = std::filesystem::path(require_value(L"--controller-state-json"));
        } else if (arg == L"--strict-render-validation") {
            options.strict_render_validation = true;
        } else if (arg == L"--analyze-render-stream") {
            options.analyze_render_stream_only = true;
        } else if (arg == L"--cpu-vertex-programs") {
            options.cpu_vertex_programs = true;
        } else if (arg == L"--cpu-vertex-attributes") {
            options.cpu_vertex_attributes = true;
        } else if (arg == L"--cpu-texture-conversion") {
            options.cpu_texture_conversion = true;
        } else if (arg == L"--flip-audit-ack") {
            options.flip_audit_ack = std::filesystem::path(
                require_value(L"--flip-audit-ack"));
        } else if (arg == L"--flip-audit-frame-directory") {
            options.flip_audit_frame_directory = std::filesystem::path(
                require_value(L"--flip-audit-frame-directory"));
        } else if (arg == L"--flip-audit-max-flips") {
            options.flip_audit_max_flips = parse_u32(
                require_value(L"--flip-audit-max-flips"),
                L"--flip-audit-max-flips");
        } else if (arg == L"--flip-audit-health-interval") {
            options.flip_audit_health_interval = parse_u32(
                require_value(L"--flip-audit-health-interval"),
                L"--flip-audit-health-interval");
        } else if (arg == L"--screenshot") {
            options.screenshot = std::filesystem::path(require_value(L"--screenshot"));
        } else if (arg == L"--hotkey-screenshot-directory") {
            options.hotkey_screenshot_directory = std::filesystem::path(
                require_value(L"--hotkey-screenshot-directory"));
        } else if (arg == L"--metrics-report-directory") {
            options.metrics_report_directory = std::filesystem::path(
                require_value(L"--metrics-report-directory"));
        } else if (arg == L"--vertex-shader") {
            options.vertex_shader = std::filesystem::path(require_value(L"--vertex-shader"));
        } else if (arg == L"--fragment-shader") {
            options.fragment_shader = std::filesystem::path(require_value(L"--fragment-shader"));
        } else if (arg == L"--texture-convert-shader") {
            options.texture_convert_shader = std::filesystem::path(
                require_value(L"--texture-convert-shader"));
        } else if (arg == L"--pipeline-cache") {
            options.pipeline_cache = std::filesystem::path(
                require_value(L"--pipeline-cache"));
        } else if (arg == L"--title") {
            options.title = require_value(L"--title");
        } else if (arg == L"--inject-input") {
            options.inject_input = true;
        } else if (arg == L"--list-adapters") {
            options.list_adapters_only = true;
        } else if (arg == L"--help" || arg == L"-h") {
            std::wcout
                << L"Usage: b2_first_frame.exe [--width N] [--height N] [--max-frames N]\n"
                << L"  --max-frames 0 keeps the window open until it is closed or Escape is pressed.\n"
                << L"                          [--presented-draw-begin N] [--presented-draw-end N]\n"
                << L"                          [--presentation-pipeline-depth 1|2]\n"
                << L"                          [--debug-json PATH] [--render-stream-json PATH]\n"
                << L"                          [--live-render-stream-json PATH] [--controller-state-json PATH]\n"
                 << L"                          [--strict-render-validation]\n"
                 << L"                          [--analyze-render-stream] [--cpu-vertex-programs]\n"
                 << L"                          [--cpu-vertex-attributes] [--cpu-texture-conversion]\n"
                << L"                          [--flip-audit-ack PATH] [--flip-audit-frame-directory PATH]\n"
                << L"                          [--flip-audit-max-flips N]\n"
                << L"                          [--flip-audit-health-interval N]\n"
                << L"                          [--screenshot PATH] [--hotkey-screenshot-directory PATH]\n"
                << L"                          [--metrics-report-directory PATH]\n"
                << L"  Press F9 to toggle the completed-guest-frame FPS counter.\n"
                << L"  Press F11 to write a metrics snapshot in the metrics report directory.\n"
                << L"  Press F12 to save the current frame in the hotkey screenshot directory.\n"
                << L"                          [--vertex-shader PATH] [--fragment-shader PATH]\n"
                << L"                          [--texture-convert-shader PATH]\n"
                << L"                          [--pipeline-cache PATH]\n"
                << L"                          [--inject-input]\n"
                << L"                          [--list-adapters]\n";
            ExitProcess(0);
        } else {
            throw std::runtime_error("unknown argument: " + narrow(arg));
        }
    }
    if (!options.analyze_render_stream_only
        && (options.vertex_shader.empty() || options.fragment_shader.empty())) {
        throw std::runtime_error("--vertex-shader and --fragment-shader are required");
    }
    if (!options.analyze_render_stream_only
        && !options.cpu_texture_conversion
        && options.texture_convert_shader.empty()) {
        throw std::runtime_error(
            "--texture-convert-shader is required for GPU texture conversion");
    }
    if (options.presented_draw_begin > options.presented_draw_end) {
        throw std::runtime_error(
            "--presented-draw-begin must not exceed --presented-draw-end");
    }
    if (options.presentation_pipeline_depth < 1u
        || options.presentation_pipeline_depth > 2u) {
        throw std::runtime_error(
            "--presentation-pipeline-depth must be 1 or 2");
    }
    if (options.presentation_pipeline_depth > 1u
        && !options.live_render_stream) {
        throw std::runtime_error(
            "--presentation-pipeline-depth 2 requires a live render stream");
    }
    if (options.flip_audit_ack.empty()
        != options.flip_audit_frame_directory.empty()) {
        throw std::runtime_error(
            "--flip-audit-ack and --flip-audit-frame-directory must be used together");
    }
    if (!options.flip_audit_ack.empty() && !options.live_render_stream) {
        throw std::runtime_error(
            "lossless flip audit requires --live-render-stream-json");
    }
    if (options.flip_audit_max_flips != 0u && options.flip_audit_ack.empty()) {
        throw std::runtime_error(
            "--flip-audit-max-flips requires lossless flip audit paths");
    }
    if (options.flip_audit_health_interval != 30u
        && options.flip_audit_ack.empty()) {
        throw std::runtime_error(
            "--flip-audit-health-interval requires lossless flip audit paths");
    }
    return options;
}

}  // namespace

int main() {
    try {
        VulkanFirstFrameApp app(parse_options());
        return app.run();
    } catch (const std::exception& exc) {
        std::cerr << "{\"event\":\"fatal\",\"error\":" << json_string(exc.what()) << "}\n";
        return 1;
    }
}
